"""Tenant-owned webhook credentials and durable inbox; no plaintext secret columns."""

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
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.identity import IdentityTimestamp


class WebhookEndpoint(IdentityTimestamp, Base):
    __tablename__ = "webhook_endpoints"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    public_id: Mapped[str] = mapped_column(String(43), nullable=False, unique=True)
    signature_version: Mapped[str] = mapped_column(String(24), default="hmac-sha256-v1")
    timestamp_tolerance: Mapped[int] = mapped_column(Integer, default=300)
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            name="fk_webhook_connection_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "tenant_id", "connection_id", name="uq_webhook_ownership"),
        CheckConstraint("source IN ('synthetic_crm', 'synthetic_ats')", name="ck_webhook_source"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_webhook_status"),
        CheckConstraint("signature_version = 'hmac-sha256-v1'", name="ck_webhook_signature"),
        CheckConstraint("timestamp_tolerance BETWEEN 30 AND 300", name="ck_webhook_tolerance"),
        Index("ix_webhook_tenant_created", "tenant_id", "created_at", "id"),
    )


class WebhookSecretVersion(Base):
    __tablename__ = "webhook_secret_versions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["endpoint_id", "tenant_id", "connection_id"],
            [
                "webhook_endpoints.id",
                "webhook_endpoints.tenant_id",
                "webhook_endpoints.connection_id",
            ],
            name="fk_secret_webhook_ownership",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("endpoint_id", "version", name="uq_webhook_secret_version"),
        CheckConstraint("version > 0", name="ck_webhook_secret_version"),
        Index("ix_secret_endpoint_created", "endpoint_id", "created_at"),
    )


class IncomingEvent(Base):
    __tablename__ = "incoming_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_status: Mapped[str] = mapped_column(String(16), default="ACCEPTED", nullable=False)
    processing_status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["endpoint_id", "tenant_id", "connection_id"],
            [
                "webhook_endpoints.id",
                "webhook_endpoints.tenant_id",
                "webhook_endpoints.connection_id",
            ],
            name="fk_event_webhook_ownership",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "endpoint_id", "source", "provider_event_id", name="uq_inbox_identity"
        ),
        UniqueConstraint("id", "tenant_id", "endpoint_id", name="uq_event_receipt_ownership"),
        UniqueConstraint("id", "tenant_id", "connection_id", name="uq_event_connection_ownership"),
        CheckConstraint("schema_version = 1", name="ck_inbox_version"),
        CheckConstraint("source IN ('synthetic_crm', 'synthetic_ats')", name="ck_inbox_source"),
        CheckConstraint(
            "event_type IN ('job.created', 'job.updated', 'candidate.created', 'candidate.updated')",
            name="ck_inbox_type",
        ),
        CheckConstraint("ingestion_status = 'ACCEPTED'", name="ck_inbox_ingestion"),
        CheckConstraint("processing_status = 'PENDING'", name="ck_inbox_processing"),
        Index("ix_inbox_tenant_received", "tenant_id", "received_at", "id"),
        Index("ix_inbox_endpoint_received", "endpoint_id", "received_at"),
        Index("ix_inbox_tenant_source_type", "tenant_id", "source", "event_type"),
    )


class EventReceipt(Base):
    __tablename__ = "event_receipts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(32))
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["endpoint_id", "tenant_id", "connection_id"],
            [
                "webhook_endpoints.id",
                "webhook_endpoints.tenant_id",
                "webhook_endpoints.connection_id",
            ],
            name="fk_receipt_webhook_ownership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_id", "tenant_id", "endpoint_id"],
            ["incoming_events.id", "incoming_events.tenant_id", "incoming_events.endpoint_id"],
            name="fk_receipt_event_ownership",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "outcome IN ('ACCEPTED', 'DUPLICATE', 'CONFLICT', 'REJECTED')",
            name="ck_receipt_outcome",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_receipt_duration"),
        CheckConstraint(
            "error_category IS NULL OR error_category IN ('MALFORMED_JSON', 'INVALID_SCHEMA', 'INVALID_SOURCE', 'CONFLICTING_EVENT')",
            name="ck_receipt_category",
        ),
        Index("ix_receipt_tenant_received", "tenant_id", "received_at", "id"),
        Index("ix_receipt_event_received", "event_id", "received_at"),
        Index("ix_receipt_endpoint_received", "endpoint_id", "received_at"),
    )
