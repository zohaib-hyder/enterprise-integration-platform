"""Tenant-owned outbox and durable action queue for the existing worker."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.synchronization import SyncRow, ownership


class NotificationTemplate(SyncRow, Base):
    __tablename__ = "notification_templates"
    name: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(10))
    active_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        *ownership(__tablename__),
        CheckConstraint("channel IN ('SLACK','EMAIL')", name="ck_template_channel"),
    )


class NotificationTemplateVersion(SyncRow, Base):
    __tablename__ = "notification_template_versions"
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(4000))
    __table_args__ = (
        *ownership(__tablename__, {"template_id": "notification_templates"}),
        UniqueConstraint("template_id", "version", name="uq_notification_template_version"),
    )


class NotificationRule(SyncRow, Base):
    __tablename__ = "notification_rules"
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    active_version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = ownership(__tablename__)


class NotificationRuleVersion(SyncRow, Base):
    __tablename__ = "notification_rule_versions"
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(30))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    destination: Mapped[str] = mapped_column(String(254))
    sync_configuration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    __table_args__ = (
        *ownership(
            __tablename__,
            {
                "rule_id": "notification_rules",
                "connection_id": "integration_connections",
                "template_version_id": "notification_template_versions",
                "sync_configuration_id": "sync_configurations",
            },
        ),
        UniqueConstraint("rule_id", "version", name="uq_notification_rule_version"),
        CheckConstraint(
            "trigger IN ('SYNC_CREATE','SYNC_UPDATE','SYNC_CONFLICT','WORKFLOW_FAILURE','MANUAL')",
            name="ck_notification_trigger",
        ),
    )


class NotificationIntent(SyncRow, Base):
    __tablename__ = "notification_intents"
    rule_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sync_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    channel: Mapped[str] = mapped_column(String(10))
    destination: Mapped[str] = mapped_column(String(254))
    rendering_inputs: Mapped[dict] = mapped_column(JSONB)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    __table_args__ = (
        *ownership(
            __tablename__,
            {
                "rule_version_id": "notification_rule_versions",
                "template_version_id": "notification_template_versions",
                "workflow_execution_id": "workflow_executions",
                "sync_execution_id": "sync_executions",
            },
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_notification_intent_key"),
    )


class NotificationDelivery(SyncRow, Base):
    __tablename__ = "notification_deliveries"
    intent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    channel: Mapped[str] = mapped_column(String(10))
    operation: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSONB)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_policy: Mapped[dict] = mapped_column(JSONB)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("worker_leases.id", ondelete="RESTRICT")
    )
    error_category: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        *ownership(
            __tablename__,
            {"intent_id": "notification_intents", "connection_id": "integration_connections"},
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_notification_delivery_key"),
        UniqueConstraint("intent_id", name="uq_delivery_intent"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','RETRY_SCHEDULED','SUCCEEDED','FAILED','CANCELLED','UNKNOWN') AND attempt_count >= 0",
            name="ck_delivery_state",
        ),
        Index("ix_delivery_due", "status", "next_attempt_at", "lease_expires_at"),
    )


class NotificationDeliveryAttempt(SyncRow, Base):
    __tablename__ = "notification_delivery_attempts"
    delivery_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    error_category: Mapped[str | None] = mapped_column(String(40))
    retry_after: Mapped[int | None] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        *ownership(__tablename__, {"delivery_id": "notification_deliveries"}),
        UniqueConstraint("delivery_id", "number", name="uq_delivery_attempt_number"),
    )


class NotificationReceipt(SyncRow, Base):
    __tablename__ = "notification_receipts"
    delivery_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    provider_reference: Mapped[str] = mapped_column(String(100))
    result: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        *ownership(__tablename__, {"delivery_id": "notification_deliveries"}),
        UniqueConstraint("delivery_id", name="uq_notification_receipt_delivery"),
    )


class CalendarCoordinationRequest(SyncRow, Base):
    __tablename__ = "calendar_coordination_requests"
    delivery_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(20))
    expected_event_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    participants: Mapped[list] = mapped_column(JSONB)
    request_key: Mapped[uuid.UUID] = mapped_column(Uuid)
    request_digest: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "tenant_id", "connection_id"],
            [
                "synthetic_calendar_events.id",
                "synthetic_calendar_events.tenant_id",
                "synthetic_calendar_events.connection_id",
            ],
            ondelete="RESTRICT",
            name="fk_calendar_request_event_owner",
        ),
        *ownership(
            __tablename__,
            {"delivery_id": "notification_deliveries", "connection_id": "integration_connections"},
        ),
        UniqueConstraint("tenant_id", "request_key", name="uq_calendar_request_key"),
    )


class CalendarCoordinationAttempt(SyncRow, Base):
    __tablename__ = "calendar_coordination_attempts"
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    delivery_attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(24))
    __table_args__ = (
        *ownership(
            __tablename__,
            {
                "request_id": "calendar_coordination_requests",
                "delivery_attempt_id": "notification_delivery_attempts",
            },
        ),
        UniqueConstraint("delivery_attempt_id", name="uq_calendar_attempt_delivery"),
    )
