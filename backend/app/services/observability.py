"""Persisted metrics, deduplicated in-platform alerts and bounded telemetry retention."""

import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, assert_permission, authorized_context
from app.connectors.contracts import ErrorCategory
from app.core.config import Settings
from app.core.security import utcnow
from app.models.integration import IntegrationConnection
from app.models.reliability import (
    AlertEvent,
    CircuitTransition,
    ConnectorObservation,
    CredentialRefreshAttempt,
    OperationalAlert,
    RateLimitBucket,
)
from app.models.tenant import Tenant
from app.models.workflow import (
    DeadLetterRecord,
    RetrySchedule,
    WorkflowExecution,
    WorkflowExecutionEvent,
)
from app.services.audit import record

SEVERITIES = {
    "REPEATED_DELIVERY_FAILURE": "WARNING",
    "TERMINAL_NOTIFICATION_FAILURE": "CRITICAL",
    "UNKNOWN_DELIVERY_OUTCOME": "CRITICAL",
    "CALENDAR_CONFLICT": "WARNING",
    "CONNECTION_UNHEALTHY": "WARNING",
    "CREDENTIAL_EXPIRED": "WARNING",
    "REFRESH_FAILED": "CRITICAL",
    "RATE_LIMIT_EXCEEDED": "WARNING",
    "CIRCUIT_OPEN": "WARNING",
    "WORKFLOW_DEAD_LETTERED": "CRITICAL",
    "WORKER_RECOVERY": "INFO",
    "REPEATED_CONNECTOR_FAILURE": "CRITICAL",
}
SAFE_CONTEXT = {
    "connection_id",
    "workflow_id",
    "execution_id",
    "event_id",
    "error_category",
    "reason",
    "failure_count",
}


REASONS = {
    "CREDENTIAL_EXPIRED",
    "REFRESH_FAILED",
    "NO_REFRESH_TOKEN",
    "ACCESS_REVOKED",
    "REFRESH_REVOKED",
    "REFRESH_EXPIRED",
    "SIMULATED_REFRESH_FAILED",
    "CREDENTIAL_MISSING_OR_REVOKED",
    "ENCRYPTION_UNAVAILABLE",
    "LOCAL_RATE_LIMIT",
    "CIRCUIT_OPEN",
    "HTTP_401",
    "HTTP_403",
    "HTTP_429",
    "HTTP_500",
    "HTTP_503",
    "RECOVERING_OUTAGE",
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "OPERATION_DEADLINE",
    "DATABASE_UNAVAILABLE",
    "PROBE_OWNERSHIP_LOST",
    "INTERNAL_ERROR",
    "VALIDATION_FAILED",
}
REASONS |= {"CONNECTOR_" + value.value for value in ErrorCategory}
CATEGORIES = {value.value for value in ErrorCategory} | {
    "LEASE_EXHAUSTED",
    "TRANSFORMATION_REVIEW",
    "UNCERTAIN_ACTION",
    "DATABASE_UNAVAILABLE",
}


def safe_context(context: dict) -> dict:
    safe = {}
    for key, value in context.items():
        if key in {"connection_id", "workflow_id", "execution_id", "event_id"}:
            try:
                safe[key] = str(uuid.UUID(str(value)))
            except (ValueError, TypeError):
                pass
        elif (
            key == "reason"
            and isinstance(value, str)
            and value in REASONS
            or key == "error_category"
            and isinstance(value, str)
            and value in CATEGORIES
            or key == "failure_count"
            and type(value) is int
            and 0 <= value <= 1000000
        ):
            safe[key] = value
    return safe


