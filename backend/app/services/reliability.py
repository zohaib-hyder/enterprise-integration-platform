"""Shared single-attempt execution. Durable workflows own every retry and wait."""

import math
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.connectors.contracts import (
    ConnectorError,
    ConnectorOperation,
    ConnectorResult,
    ErrorCategory,
)
from app.connectors.registry import registry
from app.connectors.reliability import ReliabilityError, parse_retry_after
from app.core.config import Settings
from app.core.security import utcnow
from app.models.integration import IntegrationConnection
from app.models.reliability import (
    CircuitBreaker,
    CircuitTransition,
    ConnectionReliabilityPolicy,
    ConnectorObservation,
    FailureInjection,
    RateLimitBucket,
    RateLimitPolicy,
)
from app.models.tenant import Tenant
from app.schemas.reliability import Budget, FailureConfig, ReliabilityPolicy
from app.schemas.workflow import RetryPolicy
from app.services import credentials, observability
from app.services.audit import record
from app.services.webhooks import manage_lock

DEFAULT_LIMITS = {"TENANT": 10000, "CONNECTION": 5000, "CONNECTOR": 5000}
TEMPORARY_REASONS = {
    "HTTP_500",
    "HTTP_503",
    "RECOVERING_OUTAGE",
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "OPERATION_DEADLINE",
    "DATABASE_UNAVAILABLE",
    "CONNECTOR_TIMEOUT",
    "CONNECTOR_TEMPORARY_UNAVAILABLE",
}


def policy(db: Session, connection: IntegrationConnection) -> tuple[ReliabilityPolicy, bool]:
    row = db.scalar(
        select(ConnectionReliabilityPolicy).where(
            ConnectionReliabilityPolicy.tenant_id == connection.tenant_id,
            ConnectionReliabilityPolicy.connection_id == connection.id,
        )
    )
    return (
        (ReliabilityPolicy.model_validate(row.configuration), True)
        if row
        else (ReliabilityPolicy(), False)
    )


def configure_policy(
    db: Session, context: TenantContext, connection_id: uuid.UUID, data: ReliabilityPolicy
):
    from app.services.integrations import get_connection

    manage_lock(db, context)
    connection = get_connection(db, context, connection_id, True)
    if connection.connector_type == "generic_rest":
        raise HTTPException(422, "Outbound transport remains disabled")
    row = db.scalar(
        select(ConnectionReliabilityPolicy).where(
            ConnectionReliabilityPolicy.connection_id == connection.id,
            ConnectionReliabilityPolicy.tenant_id == context.tenant.id,
        )
    )
    if row is None:
        row = ConnectionReliabilityPolicy(
            tenant_id=context.tenant.id,
            connection_id=connection.id,
            configuration=data.model_dump(mode="json"),
        )
        db.add(row)
    else:
        row.configuration = data.model_dump(mode="json")
    connection.health_status, connection.last_checked_at = "UNKNOWN", None
    record(
        db,
        "reliability.policy_updated",
        "connection",
        connection.id,
        context.tenant.id,
        context.identity.user.id,
    )
    db.flush()
    return data.model_dump()


def scope_key(connection: IntegrationConnection, scope: str) -> str:
    return (
        "tenant"
        if scope == "TENANT"
        else str(connection.id)
        if scope == "CONNECTION"
        else connection.connector_type
    )


def quota_policies(db: Session, connection: IntegrationConnection, create=False) -> list:
    result = []
    for scope in ["TENANT", "CONNECTOR", "CONNECTION"]:
        key = scope_key(connection, scope)
        row = db.scalar(
            select(RateLimitPolicy).where(
                RateLimitPolicy.tenant_id == connection.tenant_id,
                RateLimitPolicy.scope == scope,
                RateLimitPolicy.scope_key == key,
            )
        )
        if row is None and create:
            row = RateLimitPolicy(
                tenant_id=connection.tenant_id,
                scope=scope,
                scope_key=key,
                connection_id=connection.id if scope == "CONNECTION" else None,
                limit=DEFAULT_LIMITS[scope],
                window_seconds=60,
            )
            db.add(row)
            db.flush()
        result.append((scope, row))
    return result


