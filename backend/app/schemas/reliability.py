"""Bounded policies and synthetic secret submission; no plaintext read contracts."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, SecretStr, field_validator, model_validator

from app.connectors.contracts import Contract

CredentialType = Literal[
    "API_KEY", "OAUTH_ACCESS_TOKEN", "OAUTH_REFRESH_TOKEN", "SERVICE_ACCOUNT_REFERENCE"
]
AlertCategory = Literal[
    "CONNECTION_UNHEALTHY",
    "CREDENTIAL_EXPIRED",
    "REFRESH_FAILED",
    "RATE_LIMIT_EXCEEDED",
    "CIRCUIT_OPEN",
    "WORKFLOW_DEAD_LETTERED",
    "WORKER_RECOVERY",
    "REPEATED_CONNECTOR_FAILURE",
    "REPEATED_DELIVERY_FAILURE",
    "TERMINAL_NOTIFICATION_FAILURE",
    "UNKNOWN_DELIVERY_OUTCOME",
    "CALENDAR_CONFLICT",
]
AlertStatus = Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]
Severity = Literal["INFO", "WARNING", "CRITICAL"]
Scenario = Literal[
    "NONE",
    "HTTP_401",
    "HTTP_403",
    "HTTP_429",
    "HTTP_500",
    "HTTP_503",
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "EXPIRED_ACCESS_TOKEN",
    "REVOKED_REFRESH_TOKEN",
    "REFRESH_FAILED",
    "RECOVERING_OUTAGE",
]


class ReliabilityPolicy(Contract):
    schema_version: Literal[1] = 1
    connect_timeout_ms: int = Field(default=1000, ge=50, le=3000)
    read_timeout_ms: int = Field(default=3000, ge=50, le=5000)
    operation_deadline_ms: int = Field(default=5000, ge=100, le=10000)
    max_attempts: int = Field(default=3, ge=1, le=5)
    base_delay_seconds: int = Field(default=1, ge=1, le=30)
    max_delay_seconds: int = Field(default=30, ge=1, le=300)
    jitter_fraction: float = Field(default=0.2, ge=0, le=0.3, allow_inf_nan=False)
    circuit_failure_threshold: int = Field(default=5, ge=1, le=10)
    circuit_cooldown_seconds: int = Field(default=30, ge=1, le=120)
    repeated_failure_threshold: int = Field(default=3, ge=2, le=10)
    credential_mode: Literal["OPTIONAL", "API_KEY", "OAUTH", "SERVICE_ACCOUNT_REFERENCE"] = (
        "OPTIONAL"
    )

    @model_validator(mode="after")
    def bounds(self):
        if (
            max(self.connect_timeout_ms, self.read_timeout_ms) > self.operation_deadline_ms
            or self.max_delay_seconds < self.base_delay_seconds
        ):
            raise ValueError("Policy timeout or backoff bounds are inconsistent")
        return self


class Budget(Contract):
    scope: Literal["TENANT", "CONNECTION", "CONNECTOR"]
    limit: int = Field(ge=1, le=100000)
    window_seconds: int = Field(default=60, ge=1, le=120)


class FailureConfig(Contract):
    enabled: bool = False
    scenario: Scenario = "NONE"
    attempts: int = Field(default=1, ge=1, le=10)
    ttl_seconds: int = Field(default=300, ge=10, le=3600)
    retry_after: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def valid(self):
        if self.enabled and self.scenario == "NONE":
            raise ValueError("Choose an explicit fault scenario")
        if self.retry_after is not None:
            from app.connectors.reliability import parse_retry_after

            if parse_retry_after(self.retry_after) is None:
                raise ValueError("Retry-After must be delta seconds or an aware HTTP date")
        return self


class CredentialSubmission(Contract):
    credential_type: CredentialType
    value: SecretStr = Field(min_length=16, max_length=256)
    expires_at: AwareDatetime | None = None

    @field_validator("value")
    @classmethod
    def synthetic(cls, value: SecretStr):
        import re

        if not re.fullmatch(r"sim_[a-zA-Z0-9_-]{12,252}", value.get_secret_value()):
            raise ValueError("Only synthetic sim_ credential values are accepted")
        return value

    @model_validator(mode="after")
    def expiry(self):
        if self.credential_type.startswith("OAUTH_") and self.expires_at is None:
            raise ValueError("OAuth-style credentials require an aware expiration")
        return self


class CredentialReplacement(Contract):
    value: SecretStr = Field(min_length=16, max_length=256)
    expires_at: AwareDatetime | None = None

    @field_validator("value")
    @classmethod
    def synthetic(cls, value):
        return CredentialSubmission.synthetic(value)


class CredentialMetadata(Contract):
    id: uuid.UUID
    connection_id: uuid.UUID
    credential_type: CredentialType
    generation: int
    status: Literal["ACTIVE", "EXPIRED", "REVOKED", "ROTATED"]
    key_reference: str
    expires_at: datetime | None
    revoked_at: datetime | None
    rotated_at: datetime | None
    replaces_id: uuid.UUID | None
    created_at: datetime
    masked_value: Literal["••••••••"] = "••••••••"
