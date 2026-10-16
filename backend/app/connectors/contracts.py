"""Versioned adapter contracts. All connector inputs and results are validated."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.schemas.identity import EmailInput


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ConnectorType(StrEnum):
    CRM = "synthetic_crm"
    ATS = "synthetic_ats"
    SLACK = "synthetic_slack"
    EMAIL = "synthetic_email"
    CALENDAR = "synthetic_calendar"
    REST = "generic_rest"


class ErrorCategory(StrEnum):
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    TEMPORARY_UNAVAILABLE = "TEMPORARY_UNAVAILABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ERROR_MESSAGES = {
    ErrorCategory.AUTHENTICATION_FAILED: "Connector authentication failed",
    ErrorCategory.AUTHORIZATION_FAILED: "Connector access denied",
    ErrorCategory.RATE_LIMITED: "Connector rate limit reached",
    ErrorCategory.TIMEOUT: "Connector request timed out",
    ErrorCategory.TEMPORARY_UNAVAILABLE: "Connector temporarily unavailable",
    ErrorCategory.VALIDATION_FAILED: "Connector input validation failed",
    ErrorCategory.NOT_FOUND: "Synthetic record unavailable",
    ErrorCategory.CONFLICT: "Synthetic operation conflicts with existing data",
    ErrorCategory.UNSUPPORTED_OPERATION: "Operation is not supported by this connector",
    ErrorCategory.INTERNAL_ERROR: "Connector operation failed",
}


class ConnectorError(Exception):
    def __init__(self, category: ErrorCategory):
        self.category = category
        self.retryable = category in {
            ErrorCategory.RATE_LIMITED,
            ErrorCategory.TIMEOUT,
            ErrorCategory.TEMPORARY_UNAVAILABLE,
        }
        self.safe_message = ERROR_MESSAGES[category]
        super().__init__(self.safe_message)


class ConnectorConfig(Contract):
    schema_version: Literal[1] = 1


class BusinessConfig(ConnectorConfig):
    workspace_label: str = Field(
        default="Synthetic business workspace", min_length=1, max_length=100
    )


class SlackConfig(ConnectorConfig):
    allowed_channels: list[Literal["recruiting", "operations", "alerts"]] = Field(
        default_factory=lambda: ["recruiting"], min_length=1, max_length=3
    )


class SyntheticEmail(EmailInput):
    @field_validator("email")
    @classmethod
    def synthetic_domain(cls, value: str) -> str:
        if not (value.endswith("@example.test") or value.split("@")[-1].endswith(".example.test")):
            raise ValueError("Use a synthetic example.test recipient")
        return value


class EmailConfig(ConnectorConfig):
    allowed_recipients: list[str] = Field(
        default_factory=lambda: ["recruiter@example.test"], min_length=1, max_length=20
    )

    @field_validator("allowed_recipients")
    @classmethod
    def recipients(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(SyntheticEmail(email=value).email for value in values))


class CalendarConfig(ConnectorConfig):
    allowed_calendars: list[Literal["interviews", "recruiting"]] = Field(
        default_factory=lambda: ["interviews"], min_length=1, max_length=2
    )


class RestConfig(ConnectorConfig):
    # No URL, headers or credential fields. Outbound execution is disabled in Phase 2.
    mode: Literal["disabled"] = "disabled"


class ConnectorHealth(Contract):
    status: Literal["HEALTHY", "UNAVAILABLE", "DISABLED"]
    synthetic: bool = True
    checked_at: datetime
    message: str


class OperationName(StrEnum):
    CONTACT_LIST = "contact.list"
    CONTACT_GET = "contact.get"
    CONTACT_UPSERT = "contact.upsert"
    CANDIDATE_LIST = "candidate.list"
    CANDIDATE_GET = "candidate.get"
    CANDIDATE_UPSERT = "candidate.upsert"
    REQUISITION_LIST = "requisition.list"
    REQUISITION_GET = "requisition.get"
    REQUISITION_UPSERT = "requisition.upsert"
    RECORD_SEARCH = "record.search"
    MESSAGE_SEND = "message.send"
    EVENT_LIST = "event.list"
    EVENT_CREATE = "event.create"
    EVENT_UPDATE = "event.update"
    EVENT_CANCEL = "event.cancel"


class ConnectorOperation(Contract):
    schema_version: Literal[1] = 1
    name: OperationName
    payload: dict = Field(default_factory=dict)


ExternalId = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
]
IdempotencyKey = Annotated[
    str, Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
]


class ListPayload(Contract):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=25, ge=1, le=100)


class GetPayload(Contract):
    id: uuid.UUID


class SearchPayload(Contract):
    external_id: ExternalId
    kind: Literal["contact", "candidate", "requisition"]


class ContactData(Contract):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    company: str = Field(min_length=1, max_length=120)

    @field_validator("email")
    @classmethod
    def email_synthetic(cls, value: str) -> str:
        return SyntheticEmail(email=value).email


class CandidateData(Contract):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    stage: Literal["NEW", "SCREENING", "SUBMITTED"] = "NEW"
    skills: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=20
    )

    @field_validator("email")
    @classmethod
    def email_synthetic(cls, value: str) -> str:
        return SyntheticEmail(email=value).email


class RequisitionData(Contract):
    title: str = Field(min_length=1, max_length=160)
    department: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=120)
    status: Literal["OPEN", "CLOSED"] = "OPEN"


class ContactUpsert(Contract):
    external_id: ExternalId
    data: ContactData


class CandidateUpsert(Contract):
    external_id: ExternalId
    data: CandidateData


class RequisitionUpsert(Contract):
    external_id: ExternalId
    data: RequisitionData


class SlackSend(Contract):
    channel: Literal["recruiting", "operations", "alerts"]
    text: str = Field(min_length=1, max_length=2000)
    idempotency_key: IdempotencyKey


class EmailSend(Contract):
    recipient: str = Field(max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    idempotency_key: IdempotencyKey

    @field_validator("recipient")
    @classmethod
    def recipient_synthetic(cls, value: str) -> str:
        return SyntheticEmail(email=value).email

    @field_validator("subject")
    @classmethod
    def subject_safe(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("A single-line subject is required")
        return value.strip()


class EventFields(Contract):
    calendar: Literal["interviews", "recruiting"]
    title: str = Field(min_length=1, max_length=160)
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def valid_interval(self) -> "EventFields":
        if self.ends_at <= self.starts_at:
            raise ValueError("Event end must follow start")
        return self


class EventCreate(EventFields):
    idempotency_key: IdempotencyKey


class EventUpdate(EventFields):
    id: uuid.UUID


class RecordOutput(Contract):
    id: uuid.UUID
    external_id: str
    kind: Literal["contact", "candidate", "requisition"]
    data: ContactData | CandidateData | RequisitionData
    synthetic: Literal[True] = True
    created_at: datetime
    updated_at: datetime


class RecordPage(Contract):
    items: list[RecordOutput]
    total: int
    offset: int
    limit: int


class DeliveryOutput(Contract):
    id: uuid.UUID
    delivery_reference: str
    status: Literal["SIMULATED"] = "SIMULATED"
    duplicate: bool = False
    synthetic: Literal[True] = True


class EventOutput(Contract):
    id: uuid.UUID
    calendar: str
    title: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    status: Literal["BOOKED", "CANCELLED"]
    duplicate: bool = False
    synthetic: Literal[True] = True
    created_at: datetime
    updated_at: datetime


class EventPage(Contract):
    items: list[EventOutput]
    total: int
    offset: int
    limit: int


class ConnectorResult(Contract):
    schema_version: Literal[1] = 1
    operation: OperationName
    data: RecordOutput | RecordPage | DeliveryOutput | EventOutput | EventPage
    synthetic: Literal[True] = True


class OperationSpec(Contract):
    name: OperationName
    schema_version: Literal[1] = 1
    mutating: bool
    payload_schema: dict


class ConnectorCapabilities(Contract):
    connector_type: ConnectorType
    name: str
    synthetic: bool
    enabled: bool
    limitation: str | None = None
    config_schema_version: Literal[1] = 1
    config_schema: dict
    operations: list[OperationSpec]


class Connector(Protocol):
    config_model: type[ConnectorConfig]

    def validate_config(self, value: dict) -> ConnectorConfig: ...
    def health_check(self, config: ConnectorConfig) -> ConnectorHealth: ...
    def supported_operations(self) -> dict[OperationName, type[Contract]]: ...
    def execute(
        self,
        db: Session,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: ConnectorConfig,
        operation: ConnectorOperation,
    ) -> ConnectorResult: ...