def window_start(now: datetime, seconds: int) -> datetime:
    return datetime.fromtimestamp(math.floor(now.timestamp() / seconds) * seconds, tz=UTC)


def rate_status(db: Session, connection: IntegrationConnection) -> list[dict]:
    now = db.scalar(select(func.clock_timestamp()))
    result = []
    for scope, row in quota_policies(db, connection):
        seconds = row.window_seconds if row else 60
        start = window_start(now, seconds)
        used = (
            db.scalar(
                select(RateLimitBucket.used).where(
                    RateLimitBucket.policy_id == row.id,
                    RateLimitBucket.tenant_id == connection.tenant_id,
                    RateLimitBucket.window_start == start,
                )
            )
            or 0
            if row
            else 0
        )
        limit = row.limit if row else DEFAULT_LIMITS[scope]
        reset = start + timedelta(seconds=seconds)
        result.append(
            {
                "scope": scope,
                "limit": limit,
                "used": used,
                "remaining": max(0, limit - used),
                "window_seconds": seconds,
                "reset_at": reset,
                "retry_after_seconds": max(1, math.ceil((reset - now).total_seconds()))
                if used >= limit
                else 0,
            }
        )
    return result


def configure_budget(db: Session, context: TenantContext, connection_id: uuid.UUID, data: Budget):
    from app.services.integrations import get_connection

    manage_lock(db, context)
    connection = get_connection(db, context, connection_id, True)
    if connection.connector_type == "generic_rest":
        raise HTTPException(422, "Outbound transport remains disabled")
    rows = dict(quota_policies(db, connection, True))
    row = rows[data.scope]
    row.limit, row.window_seconds = data.limit, data.window_seconds
    # Counters are not cleared by configuration; reductions take effect immediately.
    record(
        db,
        "reliability.budget_updated",
        "connection",
        connection.id,
        context.tenant.id,
        context.identity.user.id,
    )
    db.flush()
    return rate_status(db, connection)


def consume_quota(db: Session, connection: IntegrationConnection) -> None:
    # Caller owns tenant + connection locks, so all three decisions/increments are atomic.
    now = db.scalar(select(func.clock_timestamp()))
    buckets = []
    retry_after = 0
    for scope, row in quota_policies(db, connection, True):
        start = window_start(now, row.window_seconds)
        bucket = db.scalar(
            select(RateLimitBucket)
            .where(
                RateLimitBucket.policy_id == row.id,
                RateLimitBucket.tenant_id == connection.tenant_id,
                RateLimitBucket.window_start == start,
            )
            .with_for_update()
        )
        if bucket is None:
            bucket = RateLimitBucket(
                tenant_id=connection.tenant_id, policy_id=row.id, window_start=start, used=0
            )
            db.add(bucket)
            db.flush()
        if bucket.used >= row.limit:
            retry_after = max(
                retry_after,
                math.ceil((start + timedelta(seconds=row.window_seconds) - now).total_seconds()),
            )
        buckets.append(bucket)
    if retry_after:
        raise ReliabilityError(
            ErrorCategory.RATE_LIMITED,
            "LOCAL_RATE_LIMIT",
            retry_after=min(300, max(1, retry_after)),
        )
    for bucket in buckets:
        bucket.used += 1
    db.flush()


def breaker(db: Session, connection: IntegrationConnection) -> CircuitBreaker:
    row = db.scalar(
        select(CircuitBreaker)
        .where(
            CircuitBreaker.tenant_id == connection.tenant_id,
            CircuitBreaker.connection_id == connection.id,
        )
        .with_for_update()
    )
    if row is None:
        row = CircuitBreaker(tenant_id=connection.tenant_id, connection_id=connection.id)
        db.add(row)
        db.flush()
    return row


def transition(
    db: Session,
    row: CircuitBreaker,
    state: str,
    reason: str,
    correlation_id: uuid.UUID,
    operation_id: uuid.UUID,
):
    legal = {"CLOSED": {"OPEN"}, "OPEN": {"HALF_OPEN"}, "HALF_OPEN": {"CLOSED", "OPEN"}}
    if state not in legal.get(row.state, set()):
        raise ValueError("Illegal breaker transition")
    db.add(
        CircuitTransition(
            tenant_id=row.tenant_id,
            connection_id=row.connection_id,
            from_state=row.state,
            to_state=state,
            reason=reason,
            correlation_id=correlation_id,
            operation_id=operation_id,
        )
    )
    row.state = state
    record(db, "circuit." + state.lower(), "connection", row.connection_id, row.tenant_id)


