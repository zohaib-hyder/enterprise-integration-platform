"""Canonical state, immutable mapping evidence and tenant-local source identities."""

import uuid
from datetime import datetime

from sqlalchemy import (
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
from app.models.identity import IdentityTimestamp

ENTITY_CHECK = "entity_type IN ('client', 'contact', 'job_requisition', 'candidate', 'application', 'activity')"
KIND_CHECK = "source_kind IN ('contact', 'candidate', 'requisition')"


class CanonicalRecord(IdentityTimestamp, Base):
    __tablename__ = "canonical_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    source_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_external_id: Mapped[str] = mapped_column(String(100))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entity_type: Mapped[str] = mapped_column(String(24))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    normalized_data: Mapped[dict] = mapped_column(JSONB)
    field_provenance: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    normalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_canonical_connection_tenant",
        ),
        UniqueConstraint("id", "tenant_id", "entity_type", name="uq_canonical_ownership"),
        CheckConstraint(ENTITY_CHECK, name="ck_canonical_entity"),
        CheckConstraint("schema_version = 1 AND revision >= 1", name="ck_canonical_version"),
        Index("ix_canonical_tenant_entity_updated", "tenant_id", "entity_type", "updated_at", "id"),
    )


class TransformationMapping(IdentityTimestamp, Base):
    __tablename__ = "transformation_mappings"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_kind: Mapped[str] = mapped_column(String(24))
    entity_type: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(100))
    active_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_mapping_connection_tenant",
        ),
        UniqueConstraint("tenant_id", "connection_id", "source_kind", name="uq_mapping_source"),
        ForeignKeyConstraint(
            ["id", "active_version"],
            [
                "transformation_mapping_versions.mapping_id",
                "transformation_mapping_versions.version",
            ],
            name="fk_mapping_active_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("id", "tenant_id", "connection_id", name="uq_mapping_ownership"),
        CheckConstraint(KIND_CHECK, name="ck_mapping_kind"),
        CheckConstraint(ENTITY_CHECK, name="ck_mapping_entity"),
        CheckConstraint("active_version >= 1", name="ck_mapping_active"),
        Index("ix_mapping_tenant_created", "tenant_id", "created_at", "id"),
    )


class TransformationMappingVersion(IdentityTimestamp, Base):
    __tablename__ = "transformation_mapping_versions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    mapping_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JSONB)
    definition_digest: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        ForeignKeyConstraint(
            ["mapping_id", "tenant_id", "connection_id"],
            [
                "transformation_mappings.id",
                "transformation_mappings.tenant_id",
                "transformation_mappings.connection_id",
            ],
            ondelete="RESTRICT",
            name="fk_version_mapping_ownership",
        ),
        UniqueConstraint("mapping_id", "version", name="uq_mapping_version"),
        UniqueConstraint("id", "tenant_id", name="uq_mapping_version_tenant"),
        UniqueConstraint("id", "tenant_id", "connection_id", name="uq_version_ownership"),
        CheckConstraint("version >= 1 AND schema_version = 1", name="ck_mapping_version"),
        Index("ix_version_tenant_mapping", "tenant_id", "mapping_id", "version"),
    )


class ExternalRecordMapping(IdentityTimestamp, Base):
    __tablename__ = "external_record_mappings"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_kind: Mapped[str] = mapped_column(String(24))
    external_id: Mapped[str] = mapped_column(String(100))
    canonical_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    entity_type: Mapped[str] = mapped_column(String(24))
    last_source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_payload_digest: Mapped[str | None] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(String(24), default="NORMALIZATION")
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            ondelete="RESTRICT",
            name="fk_external_connection_tenant",
        ),
        ForeignKeyConstraint(
            ["canonical_id", "tenant_id", "entity_type"],
            [
                "canonical_records.id",
                "canonical_records.tenant_id",
                "canonical_records.entity_type",
            ],
            ondelete="RESTRICT",
            name="fk_external_canonical_ownership",
        ),
        UniqueConstraint(
            "tenant_id", "connection_id", "source_kind", "external_id", name="uq_external_identity"
        ),
        CheckConstraint(KIND_CHECK, name="ck_external_kind"),
        CheckConstraint(
            "provenance IN ('NORMALIZATION', 'MANUAL_LINK')", name="ck_external_provenance"
        ),
        Index("ix_external_tenant_canonical", "tenant_id", "canonical_id"),
    )


