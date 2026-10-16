"""Version-one canonical entities and declarative mapping contracts."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictFloat, StrictInt, field_validator, model_validator

from app.connectors.contracts import Contract, ExternalId, SyntheticEmail

EntityType = Literal["client", "contact", "job_requisition", "candidate", "application", "activity"]
SourceKind = Literal["contact", "candidate", "requisition"]
Text = Annotated[str, Field(min_length=1, max_length=160)]
Money = Annotated[Decimal, Field(ge=0, le=10000000, max_digits=12, decimal_places=2)]


class Client(Contract):
    name: Text
    industry: Text | None = None
    headcount: int | None = Field(default=None, ge=0, le=10000000)


class Person(Contract):
    name: Text
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return SyntheticEmail(email=value).email


class Contact(Person):
    company: Text
    client_id: uuid.UUID | None = None
    last_contacted_at: AwareDatetime | None = None


class JobRequisition(Contract):
    title: Text
    department: Text
    location: Text
    status: Literal["OPEN", "CLOSED", "PAUSED"] | None = None
    recruiting_status: Literal["OPEN", "CLOSED", "PAUSED"] | None = None
    commercial_rate: Money | None = None
    description: str | None = Field(default=None, max_length=1000)


class Candidate(Person):
    stage: Literal["NEW", "SCREENING", "SUBMITTED", "PLACED", "REJECTED"]
    skills: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=20
    )
    years_experience: Decimal | None = Field(
        default=None, ge=0, le=80, max_digits=4, decimal_places=2
    )
    available_at: AwareDatetime | None = None


class Application(Contract):
    candidate_id: uuid.UUID
    job_requisition_id: uuid.UUID
    stage: Literal["DRAFT", "SUBMITTED", "INTERVIEW", "OFFER", "PLACED", "REJECTED"]
    submitted_at: AwareDatetime | None = None


class Activity(Contract):
    kind: Literal["INTERVIEW", "EMAIL", "NOTE"]
    summary: str = Field(min_length=1, max_length=1000)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def interval(self) -> "Activity":
        if self.ends_at is not None and (self.starts_at is None or self.ends_at <= self.starts_at):
            raise ValueError("Invalid activity interval")
        return self


ENTITY_MODELS = {
    "client": Client,
    "contact": Contact,
    "job_requisition": JobRequisition,
    "candidate": Candidate,
    "application": Application,
    "activity": Activity,
}


class CanonicalMetadata(Contract):
    synthetic: Literal[True] = True
    tags: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=10
    )


class CanonicalEnvelope(Contract):
    schema_version: Literal[1]
    entity_type: EntityType
    canonical_id: uuid.UUID
    tenant_id: uuid.UUID
    source_connection_id: uuid.UUID
    source_external_id: ExternalId
    source_updated_at: AwareDatetime | None = None
    normalized_at: AwareDatetime
    correlation_id: uuid.UUID
    normalized_data: dict
    metadata: CanonicalMetadata = Field(default_factory=CanonicalMetadata)

    @model_validator(mode="after")
    def validate_entity(self) -> "CanonicalEnvelope":
        model = ENTITY_MODELS[self.entity_type].model_validate(self.normalized_data)
        self.normalized_data = model.model_dump(mode="json", exclude_unset=True)
        return self


class SourceProfile(Contract):
    email: str | None = Field(default=None, max_length=254)


class SourceContact(Contract):
    name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    company: str | None = Field(default=None, max_length=160)
    profile: SourceProfile | None = None
    last_contacted_at: str | None = Field(default=None, max_length=64)


class SourceCandidate(Contract):
    name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    profile: SourceProfile | None = None
    stage: str | None = Field(default=None, max_length=32)
    skills: list[Annotated[str, Field(max_length=60)]] | None = Field(default=None, max_length=20)
    years_experience: str | StrictInt | StrictFloat | None = None
    available_at: str | None = Field(default=None, max_length=64)


class SourceJob(Contract):
    title: str | None = Field(default=None, max_length=160)
    department: str | None = Field(default=None, max_length=160)
    location: str | None = Field(default=None, max_length=160)
    status: str | None = Field(default=None, max_length=32)
    commercial_rate: str | StrictInt | StrictFloat | None = None
    description: str | None = Field(default=None, max_length=1000)


SOURCE_MODELS = {"contact": SourceContact, "candidate": SourceCandidate, "requisition": SourceJob}
SOURCE_PATHS = {
    "contact": {
        "data.name",
        "data.email",
        "data.company",
        "data.profile.email",
        "data.last_contacted_at",
    },
    "candidate": {
        "data.name",
        "data.email",
        "data.profile.email",
        "data.stage",
        "data.skills",
        "data.years_experience",
        "data.available_at",
    },
    "requisition": {
        "data.title",
        "data.department",
        "data.location",
        "data.status",
        "data.commercial_rate",
        "data.description",
    },
}
SUPPORTED = {
    ("synthetic_crm", "contact"): "contact",
    ("synthetic_crm", "requisition"): "job_requisition",
    ("synthetic_ats", "candidate"): "candidate",
    ("synthetic_ats", "requisition"): "job_requisition",
}
Operation = Literal["trim", "email", "enum", "datetime", "number", "string"]


class TransformStep(Contract):
    operation: Operation
    values: dict[str, str] | None = None
    numeric_type: Literal["integer", "decimal"] = "decimal"
    max_length: int = Field(default=160, ge=1, le=1000)

    @model_validator(mode="after")
    def parameters(self) -> "TransformStep":
        if self.operation == "enum":
            if (
                not self.values
                or len(self.values) > 20
                or any(len(k) > 32 or len(v) > 32 for k, v in self.values.items())
            ):
                raise ValueError("A bounded enum map is required")
        elif self.values is not None:
            raise ValueError("Enum parameters apply only to enum operations")
        return self


class FieldMapping(Contract):
    target: str = Field(min_length=1, max_length=64)
    source: str | None = Field(default=None, max_length=64)
    required: bool = False
    null_handling: Literal["reject", "omit", "default", "preserve"] = "omit"
    default: str | int | bool | list[str] | None = None
    steps: list[TransformStep] = Field(default_factory=list, max_length=5)

    @field_validator("default")
    @classmethod
    def bounded_default(cls, value):
        if (
            isinstance(value, str)
            and len(value) > 1000
            or isinstance(value, list)
            and (len(value) > 20 or any(len(item) > 60 for item in value))
        ):
            raise ValueError("Default exceeds bounds")
        return value


class MappingDefinition(Contract):
    schema_version: Literal[1] = 1
    fields: list[FieldMapping] = Field(min_length=1, max_length=20)


class MappingCreate(Contract):
    connection_id: uuid.UUID
    source_kind: SourceKind
    name: str = Field(min_length=1, max_length=100)
    definition: MappingDefinition | None = None


class MappingVersionCreate(Contract):
    definition: MappingDefinition


class VersionChoice(Contract):
    mapping_version_id: uuid.UUID | None = None


class TransformInput(VersionChoice):
    external_id: ExternalId
    source_updated_at: AwareDatetime | None = None
    payload: dict


class ExternalLink(Contract):
    canonical_id: uuid.UUID
    connection_id: uuid.UUID
    source_kind: SourceKind
    external_id: ExternalId


class ValidationIssue(Contract):
    category: Literal[
        "INVALID_MAPPING",
        "INVALID_SOURCE",
        "MISSING_FIELD",
        "INVALID_VALUE",
        "CANONICAL_VALIDATION",
        "AUTHORITY_CONFLICT",
        "STALE_UPDATE",
        "OUT_OF_ORDER",
        "MANUAL_REVIEW",
    ]
    field: str | None = None


def normalize_time(value: datetime) -> datetime:
    return value.astimezone(UTC)


class MappingOutput(Contract):
    id: uuid.UUID
    connection_id: uuid.UUID
    source_kind: SourceKind
    entity_type: EntityType
    name: str
    active_version: int
    created_at: datetime
    updated_at: datetime


class VersionOutput(Contract):
    id: uuid.UUID
    mapping_id: uuid.UUID
    version: int
    schema_version: Literal[1]
    definition: MappingDefinition
    definition_digest: str
    created_at: datetime


class Activation(Contract):
    mapping_version_id: uuid.UUID


class AttemptOutput(Contract):
    id: uuid.UUID
    mapping_version_id: uuid.UUID
    event_id: uuid.UUID | None
    canonical_id: uuid.UUID | None
    entity_type: EntityType
    connection_id: uuid.UUID
    source_kind: SourceKind
    external_id: ExternalId
    source_updated_at: datetime | None
    outcome: Literal["SUCCEEDED", "NO_CHANGE", "QUARANTINED", "CONFLICT", "MANUAL_REVIEW"]
    issues: list[ValidationIssue]
    correlation_id: uuid.UUID
    created_at: datetime
    result_snapshot: dict | None


class PreviewOutput(Contract):
    mapping_version_id: uuid.UUID
    outcome: Literal["SUCCEEDED", "NO_CHANGE", "QUARANTINED", "CONFLICT", "MANUAL_REVIEW"]
    issues: list[ValidationIssue]
    canonical: dict | None
    persisted: Literal[False]


class CanonicalPublic(Contract):
    schema_version: Literal[1]
    entity_type: EntityType
    canonical_id: uuid.UUID
    tenant_id: uuid.UUID
    source_connection_id: uuid.UUID
    source_external_id: ExternalId
    source_updated_at: datetime | None
    normalized_at: datetime
    correlation_id: uuid.UUID
    normalized_data: dict  # Display representation has intentional PII redaction.
    metadata: CanonicalMetadata
    revision: int
    field_provenance: dict


class ExternalOutput(Contract):
    id: uuid.UUID
    connection_id: uuid.UUID
    source_kind: SourceKind
    external_id: ExternalId
    canonical_id: uuid.UUID
    entity_type: EntityType
    last_source_updated_at: datetime | None
    provenance: Literal["NORMALIZATION", "MANUAL_LINK"]
    created_at: datetime


class InvalidOutput(Contract):
    id: uuid.UUID
    status: Literal["OPEN", "RESOLVED"]
    resolution_attempt_id: uuid.UUID | None
    attempt: AttemptOutput
    created_at: datetime


class AuthorityOutput(Contract):
    id: uuid.UUID
    entity_type: EntityType
    field: str
    owner: str


class MappingPage(Contract):
    items: list[MappingOutput]
    total: int
    offset: int
    limit: int


class VersionPage(Contract):
    items: list[VersionOutput]
    total: int
    offset: int
    limit: int


class RecordPage(Contract):
    items: list[CanonicalPublic]
    total: int
    offset: int
    limit: int


class AttemptPage(Contract):
    items: list[AttemptOutput]
    total: int
    offset: int
    limit: int


class ExternalPage(Contract):
    items: list[ExternalOutput]
    total: int
    offset: int
    limit: int


class InvalidPage(Contract):
    items: list[InvalidOutput]
    total: int
    offset: int
    limit: int


class AuthorityPage(Contract):
    items: list[AuthorityOutput]
    total: int
    offset: int
    limit: int
