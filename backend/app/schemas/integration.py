import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.connectors.contracts import ConnectorType, Contract, EventOutput, RecordOutput


class CreateConnection(Contract):
    connector_type: ConnectorType
    name: str = Field(min_length=1, max_length=100)
    config: dict = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def name_clean(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Connection name required")
        return value.strip()


class UpdateConnection(Contract):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def name_clean(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Connection name required")
        return value.strip() if value is not None else None


class ConnectionOutput(Contract):
    id: uuid.UUID
    connector_type: ConnectorType
    name: str
    status: Literal["ENABLED", "DISABLED"]
    config_schema_version: int
    public_config: dict
    health_status: Literal["UNKNOWN", "HEALTHY", "UNAVAILABLE", "DISABLED"]
    last_checked_at: datetime | None
    synthetic: bool
    created_at: datetime
    updated_at: datetime


class ConnectionPage(Contract):
    items: list[ConnectionOutput]
    total: int
    offset: int
    limit: int


class NotificationOutput(Contract):
    id: uuid.UUID
    channel: str
    text: str
    status: Literal["SIMULATED"]
    synthetic: Literal[True] = True
    created_at: datetime
    updated_at: datetime


class EmailOutput(Contract):
    id: uuid.UUID
    recipient: str
    subject: str
    body: str
    status: Literal["SIMULATED"]
    synthetic: Literal[True] = True
    created_at: datetime
    updated_at: datetime


class InspectionPage(Contract):
    items: list[RecordOutput | NotificationOutput | EmailOutput | EventOutput]
    total: int
    offset: int
    limit: int
