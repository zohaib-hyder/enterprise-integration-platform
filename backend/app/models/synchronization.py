"""Durable synchronization evidence; all references carry tenant ownership."""

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
from app.models.identity import IdentityTimestamp


class SyncRow(IdentityTimestamp):
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))


def ownership(table: str, refs: dict[str, str] | None = None):
    return (
        UniqueConstraint("id", "tenant_id", name=f"uq_{table}_tenant"),
        Index(f"ix_{table}_tenant_created", "tenant_id", "created_at", "id"),
        *(
            ForeignKeyConstraint(
                [field, "tenant_id"],
                [f"{target}.id", f"{target}.tenant_id"],
                ondelete="RESTRICT",
                name=f"fk_{table}_{field}",
            )
            for field, target in (refs or {}).items()
        ),
    )


class SyncConfiguration(SyncRow, Base):
    __tablename__ = "sync_configurations"
    name: Mapped[str] = mapped_column(String(100))
    source_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    destination_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        ForeignKeyConstraint(
            ["active_version_id", "tenant_id", "id"],
            [
                "sync_configuration_versions.id",
                "sync_configuration_versions.tenant_id",
                "sync_configuration_versions.configuration_id",
            ],
            name="fk_sync_active_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        *ownership(
            __tablename__,
            {
                "source_connection_id": "integration_connections",
                "destination_connection_id": "integration_connections",
                "workflow_id": "workflow_definitions",
            },
        ),
        UniqueConstraint(
            "tenant_id", "source_connection_id", "destination_connection_id", name="uq_sync_route"
        ),
        CheckConstraint(
            "source_connection_id != destination_connection_id AND revision >= 1",
            name="ck_sync_route",
        ),
    )


class SyncConfigurationVersion(SyncRow, Base):
    __tablename__ = "sync_configuration_versions"
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer)
    mapping_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    definition: Mapped[dict] = mapped_column(JSONB)
    definition_digest: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        *ownership(__tablename__, {"configuration_id": "sync_configurations"}),
        UniqueConstraint("configuration_id", "version", name="uq_sync_version"),
        UniqueConstraint(
            "id", "tenant_id", "configuration_id", name="uq_sync_version_configuration"
        ),
        ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id"],
            ["transformation_mapping_versions.id", "transformation_mapping_versions.tenant_id"],
            name="fk_sync_normalization_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version >= 1", name="ck_sync_version"),
    )


class SyncPlan(SyncRow, Base):
    __tablename__ = "sync_plans"
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    entity_type: Mapped[str] = mapped_column(String(24), default="job_requisition")
    canonical_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    decision: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "tenant_id", "configuration_id"],
            [
                "sync_configuration_versions.id",
                "sync_configuration_versions.tenant_id",
                "sync_configuration_versions.configuration_id",
            ],
            name="fk_sync_plan_configuration_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("entity_type = 'job_requisition'", name="ck_syncplan_entity"),
        ForeignKeyConstraint(
            ["canonical_id", "tenant_id", "entity_type"],
            [
                "canonical_records.id",
                "canonical_records.tenant_id",
                "canonical_records.entity_type",
            ],
            name="fk_syncplan_canonical",
            ondelete="RESTRICT",
        ),
        *ownership(
            __tablename__,
            {
                "configuration_id": "sync_configurations",
                "version_id": "sync_configuration_versions",
                "source_connection_id": "integration_connections",
            },
        ),
        ForeignKeyConstraint(
            ["event_id", "tenant_id", "source_connection_id"],
            ["incoming_events.id", "incoming_events.tenant_id", "incoming_events.connection_id"],
            name="fk_sync_plan_event",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "decision IN ('CREATE','UPDATE','NO_OP','CONFLICT','SKIP','INVALID')",
            name="ck_sync_plan_decision",
        ),
    )


class SyncExecution(SyncRow, Base):
    __tablename__ = "sync_executions"
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    business_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="PLANNED")
    __table_args__ = (
        *ownership(
            __tablename__,
            {
                "configuration_id": "sync_configurations",
                "plan_id": "sync_plans",
                "workflow_execution_id": "workflow_executions",
            },
        ),
        UniqueConstraint("tenant_id", "business_key", name="uq_sync_business_key"),
        CheckConstraint(
            "state IN ('PLANNED','SUCCEEDED','HELD','SKIPPED')", name="ck_sync_execution_state"
        ),
    )


class SyncOperation(SyncRow, Base):
    __tablename__ = "sync_operations"
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    entity_type: Mapped[str] = mapped_column(String(24), default="job_requisition")
    canonical_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    destination_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    action_key: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("entity_type = 'job_requisition'", name="ck_syncoperation_entity"),
        ForeignKeyConstraint(
            ["canonical_id", "tenant_id", "entity_type"],
            [
                "canonical_records.id",
                "canonical_records.tenant_id",
                "canonical_records.entity_type",
            ],
            name="fk_syncoperation_canonical",
            ondelete="RESTRICT",
        ),
        *ownership(
            __tablename__,
            {
                "execution_id": "sync_executions",
                "plan_id": "sync_plans",
                "destination_connection_id": "integration_connections",
            },
        ),
        UniqueConstraint("tenant_id", "action_key", name="uq_sync_action"),
        Index(
            "ix_sync_operation_baseline",
            "tenant_id",
            "canonical_id",
            "destination_connection_id",
            "created_at",
        ),
    )


class SyncConflict(SyncRow, Base):
    __tablename__ = "sync_conflicts"
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    category: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        *ownership(__tablename__, {"plan_id": "sync_plans", "execution_id": "sync_executions"}),
        UniqueConstraint("plan_id", "category", name="uq_sync_conflict_plan"),
        CheckConstraint(
            "status IN ('OPEN','RESOLVED') AND revision >= 1", name="ck_sync_conflict_state"
        ),
    )


class SyncResolution(SyncRow, Base):
    __tablename__ = "sync_resolutions"
    conflict_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(48))
    reason: Mapped[str] = mapped_column(String(500))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    request_key: Mapped[uuid.UUID] = mapped_column(Uuid)
    __table_args__ = (
        *ownership(__tablename__, {"conflict_id": "sync_conflicts", "plan_id": "sync_plans"}),
        UniqueConstraint("tenant_id", "request_key", name="uq_sync_resolution_request"),
    )


class ReconciliationRun(SyncRow, Base):
    __tablename__ = "reconciliation_runs"
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    started_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    offset: Mapped[int] = mapped_column(Integer)
    limit: Mapped[int] = mapped_column(Integer)
    summary: Mapped[dict] = mapped_column(JSONB)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        *ownership(
            __tablename__,
            {
                "configuration_id": "sync_configurations",
                "version_id": "sync_configuration_versions",
            },
        ),
        ForeignKeyConstraint(
            ["version_id", "tenant_id", "configuration_id"],
            [
                "sync_configuration_versions.id",
                "sync_configuration_versions.tenant_id",
                "sync_configuration_versions.configuration_id",
            ],
            name="fk_reconcile_configuration_version",
            ondelete="RESTRICT",
        ),
    )


class ReconciliationFinding(SyncRow, Base):
    __tablename__ = "reconciliation_findings"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    category: Mapped[str] = mapped_column(String(40))
    source_external_id: Mapped[str | None] = mapped_column(String(100))
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = ownership(__tablename__, {"run_id": "reconciliation_runs"})
