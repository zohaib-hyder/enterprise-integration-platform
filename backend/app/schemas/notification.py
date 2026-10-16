"""Strict, bounded notification and recruiting calendar contracts."""

import re
import uuid
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.connectors.contracts import Contract, EventFields, SyntheticEmail
from app.schemas.workflow import RetryPolicy

Trigger = Literal["SYNC_CREATE", "SYNC_UPDATE", "SYNC_CONFLICT", "WORKFLOW_FAILURE", "MANUAL"]
PLACEHOLDERS = {
    "job_title",
    "requisition_reference",
    "client_display_name",
    "sync_outcome",
    "correlation_id",
    "review_reference",
}


def validate_text(value: str) -> str:
    if re.search(
        r"(?:[a-z][a-z0-9+.-]*://|www\.|mailto:|javascript:|data:)|<[^>]*>|[\x00-\x08\x0b-\x1f]",
        value,
        re.IGNORECASE,
    ):
        raise ValueError("External links, HTML and control characters are unsupported")
    tokens = re.findall(r"\{([^{}]*)\}", value)
    if (
        any(t not in PLACEHOLDERS for t in tokens)
        or re.sub(r"\{[^{}]*\}", "", value).count("{")
        or re.sub(r"\{[^{}]*\}", "", value).count("}")
    ):
        raise ValueError("Unsupported template placeholder")
    return value


class TemplateVersionInput(Contract):
    subject: str = Field(default="", max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    _safe = field_validator("subject", "body")(validate_text)

    @field_validator("subject")
    @classmethod
    def single_line(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("Subject must be one line")
        return v


class TemplateCreate(TemplateVersionInput):
    name: str = Field(min_length=1, max_length=100)
    channel: Literal["SLACK", "EMAIL"]

    @model_validator(mode="after")
    def email_subject(self):
        if self.channel == "EMAIL" and not self.subject.strip():
            raise ValueError("Email requires a subject")
        return self


class ActivateTemplate(Contract):
    version_id: uuid.UUID


class RenderInputs(Contract):
    job_title: str = Field(default="Synthetic requisition", max_length=160)
    requisition_reference: str = Field(default="Unspecified", max_length=100)
    client_display_name: str = Field(default="Synthetic staffing customer", max_length=120)
    sync_outcome: str = Field(default="MANUAL", max_length=40)
    correlation_id: str = Field(default="", max_length=36)
    review_reference: str = Field(default="Operations review", max_length=100)

    @field_validator("*")
    @classmethod
    def safe_inputs(cls, value: str) -> str:
        if re.search(
            r"(?:[a-z][a-z0-9+.-]*://|www\.|mailto:|javascript:|data:)|[\r\n\x00-\x1f]|(?:secret|password|token)\s*[:=]",
            value,
            re.IGNORECASE,
        ):
            raise ValueError("Unsafe rendering input")
        return value


class PreviewTemplate(Contract):
    version_id: uuid.UUID
    inputs: RenderInputs = Field(default_factory=RenderInputs)


class RuleDefinition(Contract):
    trigger: Trigger
    connection_id: uuid.UUID
    template_version_id: uuid.UUID
    destination: str = Field(min_length=1, max_length=254)
    sync_configuration_id: uuid.UUID | None = None


class RuleCreate(RuleDefinition):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False


class RuleUpdate(Contract):
    expected_revision: int = Field(ge=1)
    enabled: bool
    definition: RuleDefinition | None = None


class ManualNotification(Contract):
    rule_id: uuid.UUID
    request_key: uuid.UUID
    inputs: RenderInputs = Field(default_factory=RenderInputs)


class RecoveryInput(Contract):
    reason: str = Field(min_length=8, max_length=300)


class CalendarCreate(EventFields):
    connection_id: uuid.UUID
    participants: list[str] = Field(min_length=1, max_length=20)
    request_key: uuid.UUID

    @field_validator("participants")
    @classmethod
    def validate_participants(cls, v: list[str]) -> list[str]:
        result = [SyntheticEmail(email=x).email for x in v]
        if len(set(result)) != len(result):
            raise ValueError("Duplicate participants")
        return result


class CalendarChange(EventFields):
    request_key: uuid.UUID
    expected_updated_at: AwareDatetime
    participants: list[str] = Field(min_length=1, max_length=20)

    @field_validator("participants")
    @classmethod
    def validate_participants(cls, value: list[str]) -> list[str]:
        return CalendarCreate.validate_participants(value)


class CalendarCancel(Contract):
    request_key: uuid.UUID
    expected_updated_at: AwareDatetime


DEFAULT_RETRY = RetryPolicy()
