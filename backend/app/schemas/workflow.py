"""Strict sequential workflow v1. No expressions, URLs or executable scripts."""

import uuid
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.connectors.contracts import ConnectorResult, Contract, ExternalId
from app.schemas.canonical import CanonicalEnvelope
from app.schemas.synchronization import ApplySyncConfig, SyncCheckpoint, SyncStepConfig

StepType = Literal[
    "NORMALIZE_EVENT",
    "CONDITION",
    "TRANSFORM",
    "DELAY",
    "MOCK_CONNECTOR_ACTION",
    "RECORD_RESULT",
    "PLAN_CRM_ATS_SYNC",
    "APPLY_CRM_ATS_SYNC",
    "RECORD_SYNC_OUTCOME",
]
ExecutionState = Literal[
    "PENDING",
    "RUNNING",
    "WAITING",
    "RETRY_SCHEDULED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "DEAD_LETTERED",
]
StepState = Literal[
    "PENDING", "RUNNING", "SUCCEEDED", "SKIPPED", "RETRY_SCHEDULED", "FAILED", "CANCELLED"
]
STEP_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
EVENT_PATHS = {
    "event_id",
    "event_type",
    "source",
    "external_id",
    "occurred_at",
    "data.title",
    "data.department",
    "data.location",
    "data.status",
    "data.name",
    "data.email",
    "data.stage",
    "data.skills",
}
OUTPUT_PATHS = {
    "PLAN_CRM_ATS_SYNC": {"plan_id", "execution_id", "decision", "state"},
    "APPLY_CRM_ATS_SYNC": {"plan_id", "execution_id", "decision", "state"},
    "RECORD_SYNC_OUTCOME": {"plan_id", "execution_id", "decision", "state"},
    "NORMALIZE_EVENT": {
        "canonical_id",
        "transformation_attempt_id",
        "normalized_data.title",
        "normalized_data.department",
        "normalized_data.location",
        "normalized_data.status",
        "normalized_data.recruiting_status",
        "normalized_data.name",
        "normalized_data.email",
        "normalized_data.stage",
        "normalized_data.skills",
    },
    "TRANSFORM": {
        "normalized_data.title",
        "normalized_data.status",
        "normalized_data.stage",
        "normalized_data.name",
        "normalized_data.email",
    },
    "CONDITION": {"matched"},
    "DELAY": {"due_at"},
    "MOCK_CONNECTOR_ACTION": {
        "data.id",
        "data.status",
        "data.delivery_reference",
        "data.external_id",
        "data.kind",
    },
    "RECORD_RESULT": {"summary", "outcome", "canonical_id"},
}


class Reference(Contract):
    source: Literal["event", "step"]
    step_id: str | None = Field(default=None, pattern=STEP_PATTERN)
    path: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def applicable(self) -> "Reference":
        if (
            self.source == "step"
            and not self.step_id
            or self.source == "event"
            and self.step_id is not None
        ):
            raise ValueError("Invalid reference scope")
        if self.source == "event" and self.path not in EVENT_PATHS:
            raise ValueError("Unsupported event reference")
        return self


class RetryPolicy(Contract):
    max_attempts: int = Field(default=3, ge=1, le=5)
    base_delay_seconds: int = Field(default=1, ge=1, le=30)
    max_delay_seconds: int = Field(default=30, ge=1, le=300)
    jitter_fraction: float = Field(default=0.2, ge=0, le=0.3, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded(self) -> "RetryPolicy":
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("Invalid retry interval")
        return self


class NormalizeConfig(Contract):
    mapping_version_id: uuid.UUID


class ConditionConfig(Contract):
    reference: Reference
    operator: Literal["EQ", "NE", "EXISTS"] = "EQ"
    expected: str | int | bool | None = None
    on_false: Literal["SKIP_REMAINING", "CONTINUE"] = "SKIP_REMAINING"

    @model_validator(mode="after")
    def bound_value(self) -> "ConditionConfig":
        if isinstance(self.expected, str) and len(self.expected) > 160:
            raise ValueError("Condition value too long")
        return self


class DelayConfig(Contract):
    seconds: int = Field(ge=1, le=300)


class FaultInjection(Contract):
    mode: Literal["NONE", "TRANSIENT", "PERMANENT", "TIMEOUT"] = "NONE"
    fail_first_attempts: int = Field(default=1, ge=0, le=4)
    permanent_event_ids: list[ExternalId] = Field(default_factory=list, max_length=3)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=30)


class ActionConfig(Contract):
    connection_id: uuid.UUID
    operation: Literal[
        "message.send",
        "event.create",
        "contact.list",
        "contact.get",
        "candidate.list",
        "candidate.get",
        "requisition.list",
        "requisition.get",
        "record.search",
    ]
    payload: dict = Field(default_factory=dict)
    bindings: dict[str, Reference] = Field(default_factory=dict, max_length=8)
    fault: FaultInjection = Field(default_factory=FaultInjection)

    @model_validator(mode="after")
    def safe_key(self) -> "ActionConfig":
        import json

        if (
            "idempotency_key" in self.payload
            or "idempotency_key" in self.bindings
            or len(json.dumps(self.payload, allow_nan=False)) > 4096
        ):
            raise ValueError("Action configuration exceeds bounds")
        return self


