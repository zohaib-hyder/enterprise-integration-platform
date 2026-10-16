import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, assert_permission, authorized_context
from app.connectors.contracts import (
    ConnectorError,
    ConnectorHealth,
    ConnectorOperation,
    ConnectorResult,
    ConnectorType,
    ErrorCategory,
)
from app.connectors.registry import registry
from app.connectors.simulators import event_output
from app.core.security import utcnow
from app.models.integration import (
    IntegrationConnection,
    SyntheticATSRecord,
    SyntheticCalendarEvent,
    SyntheticCRMRecord,
    SyntheticEmail,
    SyntheticNotification,
)
from app.models.tenant import Tenant
from app.schemas.integration import (
    ConnectionOutput,
    CreateConnection,
    EmailOutput,
    InspectionPage,
    NotificationOutput,
    UpdateConnection,
)
from app.services.audit import record


def connection_output(row: IntegrationConnection) -> ConnectionOutput:
    config = registry.resolve(row.connector_type).validate_config(row.public_config)
    return ConnectionOutput(
        id=row.id,
        connector_type=row.connector_type,
        name=row.name,
        status=row.status,
        config_schema_version=row.config_schema_version,
        public_config=config.model_dump(),
        health_status=row.health_status,
        last_checked_at=row.last_checked_at,
        synthetic=row.connector_type != ConnectorType.REST,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def get_connection(
    db: Session, context: TenantContext, connection_id: uuid.UUID, lock: bool = False
) -> IntegrationConnection:
    query = select(IntegrationConnection).where(
        IntegrationConnection.id == connection_id,
        IntegrationConnection.tenant_id == context.tenant.id,
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise ConnectorError(ErrorCategory.NOT_FOUND)
    return row


def authorize_management(db: Session, context: TenantContext) -> None:
    db.refresh(context.membership)
    fresh = authorized_context(db, context.identity, context.tenant.id)
    assert_permission(fresh, "integrations:manage")


def create_connection(
    db: Session, context: TenantContext, data: CreateConnection
) -> IntegrationConnection:
    # Serialize tenant name checks and revalidate role after acquiring the tenant lock.
    db.scalar(select(Tenant).where(Tenant.id == context.tenant.id).with_for_update())
    db.expire(context.membership)
    authorize_management(db, context)
    adapter = registry.resolve(data.connector_type)
    config = adapter.validate_config(data.config)
    if data.connector_type == ConnectorType.REST and data.enabled:
        raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION)
    if db.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.tenant_id == context.tenant.id,
            IntegrationConnection.name == data.name,
        )
    ):
        raise ConnectorError(ErrorCategory.CONFLICT)
    row = IntegrationConnection(
        tenant_id=context.tenant.id,
        connector_type=data.connector_type.value,
        name=data.name,
        public_config=config.model_dump(mode="json"),
        status="ENABLED" if data.enabled else "DISABLED",
    )
    db.add(row)
    db.flush()
    record(
        db, "connection.created", "connection", row.id, context.tenant.id, context.identity.user.id
    )
    return row


def update_connection(
    db: Session, context: TenantContext, connection_id: uuid.UUID, data: UpdateConnection
) -> IntegrationConnection:
    db.scalar(select(Tenant).where(Tenant.id == context.tenant.id).with_for_update())
    db.expire(context.membership)
    authorize_management(db, context)
    row = get_connection(db, context, connection_id, lock=True)
    if data.enabled and row.connector_type == ConnectorType.REST:
        raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION)
    if data.name is not None:
        if db.scalar(
            select(IntegrationConnection.id).where(
                IntegrationConnection.tenant_id == context.tenant.id,
                IntegrationConnection.name == data.name,
                IntegrationConnection.id != row.id,
            )
        ):
            raise ConnectorError(ErrorCategory.CONFLICT)
        row.name = data.name
    if data.config is not None:
        row.public_config = (
            registry.resolve(row.connector_type)
            .validate_config(data.config)
            .model_dump(mode="json")
        )
        row.health_status, row.last_checked_at = "UNKNOWN", None
    if data.enabled is not None:
        row.status = "ENABLED" if data.enabled else "DISABLED"
        row.health_status, row.last_checked_at = "UNKNOWN" if data.enabled else "DISABLED", None
    db.flush()
    record(
        db, "connection.updated", "connection", row.id, context.tenant.id, context.identity.user.id
    )
    return row