def check_breaker(row: CircuitBreaker) -> None:
    now = utcnow()
    due = (
        row.open_until
        if row.state == "OPEN"
        else row.probe_expires_at
        if row.state == "HALF_OPEN"
        else None
    )
    if due and due > now:
        raise ReliabilityError(
            ErrorCategory.TEMPORARY_UNAVAILABLE,
            "CIRCUIT_OPEN",
            retry_after=min(300, max(1, math.ceil((due - now).total_seconds()))),
        )


def reserve_probe(
    db: Session, row: CircuitBreaker, correlation_id: uuid.UUID, operation_id: uuid.UUID
) -> uuid.UUID | None:
    check_breaker(row)
    if row.state == "HALF_OPEN":
        transition(db, row, "OPEN", "PROBE_EXPIRED", correlation_id, operation_id)
    if row.state == "OPEN":
        transition(db, row, "HALF_OPEN", "COOLDOWN_COMPLETE", correlation_id, operation_id)
        row.probe_token, row.probe_expires_at = uuid.uuid4(), utcnow() + timedelta(seconds=15)
        db.flush()
        return row.probe_token
    return None


def fault_output(row: FailureInjection | None) -> dict:
    if row is None:
        return {
            "enabled": False,
            "scenario": "NONE",
            "remaining": 0,
            "consumed": 0,
            "expires_at": None,
            "retry_after": None,
        }
    return {
        **{
            k: getattr(row, k)
            for k in ("scenario", "remaining", "consumed", "expires_at", "retry_after")
        },
        "enabled": row.enabled
        and row.remaining > 0
        and row.expires_at is not None
        and row.expires_at > utcnow(),
    }


def configure_fault(
    db: Session, context: TenantContext, connection_id: uuid.UUID, data: FailureConfig
):
    from app.services.integrations import get_connection

    manage_lock(db, context)
    connection = get_connection(db, context, connection_id, True)
    if connection.connector_type not in {
        "synthetic_crm",
        "synthetic_ats",
        "synthetic_slack",
        "synthetic_email",
        "synthetic_calendar",
    }:
        raise HTTPException(422, "Fault scenarios are restricted to local synthetic connectors")
    if data.enabled and data.scenario in {
        "EXPIRED_ACCESS_TOKEN",
        "REVOKED_REFRESH_TOKEN",
        "REFRESH_FAILED",
    }:
        config, _ = policy(db, connection)
        if (
            config.credential_mode != "OAUTH"
            or credentials.latest(db, connection, "OAUTH_ACCESS_TOKEN") is None
            or credentials.latest(db, connection, "OAUTH_REFRESH_TOKEN") is None
        ):
            raise HTTPException(422, "Configure a simulated OAuth token pair and enforcement first")
    row = db.scalar(
        select(FailureInjection).where(
            FailureInjection.connection_id == connection.id,
            FailureInjection.tenant_id == context.tenant.id,
        )
    )
    if row is None:
        row = FailureInjection(tenant_id=context.tenant.id, connection_id=connection.id)
        db.add(row)
    row.enabled, row.scenario = data.enabled, data.scenario if data.enabled else "NONE"
    row.remaining, row.consumed = data.attempts if data.enabled else 0, 0
    row.retry_after = data.retry_after
    row.expires_at = utcnow() + timedelta(seconds=data.ttl_seconds) if data.enabled else None
    record(
        db,
        "reliability.fault_configured",
        "connection",
        connection.id,
        context.tenant.id,
        context.identity.user.id,
    )
    db.flush()
    return fault_output(row)


def consume_fault(db: Session, connection: IntegrationConnection) -> FailureInjection | None:
    row = db.scalar(
        select(FailureInjection)
        .where(
            FailureInjection.connection_id == connection.id,
            FailureInjection.tenant_id == connection.tenant_id,
        )
        .with_for_update()
    )
    if row is None or not fault_output(row)["enabled"]:
        if row:
            row.enabled = False
        return None
    row.remaining -= 1
    row.consumed += 1
    if row.remaining == 0:
        row.enabled = False
    db.flush()
    return row