class ResultConfig(Contract):
    summary: str = Field(default="Synthetic workflow completed", max_length=500)
    outcome: Literal["COMPLETED", "OBSERVED"] = "COMPLETED"
    canonical_id: Reference | None = None


CONFIG_MODELS = {
    "PLAN_CRM_ATS_SYNC": SyncStepConfig,
    "APPLY_CRM_ATS_SYNC": ApplySyncConfig,
    "RECORD_SYNC_OUTCOME": ApplySyncConfig,
    "NORMALIZE_EVENT": NormalizeConfig,
    "TRANSFORM": NormalizeConfig,
    "CONDITION": ConditionConfig,
    "DELAY": DelayConfig,
    "MOCK_CONNECTOR_ACTION": ActionConfig,
    "RECORD_RESULT": ResultConfig,
}


class WorkflowStep(Contract):
    id: str = Field(pattern=STEP_PATTERN)
    type: StepType
    config: dict
    output_schema: Literal["v1"] = "v1"
    timeout_seconds: int = Field(default=5, ge=1, le=10)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    failure_behavior: Literal["DEAD_LETTER", "FAIL"] = "DEAD_LETTER"

    @model_validator(mode="after")
    def validate_config(self) -> "WorkflowStep":
        parsed = CONFIG_MODELS[self.type].model_validate(self.config)
        self.config = parsed.model_dump(mode="json")
        return self

    def typed_config(self):
        return CONFIG_MODELS[self.type].model_validate(self.config)


class TriggerConfig(Contract):
    connection_id: uuid.UUID
    source: Literal["synthetic_crm", "synthetic_ats"]
    event_types: list[
        Literal["job.created", "job.updated", "candidate.created", "candidate.updated"]
    ] = Field(min_length=1, max_length=4)
    event_id_prefix: str | None = Field(
        default=None, min_length=1, max_length=60, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$"
    )


class WorkflowJSON(Contract):
    schema_version: Literal[1]
    trigger: TriggerConfig
    steps: list[WorkflowStep] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def sequential(self) -> "WorkflowJSON":
        import json

        if len(json.dumps(self.model_dump(mode="json"))) > 14000:
            raise ValueError("Workflow exceeds bounds")
        previous = {}
        for step in self.steps:
            if step.id in previous:
                raise ValueError("Duplicate step ID")
            config = step.typed_config()
            references = (
                list(config.bindings.values())
                if isinstance(config, ActionConfig)
                else [config.reference]
                if isinstance(config, ConditionConfig)
                else [config.canonical_id]
                if isinstance(config, ResultConfig) and config.canonical_id
                else []
            )
            for ref in references:
                if ref.source == "step" and (
                    ref.step_id not in previous
                    or ref.path not in OUTPUT_PATHS[previous[ref.step_id]]
                ):
                    raise ValueError("Invalid or forward step reference")
            if isinstance(config, ApplySyncConfig) and previous.get(config.plan_step_id) not in (
                {"PLAN_CRM_ATS_SYNC"}
                if step.type == "APPLY_CRM_ATS_SYNC"
                else {"APPLY_CRM_ATS_SYNC"}
            ):
                raise ValueError("Synchronization step requires its preceding checkpoint")
            previous[step.id] = step.type
        return self


class WorkflowCreate(Contract):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    definition: WorkflowJSON


class WorkflowVersionCreate(Contract):
    definition: WorkflowJSON


class WorkflowActivation(Contract):
    enabled: bool
    version_id: uuid.UUID | None = None


class ReplayRequest(Contract):
    confirmed: Literal[True]


class DispatchRequest(Contract):
    rescan_no_match: bool = False


# Code-owned checkpoint contracts, independent of browser display redaction.


class NormalizedCheckpoint(CanonicalEnvelope):
    transformation_attempt_id: uuid.UUID | None


class ConditionCheckpoint(Contract):
    matched: bool


class DelayCheckpoint(Contract):
    due_at: AwareDatetime


class ResultCheckpoint(Contract):
    summary: str = Field(max_length=500)
    outcome: Literal["COMPLETED", "OBSERVED"]
    canonical_id: uuid.UUID | None


CHECKPOINT_MODELS = {
    "PLAN_CRM_ATS_SYNC": SyncCheckpoint,
    "APPLY_CRM_ATS_SYNC": SyncCheckpoint,
    "RECORD_SYNC_OUTCOME": SyncCheckpoint,
    "NORMALIZE_EVENT": NormalizedCheckpoint,
    "TRANSFORM": NormalizedCheckpoint,
    "CONDITION": ConditionCheckpoint,
    "DELAY": DelayCheckpoint,
    "MOCK_CONNECTOR_ACTION": ConnectorResult,
    "RECORD_RESULT": ResultCheckpoint,
}
