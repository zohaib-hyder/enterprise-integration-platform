import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.identity import IdentityTimestamp


class ConnectorDefinition(Base):
    __tablename__ = "connector_definitions"
    connector_type: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="ck_definition_version"),
        CheckConstraint(
            "connector_type IN ('synthetic_crm', 'synthetic_ats', 'synthetic_slack', 'synthetic_email', 'synthetic_calendar', 'generic_rest')",
            name="ck_definition_type",
        ),
    )


class IntegrationConnection(IdentityTimestamp, Base):
    __tablename__ = "integration_connections"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    connector_type: Mapped[str] = mapped_column(
        ForeignKey("connector_definitions.connector_type", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ENABLED", nullable=False)
    config_schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    public_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    health_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_connection_tenant_name"),
        UniqueConstraint("id", "tenant_id", name="uq_connection_id_tenant"),
        CheckConstraint("status IN ('ENABLED', 'DISABLED')", name="ck_connection_status"),
        CheckConstraint(
            "health_status IN ('UNKNOWN', 'HEALTHY', 'UNAVAILABLE', 'DISABLED')",
            name="ck_connection_health",
        ),
        CheckConstraint("config_schema_version = 1", name="ck_connection_config_version"),
        CheckConstraint(
            "connector_type != 'generic_rest' OR status = 'DISABLED'", name="ck_rest_disabled"
        ),
        Index("ix_connections_tenant_created", "tenant_id", "created_at"),
    )


class SyntheticCRMRecord(IdentityTimestamp, Base):
    __tablename__ = "synthetic_crm_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_crm_connection_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "connection_id", "kind", "external_id", name="uq_crm_external"
        ),
        CheckConstraint("kind IN ('contact', 'requisition')", name="ck_crm_kind"),
        CheckConstraint("schema_version = 1", name="ck_crm_version"),
        Index("ix_crm_tenant_connection_created", "tenant_id", "connection_id", "created_at", "id"),
    )


class SyntheticATSRecord(IdentityTimestamp, Base):
    __tablename__ = "synthetic_ats_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_ats_connection_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "connection_id", "kind", "external_id", name="uq_ats_external"
        ),
        CheckConstraint("kind IN ('candidate', 'requisition')", name="ck_ats_kind"),
        CheckConstraint("schema_version = 1", name="ck_ats_version"),
        Index("ix_ats_tenant_connection_created", "tenant_id", "connection_id", "created_at", "id"),
    )


class SyntheticNotification(IdentityTimestamp, Base):
    __tablename__ = "synthetic_notifications"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="SIMULATED", nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_notification_connection_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "connection_id", "idempotency_key", name="uq_notification_idempotency"
        ),
        CheckConstraint(
            "channel IN ('recruiting', 'operations', 'alerts')", name="ck_notification_channel"
        ),
        CheckConstraint("status = 'SIMULATED'", name="ck_notification_status"),
        Index(
            "ix_notification_tenant_connection_created",
            "tenant_id",
            "connection_id",
            "created_at",
            "id",
        ),
    )


class SyntheticEmail(IdentityTimestamp, Base):
    __tablename__ = "synthetic_emails"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(4000), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="SIMULATED", nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_email_connection_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "connection_id", "idempotency_key", name="uq_email_idempotency"
        ),
        CheckConstraint("status = 'SIMULATED'", name="ck_email_status"),
        Index(
            "ix_email_tenant_connection_created", "tenant_id", "connection_id", "created_at", "id"
        ),
    )


class SyntheticCalendarEvent(IdentityTimestamp, Base):
    __tablename__ = "synthetic_calendar_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    calendar: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="BOOKED", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", "connection_id", name="uq_calendar_event_ownership"),
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_calendar_connection_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "connection_id", "idempotency_key", name="uq_calendar_idempotency"
        ),
        CheckConstraint("ends_at > starts_at", name="ck_calendar_interval"),
        CheckConstraint("status IN ('BOOKED', 'CANCELLED')", name="ck_calendar_status"),
        CheckConstraint("calendar IN ('interviews', 'recruiting')", name="ck_calendar_name"),
        Index(
            "ix_calendar_tenant_connection_interval",
            "tenant_id",
            "connection_id",
            "calendar",
            "starts_at",
            "ends_at",
        ),
    )