def injected_error(row: FailureInjection | None) -> ReliabilityError | None:
    if row is None:
        return None
    scenario = row.scenario
    categories = {
        "HTTP_401": ErrorCategory.AUTHENTICATION_FAILED,
        "HTTP_403": ErrorCategory.AUTHORIZATION_FAILED,
        "HTTP_429": ErrorCategory.RATE_LIMITED,
        "HTTP_500": ErrorCategory.INTERNAL_ERROR,
        "HTTP_503": ErrorCategory.TEMPORARY_UNAVAILABLE,
        "RECOVERING_OUTAGE": ErrorCategory.TEMPORARY_UNAVAILABLE,
        "CONNECT_TIMEOUT": ErrorCategory.TIMEOUT,
        "READ_TIMEOUT": ErrorCategory.TIMEOUT,
    }
    if scenario not in categories:
        return None
    retry_after = (
        parse_retry_after(row.retry_after)
        if row.retry_after
        else 1
        if scenario == "HTTP_429"
        else None
    )
    return ReliabilityError(
        categories[scenario],
        scenario,
        retryable=scenario in TEMPORARY_REASONS or scenario == "HTTP_429",
        retry_after=retry_after,
    )


def inspect(db: Session, connection: IntegrationConnection) -> dict:
    config, configured = policy(db, connection)
    circuit = db.scalar(
        select(CircuitBreaker).where(
            CircuitBreaker.connection_id == connection.id,
            CircuitBreaker.tenant_id == connection.tenant_id,
        )
    )
    fault = db.scalar(
        select(FailureInjection).where(
            FailureInjection.connection_id == connection.id,
            FailureInjection.tenant_id == connection.tenant_id,
        )
    )
    return {
        "connection_id": connection.id,
        "health_status": connection.health_status,
        "last_checked_at": connection.last_checked_at,
        "policy": config.model_dump(),
        "policy_configured": configured,
        "one_off_attempts": 1,
        "rate_limits": rate_status(db, connection),
        "circuit": {
            "state": circuit.state if circuit else "CLOSED",
            "failure_count": circuit.failure_count if circuit else 0,
            "open_until": circuit.open_until if circuit else None,
            "probe_expires_at": circuit.probe_expires_at if circuit else None,
        },
        "failure_injection": fault_output(fault),
    }


def apply_token_fault(
    db: Session, connection: IntegrationConnection, fault: FailureInjection | None
) -> None:
    if fault and fault.scenario in {
        "EXPIRED_ACCESS_TOKEN",
        "REVOKED_REFRESH_TOKEN",
        "REFRESH_FAILED",
    }:
        access = credentials.latest(db, connection, "OAUTH_ACCESS_TOKEN")
        if access:
            access.expires_at = utcnow() - timedelta(seconds=1)
        if fault.scenario == "REVOKED_REFRESH_TOKEN":
            refresh_token = credentials.latest(db, connection, "OAUTH_REFRESH_TOKEN")
            if refresh_token:
                refresh_token.status, refresh_token.revoked_at = "REVOKED", utcnow()
        db.flush()


