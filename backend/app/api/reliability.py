"""Tenant-authorized operations API. Secrets have submission-only contracts."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.canonical import DB, Manage, Read, mutations, page
from app.api.dependencies import TenantContext, require_tenant_permission
from app.api.webhooks import Limit, Offset
from app.connectors.contracts import ErrorCategory
from app.connectors.reliability import ReliabilityError
from app.models.reliability import (
    AlertEvent,
    CircuitTransition,
    ConnectionCredential,
    CredentialRefreshAttempt,
    OperationalAlert,
)
from app.schemas.reliability import (
    AlertCategory,
    AlertStatus,
    Budget,
    CredentialMetadata,
    CredentialReplacement,
    CredentialSubmission,
    FailureConfig,
    ReliabilityPolicy,
    Severity,
)
from app.services import credentials, integrations, observability, reliability

router = APIRouter(prefix="/api/v1")
AlertManage = Annotated[TenantContext, Depends(require_tenant_permission("alerts:acknowledge"))]
Hours = Annotated[int, Query(ge=1, le=168)]


def correlation(request: Request) -> uuid.UUID:
    return uuid.UUID(request.state.correlation_id)


def error_response(error: ReliabilityError, correlation_id: uuid.UUID) -> JSONResponse:
    statuses = {
        ErrorCategory.AUTHENTICATION_FAILED: 401,
        ErrorCategory.AUTHORIZATION_FAILED: 403,
        ErrorCategory.RATE_LIMITED: 429,
        ErrorCategory.TIMEOUT: 504,
        ErrorCategory.TEMPORARY_UNAVAILABLE: 503,
        ErrorCategory.VALIDATION_FAILED: 422,
        ErrorCategory.NOT_FOUND: 404,
        ErrorCategory.CONFLICT: 409,
        ErrorCategory.UNSUPPORTED_OPERATION: 400,
        ErrorCategory.INTERNAL_ERROR: 500,
    }
    headers = {}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    if error.operation_id:
        headers["X-Operation-ID"] = str(error.operation_id)
    return JSONResponse(
        {
            "detail": error.safe_message,
            "error": {
                "category": error.category.value,
                "reason": error.reason,
                "retryable": error.retryable,
                "retry_after_seconds": error.retry_after,
            },
            "operation_id": str(error.operation_id) if error.operation_id else None,
            "correlation_id": str(correlation_id),
        },
        status_code=statuses[error.category],
        headers=headers,
    )


@router.get("/connections/{connection_id}/reliability")
def inspect(connection_id: uuid.UUID, context: Read, db: DB) -> dict:
    return reliability.inspect(db, integrations.get_connection(db, context, connection_id))


@router.put("/connections/{connection_id}/reliability", dependencies=mutations)
def configure(connection_id: uuid.UUID, data: ReliabilityPolicy, context: Manage, db: DB) -> dict:
    return reliability.configure_policy(db, context, connection_id, data)


@router.get("/connections/{connection_id}/health-details")
def health_details(connection_id: uuid.UUID, context: Read, db: DB) -> dict:
    connection = integrations.get_connection(db, context, connection_id)
    return reliability.inspect(db, connection)


@router.get("/connections/{connection_id}/rate-limits")
def budgets(connection_id: uuid.UUID, context: Read, db: DB) -> list[dict]:
    return reliability.rate_status(db, integrations.get_connection(db, context, connection_id))


@router.put("/connections/{connection_id}/rate-limits", dependencies=mutations)
def configure_budget(connection_id: uuid.UUID, data: Budget, context: Manage, db: DB) -> list[dict]:
    return reliability.configure_budget(db, context, connection_id, data)


@router.get("/connections/{connection_id}/circuit")
def circuit(connection_id: uuid.UUID, context: Read, db: DB) -> dict:
    return reliability.inspect(db, integrations.get_connection(db, context, connection_id))[
        "circuit"
    ]


@router.get("/connections/{connection_id}/circuit/history")
def circuit_history(
    connection_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    integrations.get_connection(db, context, connection_id)
    rows, total = page(
        db,
        CircuitTransition,
        context.tenant.id,
        offset,
        limit,
        [CircuitTransition.connection_id == connection_id],
    )
    return {
        "items": [
            {
                k: getattr(row, k)
                for k in (
                    "id",
                    "from_state",
                    "to_state",
                    "reason",
                    "correlation_id",
                    "operation_id",
                    "created_at",
                )
            }
            for row in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/connections/{connection_id}/credentials")
def credential_metadata(
    connection_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    integrations.get_connection(db, context, connection_id)
    rows, total = page(
        db,
        ConnectionCredential,
        context.tenant.id,
        offset,
        limit,
        [ConnectionCredential.connection_id == connection_id],
    )
    return {
        "items": [credentials.metadata(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post(
    "/connections/{connection_id}/credentials",
    dependencies=mutations,
    response_model=CredentialMetadata,
    status_code=201,
)
def submit(
    connection_id: uuid.UUID, data: CredentialSubmission, context: Manage, db: DB, request: Request
):
    try:
        return credentials.metadata(
            credentials.submit(db, context, request.app.state.settings, connection_id, data)
        )
    except ReliabilityError as error:
        return error_response(error, correlation(request))


@router.post(
    "/connections/{connection_id}/credentials/{credential_id}/rotate",
    dependencies=mutations,
    response_model=CredentialMetadata,
    status_code=201,
)
def rotate(
    connection_id: uuid.UUID,
    credential_id: uuid.UUID,
    data: CredentialReplacement,
    context: Manage,
    db: DB,
    request: Request,
):
    row = credentials.get(db, context, connection_id, credential_id)
    if row.credential_type.startswith("OAUTH_") and data.expires_at is None:
        from fastapi import HTTPException

        raise HTTPException(422, "OAuth-style rotation requires an aware expiration")
    submission = CredentialSubmission(
        credential_type=row.credential_type, value=data.value, expires_at=data.expires_at
    )
    try:
        return credentials.metadata(
            credentials.submit(
                db,
                context,
                request.app.state.settings,
                connection_id,
                submission,
                replace_id=credential_id,
            )
        )
    except ReliabilityError as error:
        return error_response(error, correlation(request))


@router.post(
    "/connections/{connection_id}/credentials/{credential_id}/revoke",
    dependencies=mutations,
    response_model=CredentialMetadata,
)
def revoke(connection_id: uuid.UUID, credential_id: uuid.UUID, context: Manage, db: DB) -> dict:
    return credentials.metadata(credentials.revoke(db, context, connection_id, credential_id))


@router.post("/connections/{connection_id}/credentials/refresh", dependencies=mutations)
def refresh(connection_id: uuid.UUID, context: Manage, db: DB, request: Request):
    credentials.manage_lock(db, context)
    connection = integrations.get_connection(db, context, connection_id, True)
    config, _ = reliability.policy(db, connection)
    if config.credential_mode != "OAUTH":
        from fastapi import HTTPException

        raise HTTPException(422, "Enable simulated OAuth enforcement before refresh")
    from app.models.reliability import FailureInjection

    fault = db.scalar(
        select(FailureInjection).where(
            FailureInjection.connection_id == connection.id,
            FailureInjection.tenant_id == context.tenant.id,
        )
    )
    fault = (
        reliability.consume_fault(db, connection)
        if fault
        and fault.scenario in {"EXPIRED_ACCESS_TOKEN", "REVOKED_REFRESH_TOKEN", "REFRESH_FAILED"}
        else None
    )
    reliability.apply_token_fault(db, connection, fault)
    try:
        return credentials.refresh(
            db,
            request.app.state.settings,
            connection,
            correlation(request),
            context.identity.user.id,
            force_failure=bool(fault and fault.scenario == "REFRESH_FAILED"),
        )
    except ReliabilityError as error:
        return error_response(error, correlation(request))


@router.get("/connections/{connection_id}/credentials/refresh-history")
def refresh_history(
    connection_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    integrations.get_connection(db, context, connection_id)
    rows, total = page(
        db,
        CredentialRefreshAttempt,
        context.tenant.id,
        offset,
        limit,
        [CredentialRefreshAttempt.connection_id == connection_id],
    )
    return {
        "items": [
            {
                k: getattr(row, k)
                for k in (
                    "id",
                    "outcome",
                    "reason",
                    "access_credential_id",
                    "refresh_credential_id",
                    "correlation_id",
                    "created_at",
                )
            }
            for row in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/connections/{connection_id}/failure-injection")
def faults(connection_id: uuid.UUID, context: Read, db: DB):
    return reliability.inspect(db, integrations.get_connection(db, context, connection_id))[
        "failure_injection"
    ]


@router.put("/connections/{connection_id}/failure-injection", dependencies=mutations)
def configure_fault(connection_id: uuid.UUID, data: FailureConfig, context: Manage, db: DB):
    return reliability.configure_fault(db, context, connection_id, data)


@router.get("/operations/metrics")
def metrics(context: Read, db: DB, request: Request, hours: Hours = 24) -> dict:
    return observability.metrics(db, context.tenant.id, request.app.state.settings, hours)


@router.get("/alerts")
def alerts(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    status: AlertStatus | None = None,
    severity: Severity | None = None,
    category: AlertCategory | None = None,
) -> dict:
    filters = []
    for field, value in [("status", status), ("severity", severity), ("category", category)]:
        if value is not None:
            filters.append(getattr(OperationalAlert, field) == value)
    rows, total = page(db, OperationalAlert, context.tenant.id, offset, limit, filters)
    return {
        "items": [observability.alert_output(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/alerts/{alert_id}")
def alert(alert_id: uuid.UUID, context: Read, db: DB) -> dict:
    return observability.alert_output(observability.get_alert(db, context, alert_id))


@router.get("/alerts/{alert_id}/history")
def alert_history(
    alert_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    observability.get_alert(db, context, alert_id)
    rows, total = page(
        db, AlertEvent, context.tenant.id, offset, limit, [AlertEvent.alert_id == alert_id]
    )
    return {
        "items": [
            {
                k: getattr(row, k)
                for k in ("id", "event_type", "actor_id", "correlation_id", "created_at")
            }
            for row in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/alerts/{alert_id}/acknowledge", dependencies=mutations)
def acknowledge(alert_id: uuid.UUID, context: AlertManage, db: DB, request: Request) -> dict:
    return observability.alert_output(
        observability.update_alert(db, context, alert_id, "acknowledge", correlation(request))
    )


@router.post("/alerts/{alert_id}/resolve", dependencies=mutations)
def resolve(alert_id: uuid.UUID, context: Manage, db: DB, request: Request) -> dict:
    return observability.alert_output(
        observability.update_alert(db, context, alert_id, "resolve", correlation(request))
    )
