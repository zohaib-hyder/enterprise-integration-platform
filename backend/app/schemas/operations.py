"""Strict inputs and safe cross-domain response contracts."""

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.connectors.contracts import Contract

TargetKind = Literal[
    "event",
    "receipt",
    "workflow",
    "connection",
    "sync",
    "conflict",
    "delivery",
    "calendar",
    "alert",
    "incident",
    "finding",
]
Action = Literal[
    "RETRY_NOTIFICATION",
    "REPLAY_WORKFLOW",
    "RESUME_ELIGIBLE_EXECUTION",
    "RECHECK_CONNECTOR",
    "REFRESH_SIMULATED_CREDENTIAL",
    "RECONCILE_SYNC",
    "REVIEW_SYNC_CONFLICT",
    "REVIEW_UNKNOWN_OUTCOME",
    "ACKNOWLEDGE_ALERT",
    "ESCALATE_FOR_MANUAL_REVIEW",
]
IncidentStatus = Literal["OPEN", "INVESTIGATING", "MITIGATED", "RESOLVED", "REOPENED"]
Severity = Literal["INFO", "WARNING", "CRITICAL"]


class Reason(Contract):
    reason: str = Field(min_length=8, max_length=500)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        if re.search(
            r"[\x00-\x1f<>]|[a-z]+://|\bBearer\s+\S+|\bsim_[A-Za-z0-9_-]{12,}|(?:api[_ -]?key|credential|signing[_ -]?secret)\s*[:=]|(?:secret|password|token|authorization|signing[_ -]?key)\s*[:=]",
            value,
            re.IGNORECASE,
        ):
            raise ValueError("Use a safe operational reason without credentials, URLs or payloads")
        if len(value.strip()) < 8:
            raise ValueError("Provide a meaningful operational reason")
        return value.strip()


class IncidentChange(Reason):
    expected_revision: int = Field(ge=1)
    status: IncidentStatus | None = None
    severity: Severity | None = None
    assigned_user_id: uuid.UUID | None = None
    clear_assignment: bool = False
    acknowledge: bool = False


class RecoveryPreview(Contract):
    target_kind: TargetKind
    target_id: uuid.UUID


class RecoveryCreate(RecoveryPreview, Reason):
    action: Action
    evidence_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_key: uuid.UUID
    incident_id: uuid.UUID | None = None


class Confirmation(Contract):
    evidence_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm: Literal[True]


class ApprovalDecision(Reason):
    evidence_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["APPROVED", "REJECTED"]


class ScanInput(Contract):
    hours: Literal[1, 24, 168, 720] = 24
    limit: int = Field(default=100, ge=1, le=100)
    grace_seconds: int = Field(default=120, ge=30, le=3600)


class IncidentOutput(Contract):
    id: uuid.UUID
    category: str
    severity: Severity
    status: IncidentStatus
    target_kind: str
    target_id: uuid.UUID
    correlation_id: uuid.UUID | None
    assigned_user_id: uuid.UUID | None
    resolution_notes: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    revision: int
    created_at: datetime
    updated_at: datetime


class TimelineItem(Contract):
    id: uuid.UUID
    event_type: str
    timestamp: datetime
    status: str
    source_system: str
    entity_reference: uuid.UUID
    correlation_id: uuid.UUID
    summary: str
    detail_link: str
    provenance: Literal["CORRELATION_ID", "DURABLE_REFERENCE"]


class TimelineOutput(Contract):
    correlation_id: uuid.UUID
    items: list[TimelineItem]
    offset: int
    limit: int
    bounded: bool
    unlinked: str


class RecoveryOption(Contract):
    action: Action
    eligible: bool
    required_permission: str
    target_kind: str
    target_id: uuid.UUID
    expected_effect: str
    preconditions: list[str]
    idempotency_protections: list[str]
    risks: list[str]
    blocked_reasons: list[str]
    requires_approval: bool
    evidence_revision: str
    detail_link: str


class RecoveryPlanOutput(Contract):
    target_kind: str
    target_id: uuid.UUID
    correlation_id: uuid.UUID | None
    evidence_revision: str
    observed_symptom: str
    classification: str
    certainty: str
    evidence_references: list[dict[str, str]]
    runbook_id: str
    actions: list[RecoveryOption]


from typing import Generic, TypeVar

from pydantic import JsonValue

T = TypeVar("T")


class OperationsPage(Contract, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int


class ActivityOutput(Contract):
    id: uuid.UUID
    incident_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    target_kind: str
    target_id: uuid.UUID
    action: str
    correlation_id: uuid.UUID | None
    reason: str
    outcome: str
    created_at: datetime


class RecoveryOutput(Contract):
    id: uuid.UUID
    request_key: uuid.UUID
    actor_id: uuid.UUID
    incident_id: uuid.UUID | None
    target_kind: str
    target_id: uuid.UUID
    action: Action
    evidence_revision: str
    reason: str
    correlation_id: uuid.UUID
    requires_approval: bool
    status: str
    expires_at: datetime
    result: dict[str, JsonValue]
    created_at: datetime
    updated_at: datetime


class ApprovalOutput(Contract):
    id: uuid.UUID
    request_id: uuid.UUID
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None
    status: str
    reason: str
    expires_at: datetime
    evidence_revision: str
    created_at: datetime


class FindingOutput(Contract):
    id: uuid.UUID
    run_id: uuid.UUID
    category: str
    target_kind: str
    target_id: uuid.UUID
    correlation_id: uuid.UUID | None
    classification: str
    created_at: datetime


class ScanOutput(Contract):
    id: uuid.UUID
    actor_id: uuid.UUID
    correlation_id: uuid.UUID
    window_hours: int
    scan_limit: int
    summary: dict[str, JsonValue]
    created_at: datetime


class HealthSnapshot(Contract):
    workflow_backlog: int
    expired_workflow_leases: int
    open_sync_conflicts: int
    pending_notification_retries: int
    open_alerts: int
    unresolved_calendar_conflicts: int
    tenant_lease_workers_fresh: int
    tenant_lease_workers_stale: int
    worker_health: Literal["HEALTHY", "STALE", "UNKNOWN"]
    connector_health: dict[str, int]
    connector_sample_limit: int


class TrendPoint(Contract):
    timestamp: datetime
    count: int


class OverviewOutput(Contract):
    checked_at: datetime
    window_hours: int
    window_start: datetime
    webhook_receipts: dict[str, int]
    accepted_events: int
    workflows: dict[str, int]
    deliveries: dict[str, int]
    sync: dict[str, int]
    dead_letters: int
    recovery_outcomes: dict[str, int]
    snapshot: HealthSnapshot
    failure_trend: list[TrendPoint]
    basis: str


class RunbookOutput(Contract):
    id: str
    version: int
    title: str
    symptom: str
    evidence_to_inspect: list[str]
    preconditions: list[str]
    safe_recovery_options: list[str]
    escalation_conditions: list[str]
    verification_steps: list[str]


class AssigneeOutput(Contract):
    id: uuid.UUID
    display_name: str
    role: str


class IncidentRefreshOutput(Contract):
    scanned: dict[str, int]
    bounded: bool
    limit_per_source: int
    hours: int