def execute_once(
    db: Session,
    context: TenantContext,
    connection_id: uuid.UUID,
    operation: ConnectorOperation,
    *,
    settings: Settings | None = None,
    correlation_id: uuid.UUID | None = None,
    workflow_execution_id: uuid.UUID | None = None,
    attempt: int = 1,
    deadline_seconds: int | None = None,
) -> ConnectorResult:
    from app.services.integrations import authorize_management, get_connection

    settings = settings or db.info.get("settings") or Settings()
    authorize_management(db, context)
    connection = get_connection(db, context, connection_id)
    if connection.status != "ENABLED":
        raise ConnectorError(ErrorCategory.AUTHORIZATION_FAILED)
    started = time.monotonic()
    operation_id = uuid.uuid4()
    correlation_id = correlation_id or uuid.uuid4()
    db.info["last_operation_id"] = operation_id
    config, configured = policy(db, connection)
    overall_ms = min(config.operation_deadline_ms, (deadline_seconds or 10) * 1000)
    old_lock = db.scalar(text("SELECT current_setting('lock_timeout')"))
    old_read = db.scalar(text("SELECT current_setting('statement_timeout')"))
    admitted = False
    locked = False
    circuit = None
    error = None
    result = None
    probe = None
    try:
        # Savepoint protects the caller's transaction if lock acquisition times out.
        with db.begin_nested():
            db.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": str(min(config.connect_timeout_ms, overall_ms))},
            )
            db.scalar(select(Tenant).where(Tenant.id == context.tenant.id).with_for_update())
            authorize_management(db, context)
            connection = get_connection(db, context, connection_id, True)
            db.refresh(connection)
            if connection.status != "ENABLED":
                raise ConnectorError(ErrorCategory.AUTHORIZATION_FAILED)
        locked = True
        config, configured = policy(db, connection)
        overall_ms = min(config.operation_deadline_ms, (deadline_seconds or 10) * 1000)
        circuit = breaker(db, connection)
        check_breaker(circuit)
        consume_quota(db, connection)
        probe = reserve_probe(db, circuit, correlation_id, operation_id)
        fault = consume_fault(db, connection)
        apply_token_fault(db, connection, fault)
        credentials.authorize(
            db,
            settings,
            connection,
            config.credential_mode,
            correlation_id,
            operation_id,
            context.identity.user.id,
            bool(fault and fault.scenario == "REFRESH_FAILED"),
        )
        admitted = True
        simulated = injected_error(fault)
        if simulated:
            raise simulated
        adapter = registry.resolve(connection.connector_type)
        remaining_ms = overall_ms - int((time.monotonic() - started) * 1000)
        if remaining_ms <= 0:
            raise ReliabilityError(ErrorCategory.TIMEOUT, "OPERATION_DEADLINE", retryable=True)
        with db.begin_nested():
            db.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": str(min(config.read_timeout_ms, remaining_ms))},
            )
            result = adapter.execute(
                db,
                context.tenant.id,
                connection.id,
                adapter.validate_config(connection.public_config),
                operation,
            )
            if (time.monotonic() - started) * 1000 > overall_ms:
                raise ReliabilityError(ErrorCategory.TIMEOUT, "OPERATION_DEADLINE", retryable=True)
            if circuit.probe_token != probe and probe is not None:
                raise ReliabilityError(ErrorCategory.TEMPORARY_UNAVAILABLE, "PROBE_OWNERSHIP_LOST")
    except ReliabilityError as failure:
        error = failure
    except ConnectorError as failure:
        error = ReliabilityError(
            failure.category, "CONNECTOR_" + failure.category.value, retryable=failure.retryable
        )
    except ValidationError:
        error = ReliabilityError(
            ErrorCategory.VALIDATION_FAILED, "VALIDATION_FAILED", retryable=False
        )
    except DBAPIError as failure:
        code = getattr(failure.orig, "sqlstate", None)
        error = ReliabilityError(
            ErrorCategory.TIMEOUT
            if code in {"57014", "55P03"}
            else ErrorCategory.TEMPORARY_UNAVAILABLE,
            "CONNECT_TIMEOUT"
            if code == "55P03"
            else "READ_TIMEOUT"
            if code == "57014"
            else "DATABASE_UNAVAILABLE",
            retryable=True,
        )
    except Exception:  # noqa: BLE001 - never expose adapter exceptions, inputs or credential bytes
        error = ReliabilityError(ErrorCategory.INTERNAL_ERROR, "INTERNAL_ERROR", retryable=False)
    finally:
        # Local settings from a failed savepoint are reset; restore the worker's bounds too.
        db.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": old_read}
        )
        db.execute(text("SELECT set_config('lock_timeout', :timeout, true)"), {"timeout": old_lock})
    if not locked and error is not None:
        # A timed-out lock cannot safely persist tenant FK evidence until it is acquired.
        # No quota/effect was committed; return a classified failure without inventing metrics.
        error.operation_id = operation_id
        raise error
    now = utcnow()
    if error is not None:
        error.operation_id = operation_id
        if configured:
            error.retry_policy = RetryPolicy(
                max_attempts=config.max_attempts,
                base_delay_seconds=config.base_delay_seconds,
                max_delay_seconds=config.max_delay_seconds,
                jitter_fraction=config.jitter_fraction,
            )
        outcome = (
            "DEFERRED"
            if error.reason in {"LOCAL_RATE_LIMIT", "CIRCUIT_OPEN", "HTTP_429"}
            else "FAILED"
        )
        if circuit is not None:
            if outcome == "FAILED" and error.category not in {
                ErrorCategory.VALIDATION_FAILED,
                ErrorCategory.NOT_FOUND,
                ErrorCategory.CONFLICT,
                ErrorCategory.UNSUPPORTED_OPERATION,
            }:
                connection.health_status, connection.last_checked_at = "UNAVAILABLE", now
                circuit.operation_failure_count += 1
                observability.emit(
                    db,
                    connection.tenant_id,
                    "CONNECTION_UNHEALTHY",
                    connection.id,
                    correlation_id,
                    str(operation_id),
                    {
                        "connection_id": connection.id,
                        "error_category": error.category.value,
                        "reason": error.reason,
                    },
                )
                if circuit.operation_failure_count >= config.repeated_failure_threshold:
                    observability.emit(
                        db,
                        connection.tenant_id,
                        "REPEATED_CONNECTOR_FAILURE",
                        connection.id,
                        correlation_id,
                        str(operation_id),
                        {
                            "connection_id": connection.id,
                            "failure_count": circuit.operation_failure_count,
                            "reason": error.reason,
                        },
                    )
            if error.reason in TEMPORARY_REASONS:
                circuit.failure_count += 1
                if (
                    circuit.state == "HALF_OPEN"
                    or circuit.failure_count >= config.circuit_failure_threshold
                ):
                    transition(db, circuit, "OPEN", error.reason, correlation_id, operation_id)
                    circuit.open_until = now + timedelta(seconds=config.circuit_cooldown_seconds)
                    circuit.probe_token = circuit.probe_expires_at = None
            elif circuit.state == "HALF_OPEN" and error.reason != "CIRCUIT_OPEN":
                # A permanent auth/validation result is not evidence of healthy recovery.
                transition(db, circuit, "OPEN", error.reason, correlation_id, operation_id)
                circuit.open_until = now + timedelta(seconds=config.circuit_cooldown_seconds)
                circuit.probe_token = circuit.probe_expires_at = None
        if error.category == ErrorCategory.RATE_LIMITED:
            observability.emit(
                db,
                connection.tenant_id,
                "RATE_LIMIT_EXCEEDED",
                connection.id,
                correlation_id,
                str(operation_id),
                {"connection_id": connection.id, "reason": error.reason},
            )
        if error.reason == "CIRCUIT_OPEN" or circuit is not None and circuit.state == "OPEN":
            observability.emit(
                db,
                connection.tenant_id,
                "CIRCUIT_OPEN",
                connection.id,
                correlation_id,
                str(operation_id),
                {"connection_id": connection.id, "reason": error.reason},
            )
    else:
        outcome = "SUCCEEDED"
        connection.health_status, connection.last_checked_at = "HEALTHY", now
        circuit.failure_count = circuit.operation_failure_count = 0
        if circuit.state == "HALF_OPEN":
            transition(db, circuit, "CLOSED", "PROBE_SUCCEEDED", correlation_id, operation_id)
        circuit.open_until = circuit.probe_token = circuit.probe_expires_at = None
        observability.resolve_connection_alerts(
            db, connection.tenant_id, connection.id, correlation_id, str(operation_id)
        )
        record(
            db,
            "connector.executed",
            "connection",
            connection.id,
            connection.tenant_id,
            context.identity.user.id,
        )
    db.add(
        ConnectorObservation(
            id=operation_id,
            tenant_id=connection.tenant_id,
            connection_id=connection.id,
            connector_type=connection.connector_type,
            operation_name=operation.name.value,
            correlation_id=correlation_id,
            workflow_execution_id=workflow_execution_id,
            outcome=outcome,
            reason=error.reason if error else None,
            error_category=error.category.value if error else None,
            admitted=admitted,
            retry_attempt=attempt > 1,
            retry_after_seconds=error.retry_after if error else None,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
    )
    db.flush()
    observability.prune(db, context.tenant.id, settings)
    if error:
        raise error
    return result
