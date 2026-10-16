"""Durable tenant-scoped workflow plans, execution checkpoints and fenced leases."""

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


class WorkflowDefinition(IdentityTimestamp, Base):
    __tablename__ = "workflow_definitions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="INACTIVE")
    active_version: Mapped[int] = mapped_column(Integer, default=1)
    trigger_configuration: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_workflow_name"),
        UniqueConstraint("id", "tenant_id", name="uq_workflow_tenant"),
        ForeignKeyConstraint(
            ["id", "active_version"],
            ["workflow_versions.workflow_id", "workflow_versions.version"],
            name="fk_workflow_active_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_workflow_status"),
        Index("ix_workflow_tenant_status", "tenant_id", "status", "created_at"),
    )


class WorkflowVersion(IdentityTimestamp, Base):
    __tablename__ = "workflow_versions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JSONB)
    definition_digest: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_id", "tenant_id"],
            ["workflow_definitions.id", "workflow_definitions.tenant_id"],
            ondelete="RESTRICT",
            name="fk_workflow_version_tenant",
        ),
        UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),
        UniqueConstraint("id", "tenant_id", "workflow_id", name="uq_workflow_version_ownership"),
        CheckConstraint("version >= 1 AND schema_version = 1", name="ck_workflow_version"),
    )


class WorkflowTrigger(IdentityTimestamp, Base):
    __tablename__ = "workflow_triggers"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(String(40))
    event_types: Mapped[list] = mapped_column(JSONB)
    event_id_prefix: Mapped[str | None] = mapped_column(String(60))
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "tenant_id", "workflow_id"],
            [
                "workflow_versions.id",
                "workflow_versions.tenant_id",
                "workflow_versions.workflow_id",
            ],
            name="fk_trigger_version_ownership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            name="fk_trigger_connection_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("source IN ('synthetic_crm', 'synthetic_ats')", name="ck_trigger_source"),
        Index("ix_trigger_tenant_source", "tenant_id", "connection_id", "source"),
    )


class WorkerLease(IdentityTimestamp, Base):
    # Global infrastructure heartbeat; tenant data is never stored in this record.
    __tablename__ = "worker_leases"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identity: Mapped[str] = mapped_column(String(70))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_worker_heartbeat", "heartbeat_at"),)


class WorkflowExecution(IdentityTimestamp, Base):
    __tablename__ = "workflow_executions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    root_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    replay_of_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default="PENDING")
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_category: Mapped[str | None] = mapped_column(String(40))
    lease_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("worker_leases.id", ondelete="RESTRICT")
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "tenant_id", "workflow_id"],
            [
                "workflow_versions.id",
                "workflow_versions.tenant_id",
                "workflow_versions.workflow_id",
            ],
            name="fk_execution_version_ownership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_id", "tenant_id", "connection_id"],
            ["incoming_events.id", "incoming_events.tenant_id", "incoming_events.connection_id"],
            name="fk_execution_event_ownership",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_execution_tenant"),
        ForeignKeyConstraint(
            ["root_id", "tenant_id"],
            ["workflow_executions.id", "workflow_executions.tenant_id"],
            name="fk_execution_root_tenant",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["replay_of_id", "tenant_id"],
            ["workflow_executions.id", "workflow_executions.tenant_id"],
            name="fk_execution_replay_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "event_id",
            "workflow_id",
            "version_id",
            "generation",
            name="uq_event_workflow_execution",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'WAITING', 'RETRY_SCHEDULED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED')",
            name="ck_execution_state",
        ),
        CheckConstraint("cursor >= 0 AND generation >= 0", name="ck_execution_position"),
        Index("ix_execution_due", "state", "next_run_at", "lease_expires_at"),
        Index("ix_execution_tenant_created", "tenant_id", "created_at", "id"),
    )


class WorkflowStepExecution(IdentityTimestamp, Base):
    __tablename__ = "workflow_step_executions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step_id: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict | None] = mapped_column(JSONB)
    error_category: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["execution_id", "tenant_id"],
            ["workflow_executions.id", "workflow_executions.tenant_id"],
            name="fk_step_execution_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("execution_id", "position", name="uq_step_position"),
        UniqueConstraint("execution_id", "step_id", name="uq_execution_step"),
        UniqueConstraint("id", "tenant_id", "execution_id", name="uq_step_ownership"),
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'SKIPPED', 'RETRY_SCHEDULED', 'FAILED', 'CANCELLED')",
            name="ck_step_state",
        ),
        CheckConstraint("attempt_count >= 0 AND position >= 0", name="ck_step_attempt"),
    )


class WorkflowExecutionEvent(IdentityTimestamp, Base):
    __tablename__ = "workflow_execution_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step_id: Mapped[str | None] = mapped_column(String(40))
    event_type: Mapped[str] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        ForeignKeyConstraint(
            ["execution_id", "tenant_id"],
            ["workflow_executions.id", "workflow_executions.tenant_id"],
            name="fk_history_execution_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_workflow_history", "tenant_id", "execution_id", "created_at", "id"),
    )


class RetrySchedule(IdentityTimestamp, Base):
    __tablename__ = "retry_schedules"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step_execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attempt: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="SCHEDULED")
    category: Mapped[str] = mapped_column(String(40))
    __table_args__ = (
        ForeignKeyConstraint(
            ["step_execution_id", "tenant_id", "execution_id"],
            [
                "workflow_step_executions.id",
                "workflow_step_executions.tenant_id",
                "workflow_step_executions.execution_id",
            ],
            name="fk_retry_step_ownership",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("step_execution_id", "attempt", name="uq_step_retry"),
        CheckConstraint(
            "state IN ('SCHEDULED', 'CONSUMED', 'CANCELLED') AND attempt >= 1",
            name="ck_retry_state",
        ),
        Index("ix_retry_tenant_due", "tenant_id", "state", "available_at"),
    )


class DeadLetterRecord(IdentityTimestamp, Base):
    __tablename__ = "dead_letter_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step_id: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(40))
    attempt_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(
            ["execution_id", "tenant_id"],
            ["workflow_executions.id", "workflow_executions.tenant_id"],
            name="fk_dead_execution_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_id", "tenant_id", "connection_id"],
            ["incoming_events.id", "incoming_events.tenant_id", "incoming_events.connection_id"],
            name="fk_dead_event_ownership",
            ondelete="RESTRICT",
        ),
        Index("ix_dead_tenant_created", "tenant_id", "created_at", "id"),
    )


class WorkflowActionReceipt(IdentityTimestamp, Base):
    __tablename__ = "workflow_action_receipts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    root_execution_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step_id: Mapped[str] = mapped_column(String(40))
    request_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="APPLIED")
    result: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["root_execution_id", "tenant_id"],
            ["workflow_executions.id", "workflow_executions.tenant_id"],
            name="fk_action_root_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["integration_connections.id", "integration_connections.tenant_id"],
            name="fk_action_connection_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "root_execution_id", "step_id", name="uq_workflow_action"),
        CheckConstraint("state IN ('APPLIED', 'UNKNOWN')", name="ck_action_state"),
    )


class WorkflowDispatchRecord(IdentityTimestamp, Base):
    __tablename__ = "workflow_dispatch_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(16))
    execution_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "tenant_id", "connection_id"],
            ["incoming_events.id", "incoming_events.tenant_id", "incoming_events.connection_id"],
            name="fk_dispatch_event_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('DISPATCHED', 'NO_MATCH') AND execution_count >= 0", name="ck_dispatch_state"
        ),
        Index("ix_dispatch_tenant_state", "tenant_id", "state"),
    )
