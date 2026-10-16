import re
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Role(StrEnum):
    TENANT_ADMIN = "TENANT_ADMIN"
    INTEGRATION_ENGINEER = "INTEGRATION_ENGINEER"
    OPERATIONS_USER = "OPERATIONS_USER"


class SafeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class EmailInput(SafeModel):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(
            r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", value
        ):
            raise ValueError("Valid email required")
        return value


class LoginInput(EmailInput):
    password: SecretStr = Field(min_length=1, max_length=1024)


class InvitationInput(EmailInput):
    role: Role


class AcceptInvitation(SafeModel):
    token: SecretStr = Field(min_length=40, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)


class MemberChange(SafeModel):
    role: Role | None = None
    status: str | None = Field(default=None, pattern="^(ACTIVE|INACTIVE)$")


class TenantSelection(SafeModel):
    tenant_id: uuid.UUID


class UserOutput(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str


class TenantOutput(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: Role


class MembershipOutput(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    status: str


class TenantSettingsInput(SafeModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Tenant name required")
        return value.strip()


class MeOutput(BaseModel):
    user: UserOutput
    tenants: list[TenantOutput]
    active_tenant_id: uuid.UUID | None
    permissions: list[str]
    memberships: list[MembershipOutput]
    session_id: uuid.UUID


class MemberOutput(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    status: str
    created_at: datetime


class InvitationOutput(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    expires_at: datetime
    created_at: datetime
    status: str


class AuditOutput(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID | None
    metadata: dict[str, str]
    created_at: datetime


class MessageOutput(BaseModel):
    message: str


class MemberPage(BaseModel):
    items: list[MemberOutput]
    total: int
    offset: int
    limit: int


class InvitationPage(BaseModel):
    items: list[InvitationOutput]
    total: int
    offset: int
    limit: int


class AuditPage(BaseModel):
    items: list[AuditOutput]
    total: int
    offset: int
    limit: int
