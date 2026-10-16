"""Bounded synchronization contracts. No executable expressions or arbitrary fields."""

import uuid
from typing import Literal

from pydantic import Field, model_validator

from app.connectors.contracts import Contract, ExternalId

Decision = Literal["CREATE", "UPDATE", "NO_OP", "CONFLICT", "SKIP", "INVALID"]
Category = Literal[
    "STALE_SOURCE",
    "DESTINATION_CHANGED",
    "FIELD_AUTHORITY_VIOLATION",
    "IDENTITY_COLLISION",
    "AMBIGUOUS_MATCH",
    "INVALID_MAPPING",
    "CONCURRENT_UPDATE",
    "UNKNOWN_WRITE_OUTCOME",
]


class SyncDefinition(Contract):
    entity_type: Literal["job_requisition"] = "job_requisition"
    mapping_version_id: uuid.UUID
    trigger_event_types: list[Literal["job.created", "job.updated"]] = Field(
        default_factory=lambda: ["job.created", "job.updated"], min_length=1, max_length=2
    )
    destination_fields: dict[str, str] = Field(
        default_factory=lambda: {
            "title": "title",
            "department": "department",
            "location": "location",
        },
        min_length=1,
        max_length=3,
    )
    field_authority_policy: Literal["TENANT_FIELD_AUTHORITY"] = "TENANT_FIELD_AUTHORITY"
    create_enabled: bool = True
    update_enabled: bool = True
    conflict_policy: Literal["HOLD"] = "HOLD"
    match_rule: Literal["NONE", "EXACT_TITLE_DEPARTMENT_LOCATION"] = "NONE"

    @model_validator(mode="after")
    def supported(self):
        if any(
            k != v or k not in {"title", "department", "location"}
            for k, v in self.destination_fields.items()
        ):
            raise ValueError(
                "ATS v1 supports only title, department and location; ATS status is preserved"
            )
        if len(set(self.trigger_event_types)) != len(self.trigger_event_types):
            raise ValueError("Duplicate event types")
        return self


class SyncCreate(Contract):
    name: str = Field(min_length=1, max_length=100)
    source_connection_id: uuid.UUID
    destination_connection_id: uuid.UUID
    definition: SyncDefinition


class SyncActivation(Contract):
    enabled: bool
    version_id: uuid.UUID | None = None
    expected_revision: int = Field(ge=1)


class PlanPreview(Contract):
    event_id: uuid.UUID | None = None
    source_external_id: ExternalId | None = None

    @model_validator(mode="after")
    def one(self):
        if (self.event_id is None) == (self.source_external_id is None):
            raise ValueError("Choose one verified event or current synthetic source record")
        return self


class Resolution(Contract):
    action: Literal[
        "ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS",
        "KEEP_DESTINATION",
        "SKIP_UPDATE",
        "RETRY_AFTER_REFRESH",
        "LINK_EXISTING_IDENTITY",
    ]
    reason: str = Field(min_length=8, max_length=500)
    expected_revision: int = Field(ge=1)
    expected_destination_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_link_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_key: uuid.UUID
    destination_external_id: ExternalId | None = None


class ReconcileRequest(Contract):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=25, ge=1, le=100)


class SyncStepConfig(Contract):
    configuration_version_id: uuid.UUID


class ApplySyncConfig(Contract):
    plan_step_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")


class SyncCheckpoint(Contract):
    plan_id: uuid.UUID
    execution_id: uuid.UUID
    decision: Decision
    state: Literal["PLANNED", "SUCCEEDED", "HELD", "SKIPPED"]


class SourceIdentity(Contract):
    connection_id: uuid.UUID
    external_id: ExternalId
    canonical_id: uuid.UUID


class DestinationIdentity(Contract):
    connection_id: uuid.UUID
    external_id: ExternalId
    record_id: uuid.UUID | None


class FieldChange(Contract):
    before: str | None
    after: str


class AuthorityDecision(Contract):
    owner: str = Field(max_length=40)
    authorized: bool
    destination_field: Literal["title", "department", "location"]


class PlanEvidence(Contract):
    decision: Decision
    proposed_changes: dict[str, FieldChange]
    preserved_destination_fields: dict[str, str]
    authority_decisions: dict[str, AuthorityDecision]
    conflict_reasons: list[Category]
    explanation: str = Field(max_length=300)
    source_identity: SourceIdentity
    destination_identity: DestinationIdentity
    source_data: dict[str, str | None]
    source_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_record_updated_at: str | None
    source_updated_at: str | None
    destination_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_updated_at: str | None
    mapping_version_id: uuid.UUID
    configuration_version_id: uuid.UUID
    candidate_ids: list[uuid.UUID] = Field(max_length=2)
