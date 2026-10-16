import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.connectors.contracts import CandidateData, Contract, ExternalId, RequisitionData

Source = Literal["synthetic_crm", "synthetic_ats"]
EventType = Literal["job.created", "job.updated", "candidate.created", "candidate.updated"]


class EndpointCreate(Contract):
    connection_id: uuid.UUID
    source: Source
    timestamp_tolerance: int = Field(default=300, ge=30, le=300)


class EndpointUpdate(Contract):
    enabled: bool


class RotateSecret(Contract):
    grace_seconds: int = Field(default=300, ge=0, le=900)


class EndpointOutput(Contract):
    id: uuid.UUID
    connection_id: uuid.UUID
    source: Source
    status: Literal["ACTIVE", "DISABLED"]
    public_id: str
    signature_version: Literal["hmac-sha256-v1"]
    timestamp_tolerance: int
    current_secret_version: int
    previous_secret_valid_until: datetime | None
    created_at: datetime
    updated_at: datetime


class EndpointDisclosure(Contract):
    endpoint: EndpointOutput
    signing_secret: str
    secret_version: int


class EndpointPage(Contract):
    items: list[EndpointOutput]
    total: int
    offset: int
    limit: int


class BusinessEvent(Contract):
    schema_version: Literal[1]
    source: Source
    event_id: ExternalId
    event_type: EventType
    occurred_at: AwareDatetime
    external_id: ExternalId
    data: CandidateData | RequisitionData

    @model_validator(mode="after")
    def applicable(self) -> "BusinessEvent":
        if self.event_type.startswith("candidate."):
            if self.source != "synthetic_ats" or not isinstance(self.data, CandidateData):
                raise ValueError("Candidate events require ATS candidate data")
        elif not isinstance(self.data, RequisitionData):
            raise ValueError("Job events require requisition data")
        return self


class EventOutput(Contract):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    connection_id: uuid.UUID
    source: Source
    provider_event_id: str
    event_type: EventType
    schema_version: int
    payload_digest: str
    received_at: datetime
    occurred_at: datetime
    ingestion_status: Literal["ACCEPTED"]
    processing_status: Literal["PENDING"]
    correlation_id: uuid.UUID
    duplicate_count: int
    payload: dict | None = None


class EventPage(Contract):
    items: list[EventOutput]
    total: int
    offset: int
    limit: int


class ReceiptOutput(Contract):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_id: uuid.UUID | None
    outcome: Literal["ACCEPTED", "DUPLICATE", "CONFLICT", "REJECTED"]
    error_category: str | None
    correlation_id: uuid.UUID
    duration_ms: int
    received_at: datetime


class ReceiptPage(Contract):
    items: list[ReceiptOutput]
    total: int
    offset: int
    limit: int


class SimulationInput(Contract):
    event_type: EventType = "job.created"
    event_id: ExternalId
    scenario: Literal["valid", "duplicate", "invalid_signature", "stale", "altered_payload"] = (
        "valid"
    )