def test_health(db: Session, context: TenantContext, connection_id: uuid.UUID) -> ConnectorHealth:
    authorize_management(db, context)
    row = get_connection(db, context, connection_id, lock=True)
    adapter = registry.resolve(row.connector_type)
    health = (
        adapter.health_check(adapter.validate_config(row.public_config))
        if row.status == "ENABLED"
        else ConnectorHealth(
            status="DISABLED",
            checked_at=utcnow(),
            synthetic=row.connector_type != ConnectorType.REST,
            message="Connection disabled; no provider calls",
        )
    )
    from app.core.config import Settings
    from app.services import credentials, reliability

    policy, _ = reliability.policy(db, row)
    snapshot = reliability.inspect(db, row)
    credential_type = (
        "OAUTH_ACCESS_TOKEN" if policy.credential_mode == "OAUTH" else policy.credential_mode
    )
    credential = (
        credentials.latest(db, row, credential_type)
        if policy.credential_mode != "OPTIONAL"
        else None
    )
    credential_invalid = policy.credential_mode != "OPTIONAL" and (
        credential is None or credentials.metadata(credential)["status"] != "ACTIVE"
    )
    if credential is not None and not credential_invalid:
        from app.connectors.reliability import ReliabilityError

        try:
            credentials.decrypt(db.info.get("settings") or Settings(), credential)
        except ReliabilityError:
            credential_invalid = True
    if row.status == "ENABLED" and (snapshot["circuit"]["state"] != "CLOSED" or credential_invalid):
        health = ConnectorHealth(
            status="UNAVAILABLE",
            checked_at=utcnow(),
            message="Circuit or credential controls require attention",
        )
    row.health_status, row.last_checked_at = health.status, health.checked_at
    record(
        db,
        "connection.health_checked",
        "connection",
        row.id,
        context.tenant.id,
        context.identity.user.id,
    )
    return health


def execute(
    db: Session,
    context: TenantContext,
    connection_id: uuid.UUID,
    operation: ConnectorOperation,
    *,
    propagate_database_errors: bool = False,
    settings=None,
    correlation_id: uuid.UUID | None = None,
    workflow_execution_id: uuid.UUID | None = None,
    attempt: int = 1,
    deadline_seconds: int | None = None,
) -> ConnectorResult:
    # Compatibility flag is retained; the shared layer now classifies SQL failures safely.
    from app.services.reliability import execute_once

    return execute_once(
        db,
        context,
        connection_id,
        operation,
        settings=settings,
        correlation_id=correlation_id,
        workflow_execution_id=workflow_execution_id,
        attempt=attempt,
        deadline_seconds=deadline_seconds,
    )


def inspect_records(
    db: Session, context: TenantContext, connection_id: uuid.UUID, offset: int, limit: int
) -> InspectionPage:
    authorize_management(db, context)
    row = get_connection(db, context, connection_id)
    model = {
        ConnectorType.CRM: SyntheticCRMRecord,
        ConnectorType.ATS: SyntheticATSRecord,
        ConnectorType.SLACK: SyntheticNotification,
        ConnectorType.EMAIL: SyntheticEmail,
        ConnectorType.CALENDAR: SyntheticCalendarEvent,
    }.get(ConnectorType(row.connector_type))
    if model is None:
        raise ConnectorError(ErrorCategory.UNSUPPORTED_OPERATION)
    scope = [model.tenant_id == context.tenant.id, model.connection_id == row.id]
    total = db.scalar(select(func.count()).select_from(model).where(*scope))
    rows = db.scalars(
        select(model).where(*scope).order_by(model.created_at, model.id).offset(offset).limit(limit)
    )
    items = []
    for item in rows:
        if row.connector_type in {ConnectorType.CRM, ConnectorType.ATS}:
            items.append(registry.resolve(row.connector_type).output(item))
        elif row.connector_type == ConnectorType.CALENDAR:
            items.append(event_output(item))
        elif row.connector_type == ConnectorType.SLACK:
            items.append(
                NotificationOutput(
                    id=item.id,
                    channel=item.channel,
                    text=item.text,
                    status=item.status,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
        else:
            items.append(
                EmailOutput(
                    id=item.id,
                    recipient=item.recipient,
                    subject=item.subject,
                    body=item.body,
                    status=item.status,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
    return InspectionPage(items=items, total=total, offset=offset, limit=limit)
