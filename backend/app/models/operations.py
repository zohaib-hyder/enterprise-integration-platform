"""Cross-domain operations metadata. Business state stays in its owning domain."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class Incident(SyncRow, Base):
    __tablename__ = "operation_incidents"
    category: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    problem_key: Mapped[str] = mapped_column(String(64))
    target_kind: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolution_notes: Mapped[str] = mapped_column(String(500), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        *ownership(__tablename__),
        UniqueConstraint("tenant_id", "problem_key", name="uq_incident_problem"),
        CheckConstraint(
            "status IN ('OPEN','INVESTIGATING','MITIGATED','RESOLVED','REOPENED') AND severity IN ('INFO','WARNING','CRITICAL') AND revision >= 1 AND occurrence_count >= 0",
            name="ck_incident_contract",
        ),
        Index("ix_incident_queue", "tenant_id", "status", "severity", "last_seen_at"),
    )


class IncidentEvidence(SyncRow, Base):
    __tablename__ = "operation_incident_evidence"
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_kind: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    occurrence_key: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    __table_args__ = (
        *ownership(__tablename__, {"incident_id": "operation_incidents"}),
        UniqueConstraint("tenant_id", "occurrence_key", name="uq_incident_occurrence"),
    )


class OperationsActivity(SyncRow, Base):
    __tablename__ = "operation_activities"
    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    target_kind: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(48))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(String(500))
    outcome: Mapped[str] = mapped_column(String(40))
    __table_args__ = ownership(__tablename__, {"incident_id": "operation_incidents"})


class RecoveryRequest(SyncRow, Base):
    __tablename__ = "operation_recovery_requests"
    request_key: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    target_kind: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(40))
    evidence_revision: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(500))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    requires_approval: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(24), default="PREVIEWED")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        *ownership(__tablename__, {"incident_id": "operation_incidents"}),
        UniqueConstraint("tenant_id", "request_key", name="uq_recovery_request_key"),
        CheckConstraint(
            "status IN ('PREVIEWED','AWAITING_APPROVAL','EXECUTED','BLOCKED','STALE','EXPIRED','REJECTED','FAILED')",
            name="ck_recovery_status",
        ),
    )


class RecoveryApproval(SyncRow, Base):
    __tablename__ = "operation_recovery_approvals"
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    reason: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_revision: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        *ownership(__tablename__, {"request_id": "operation_recovery_requests"}),
        UniqueConstraint("request_id", name="uq_recovery_approval"),
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED') AND (approved_by IS NULL OR approved_by != requested_by)",
            name="ck_recovery_approval",
        ),
    )


class OperationalReconciliationRun(SyncRow, Base):
    __tablename__ = "operation_reconciliation_runs"
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    window_hours: Mapped[int] = mapped_column(Integer)
    scan_limit: Mapped[int] = mapped_column(Integer)
    summary: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = ownership(__tablename__)


class OperationalFinding(SyncRow, Base):
    __tablename__ = "operation_findings"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    category: Mapped[str] = mapped_column(String(48))
    target_kind: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    classification: Mapped[str] = mapped_column(String(64))
    __table_args__ = ownership(__tablename__, {"run_id": "operation_reconciliation_runs"})


# Tenant + correlation/time indexes support bounded provenance queries without a new log store.
from app.models.canonical import TransformationAttempt
from app.models.notification import NotificationDelivery, NotificationIntent
from app.models.reliability import OperationalAlert
from app.models.synchronization import SyncPlan
from app.models.webhook import EventReceipt, IncomingEvent
from app.models.workflow import WorkflowExecution

for _model in (
    TransformationAttempt,
    NotificationDelivery,
    NotificationIntent,
    OperationalAlert,
    SyncPlan,
    EventReceipt,
    IncomingEvent,
    WorkflowExecution,
    Incident,
    OperationsActivity,
):
    _time = _model.received_at if _model is EventReceipt else _model.created_at
    Index("ix_ops_corr_" + _model.__tablename__, _model.tenant_id, _model.correlation_id, _time)