class TransformationAttempt(IdentityTimestamp, Base):
    __tablename__ = "transformation_attempts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    mapping_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    canonical_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    entity_type: Mapped[str] = mapped_column(String(24))
    source_kind: Mapped[str] = mapped_column(String(24))
    external_id: Mapped[str] = mapped_column(String(100))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24))
    issues: Mapped[list] = mapped_column(JSONB, default=list)
    source_snapshot: Mapped[dict] = mapped_column(JSONB)
    result_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    __table_args__ = (
        ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id", "connection_id"],
            [
                "transformation_mapping_versions.id",
                "transformation_mapping_versions.tenant_id",
                "transformation_mapping_versions.connection_id",
            ],
            ondelete="RESTRICT",
            name="fk_attempt_version_ownership",
        ),
        ForeignKeyConstraint(
            ["canonical_id", "tenant_id", "entity_type"],
            [
                "canonical_records.id",
                "canonical_records.tenant_id",
                "canonical_records.entity_type",
            ],
            ondelete="RESTRICT",
            name="fk_attempt_canonical_ownership",
        ),
        # Inbox ownership already binds each event's connection/tenant to its endpoint.
        ForeignKeyConstraint(
            ["event_id", "tenant_id", "connection_id"],
            ["incoming_events.id", "incoming_events.tenant_id", "incoming_events.connection_id"],
            ondelete="RESTRICT",
            name="fk_attempt_event_ownership",
        ),
        UniqueConstraint("tenant_id", "fingerprint", name="uq_attempt_fingerprint"),
        UniqueConstraint("id", "tenant_id", "connection_id", name="uq_attempt_ownership"),
        CheckConstraint(
            "outcome IN ('SUCCEEDED', 'NO_CHANGE', 'QUARANTINED', 'CONFLICT', 'MANUAL_REVIEW')",
            name="ck_attempt_outcome",
        ),
        Index("ix_attempt_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_attempt_tenant_event", "tenant_id", "event_id"),
    )


class QuarantinedRecord(IdentityTimestamp, Base):
    __tablename__ = "quarantined_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    resolution_attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "tenant_id", "connection_id"],
            [
                "transformation_attempts.id",
                "transformation_attempts.tenant_id",
                "transformation_attempts.connection_id",
            ],
            ondelete="RESTRICT",
            name="fk_quarantine_attempt_ownership",
        ),
        ForeignKeyConstraint(
            ["resolution_attempt_id", "tenant_id", "connection_id"],
            [
                "transformation_attempts.id",
                "transformation_attempts.tenant_id",
                "transformation_attempts.connection_id",
            ],
            ondelete="RESTRICT",
            name="fk_quarantine_resolution_ownership",
        ),
        UniqueConstraint("attempt_id", name="uq_quarantine_attempt"),
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="ck_quarantine_status"),
        Index("ix_quarantine_tenant_status", "tenant_id", "status", "created_at"),
    )


class FieldAuthorityRule(IdentityTimestamp, Base):
    __tablename__ = "field_authority_rules"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    entity_type: Mapped[str] = mapped_column(String(24))
    field: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(40))
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_type", "field", name="uq_authority_field"),
        CheckConstraint(ENTITY_CHECK, name="ck_authority_entity"),
        CheckConstraint(
            "owner IN ('synthetic_crm', 'synthetic_ats', 'synthetic_calendar', 'ORIGIN', 'REVIEW')",
            name="ck_authority_owner",
        ),
        Index("ix_authority_tenant_entity", "tenant_id", "entity_type"),
    )