def emit(
    db: Session,
    tenant_id: uuid.UUID,
    category: str,
    resource_id: uuid.UUID,
    correlation_id: uuid.UUID,
    source_key: str,
    context: dict | None = None,
) -> OperationalAlert:
    if category not in SEVERITIES:
        raise ValueError("Unsupported alert category")
    # All callers follow tenant -> execution/connection ordering. The lock also fences
    # alert deduplication across connection/workflow events and operator updates.
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    row = db.scalar(
        select(OperationalAlert)
        .where(
            OperationalAlert.tenant_id == tenant_id,
            OperationalAlert.category == category,
            OperationalAlert.resource_key == str(resource_id),
        )
        .with_for_update()
    )
    safe = safe_context(context or {})
    event_type = "OPENED" if row is None or row.status == "RESOLVED" else "RECURRED"
    if row is not None and db.scalar(
        select(AlertEvent.id).where(
            AlertEvent.alert_id == row.id,
            AlertEvent.source_key == source_key,
            AlertEvent.event_type.in_(["OPENED", "RECURRED"]),
        )
    ):
        return row
    now = utcnow()
    if row is None:
        row = OperationalAlert(
            tenant_id=tenant_id,
            category=category,
            resource_key=str(resource_id),
            severity=SEVERITIES[category],
            correlation_id=correlation_id,
            context=safe,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(row)
        db.flush()
    else:
        row.occurrence_count += 1
        row.last_seen_at, row.correlation_id, row.context = now, correlation_id, safe
        if row.status == "RESOLVED":
            row.status = "OPEN"
            row.resolved_at = row.acknowledged_at = None
    db.add(
        AlertEvent(
            tenant_id=tenant_id,
            alert_id=row.id,
            event_type=event_type,
            source_key=source_key,
            correlation_id=correlation_id,
        )
    )
    record(db, "alert." + event_type.lower(), "alert", row.id, tenant_id)
    from app.services.operations import observe_alert

    observe_alert(db, row)
    db.flush()
    return row


def resolve_connection_alerts(
    db: Session,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    correlation_id: uuid.UUID,
    source_key: str,
    categories: list[str] | None = None,
) -> None:
    scope = categories or [
        "CONNECTION_UNHEALTHY",
        "CREDENTIAL_EXPIRED",
        "REFRESH_FAILED",
        "CIRCUIT_OPEN",
        "REPEATED_CONNECTOR_FAILURE",
    ]
    for row in db.scalars(
        select(OperationalAlert)
        .where(
            OperationalAlert.tenant_id == tenant_id,
            OperationalAlert.resource_key == str(connection_id),
            OperationalAlert.category.in_(scope),
            OperationalAlert.status != "RESOLVED",
        )
        .with_for_update()
    ):
        row.status, row.resolved_at = "RESOLVED", utcnow()
        db.add(
            AlertEvent(
                tenant_id=tenant_id,
                alert_id=row.id,
                event_type="RESOLVED",
                source_key=source_key,
                correlation_id=correlation_id,
            )
        )
        record(db, "alert.resolved", "alert", row.id, tenant_id)


def get_alert(db: Session, context: TenantContext, alert_id: uuid.UUID, lock=False):
    query = select(OperationalAlert).where(
        OperationalAlert.id == alert_id, OperationalAlert.tenant_id == context.tenant.id
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "Alert unavailable")
    return row


def alert_output(row: OperationalAlert) -> dict:
    data = {
        k: getattr(row, k)
        for k in (
            "id",
            "category",
            "severity",
            "status",
            "correlation_id",
            "context",
            "occurrence_count",
            "first_seen_at",
            "last_seen_at",
            "acknowledged_at",
            "resolved_at",
        )
    }

    data["context"] = safe_context(row.context)
    return data


def update_alert(
    db: Session, context: TenantContext, alert_id: uuid.UUID, action: str, correlation_id: uuid.UUID
) -> OperationalAlert:
    db.scalar(select(Tenant).where(Tenant.id == context.tenant.id).with_for_update())
    db.refresh(context.membership)
    assert_permission(
        authorized_context(db, context.identity, context.tenant.id),
        "alerts:acknowledge" if action == "acknowledge" else "alerts:resolve",
    )
    row = get_alert(db, context, alert_id, True)
    if action == "acknowledge":
        if row.status == "RESOLVED":
            raise HTTPException(409, "Resolved alert cannot be acknowledged")
        if row.status == "ACKNOWLEDGED":
            return row
        row.status, row.acknowledged_at = "ACKNOWLEDGED", utcnow()
        event = "ACKNOWLEDGED"
    elif action == "resolve":
        if row.status == "RESOLVED":
            return row
        row.status, row.resolved_at = "RESOLVED", utcnow()
        event = "RESOLVED"
    else:
        raise ValueError("Unsupported alert action")
    db.add(
        AlertEvent(
            tenant_id=row.tenant_id,
            alert_id=row.id,
            event_type=event,
            source_key=str(uuid.uuid4()),
            actor_id=context.identity.user.id,
            correlation_id=correlation_id,
        )
    )
    record(db, "alert." + event.lower(), "alert", row.id, row.tenant_id, context.identity.user.id)
    return row


def prune(db: Session, tenant_id: uuid.UUID, settings: Settings) -> None:
    """Only Phase 6 telemetry is retained by TTL/cap; never business/inbox/credentials/audit."""
    cutoff = utcnow() - timedelta(days=settings.operation_retention_days)
    for model in [ConnectorObservation, CircuitTransition, CredentialRefreshAttempt, AlertEvent]:
        cap = settings.operation_retention_max_rows
        old = list(
            db.scalars(
                select(model.id)
                .where(model.tenant_id == tenant_id, model.created_at < cutoff)
                .order_by(model.created_at, model.id)
                .limit(100)
            )
        )
        overflow = list(
            db.scalars(
                select(model.id)
                .where(model.tenant_id == tenant_id)
                .order_by(model.created_at.desc(), model.id.desc())
                .offset(cap)
                .limit(100)
            )
        )
        ids = set(old + overflow)
        if ids:
            db.execute(delete(model).where(model.tenant_id == tenant_id, model.id.in_(ids)))
    # Expired fixed windows have no future quota effect. Current windows are retained.
    old_buckets = list(
        db.scalars(
            select(RateLimitBucket.id)
            .where(
                RateLimitBucket.tenant_id == tenant_id,
                RateLimitBucket.window_start < utcnow() - timedelta(seconds=240),
            )
            .limit(100)
        )
    )
    if old_buckets:
        db.execute(
            delete(RateLimitBucket).where(
                RateLimitBucket.id.in_(old_buckets), RateLimitBucket.tenant_id == tenant_id
            )
        )


def metrics(db: Session, tenant_id: uuid.UUID, settings: Settings, hours=24) -> dict:
    since = max(
        utcnow() - timedelta(hours=hours),
        utcnow() - timedelta(days=settings.operation_retention_days),
    )
    scope = [ConnectorObservation.tenant_id == tenant_id, ConnectorObservation.created_at >= since]
    groups = db.execute(
        select(ConnectorObservation.outcome, func.count())
        .where(*scope)
        .group_by(ConnectorObservation.outcome)
    ).all()
    outcomes = dict(groups)

    def count(model, *extra):
        return (
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.tenant_id == tenant_id, model.created_at >= since, *extra)
            )
            or 0
        )

    latencies = db.execute(
        select(
            func.avg(ConnectorObservation.latency_ms), func.max(ConnectorObservation.latency_ms)
        ).where(*scope)
    ).one()
    backlog = dict(
        db.execute(
            select(WorkflowExecution.state, func.count())
            .where(WorkflowExecution.tenant_id == tenant_id)
            .group_by(WorkflowExecution.state)
        ).all()
    )
    health = dict(
        db.execute(
            select(IntegrationConnection.health_status, func.count())
            .where(IntegrationConnection.tenant_id == tenant_id)
            .group_by(IntegrationConnection.health_status)
        ).all()
    )
    return {
        "since": since,
        "checked_at": utcnow(),
        "operation_count": sum(outcomes.values()),
        "admitted_operation_count": count(
            ConnectorObservation, ConnectorObservation.admitted.is_(True)
        ),
        "success_count": outcomes.get("SUCCEEDED", 0),
        "failure_count": outcomes.get("FAILED", 0),
        "deferred_count": outcomes.get("DEFERRED", 0),
        "retry_count": count(RetrySchedule),
        "operation_retry_count": count(
            ConnectorObservation, ConnectorObservation.retry_attempt.is_(True)
        ),
        "rate_limit_count": count(
            ConnectorObservation, ConnectorObservation.error_category == "RATE_LIMITED"
        ),
        "circuit_transition_count": count(CircuitTransition),
        "credential_refresh_count": count(CredentialRefreshAttempt),
        "credential_refresh_success_count": count(
            CredentialRefreshAttempt, CredentialRefreshAttempt.outcome == "SUCCEEDED"
        ),
        "latency_ms": {
            "average": round(float(latencies[0]), 2) if latencies[0] is not None else None,
            "maximum": latencies[1],
        },
        "workflow_backlog": backlog,
        "dead_letter_count": count(DeadLetterRecord),
        "worker_lease_recovery_count": count(
            WorkflowExecutionEvent, WorkflowExecutionEvent.event_type == "LEASE_RECOVERED"
        ),
        "connection_health": health,
        "retention": {
            "days": settings.operation_retention_days,
            "maximum_rows_per_tenant": settings.operation_retention_max_rows,
        },
        "basis": "Retained committed observations; counts include admitted failures and pre-admission deferrals. No extrapolation or distributed tracing.",
    }
