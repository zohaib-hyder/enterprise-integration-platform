from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    ai_openai_enabled: bool = False
    ai_openai_api_key: SecretStr | None = None
    ai_openai_model: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    ai_timeout_seconds: float = Field(default=8, ge=1, le=10)
    ai_max_output_tokens: int = Field(default=1800, ge=256, le=4000)
    ai_max_concurrency: int = Field(default=2, ge=1, le=4)
    ai_requests_per_hour: int = Field(default=30, ge=1, le=100)
    ai_input_price_per_million: float | None = Field(default=None, ge=0)
    ai_output_price_per_million: float | None = Field(default=None, ge=0)
    ai_retention_days: int = Field(default=30, ge=1, le=90)
    ai_max_retained_requests: int = Field(default=1000, ge=100, le=5000)
    app_name: str = "Northstar Integration Platform"
    app_env: Literal["development", "test", "production"] = "development"
    app_debug: bool = False
    database_url: SecretStr | None = None
    webhook_requests_per_minute: int = Field(default=120, ge=10, le=1000)
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    cors_origins: list[str] = []
    auth_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    csrf_secret: SecretStr | None = Field(default=None, min_length=32)
    session_hours: int = Field(default=8, ge=1, le=24)
    invitation_hours: int = Field(default=48, ge=1, le=168)
    login_account_limit: int = Field(default=10, ge=1, le=100)
    login_ip_limit: int = Field(default=100, ge=1, le=1000)
    login_window_seconds: int = Field(default=300, ge=60, le=3600)
    invitation_delivery_dir: str = ".local/invitations"
    webhook_encryption_key: SecretStr | None = None
    credential_encryption_key: SecretStr | None = None
    credential_key_reference: str = Field(
        default="local-v1", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$"
    )
    credential_previous_keys: dict[str, SecretStr] = Field(default_factory=dict, max_length=3)
    operation_retention_days: int = Field(default=7, ge=1, le=30)
    operation_retention_max_rows: int = Field(default=5000, ge=100, le=20000)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        import re

        if not hosts or any(
            not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", h) for h in hosts
        ):
            raise ValueError(
                "Configure explicit hostnames without schemes, paths, ports or wildcards"
            )
        return hosts

    @field_validator("credential_encryption_key")
    @classmethod
    def credential_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            from app.core.webhook_security import decode_key

            decode_key(value.get_secret_value())
        return value

    @field_validator("credential_previous_keys")
    @classmethod
    def previous_keys(cls, values: dict[str, SecretStr]) -> dict[str, SecretStr]:
        import re

        from app.core.webhook_security import decode_key

        for reference, value in values.items():
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", reference):
                raise ValueError("Invalid credential key reference")
            decode_key(value.get_secret_value())
        return values

    @field_validator("webhook_encryption_key")
    @classmethod
    def validate_webhook_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            import base64

            try:
                raw = value.get_secret_value()
                decoded = base64.b64decode(raw, altchars=b"-_", validate=True)
                if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode() != raw:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(
                    "Webhook encryption key must be canonical URL-safe base64 of 32 bytes"
                ) from None
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                url = make_url(value.get_secret_value())
                if url.drivername != "postgresql+psycopg" or not url.database or not url.host:
                    raise ValueError
            except (ValueError, ArgumentError):
                raise ValueError("A PostgreSQL psycopg connection URL is required") from None
        return value

    @field_validator("cors_origins", "auth_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError("CORS must contain explicit HTTP(S) origins without paths")
        return origins

    @model_validator(mode="after")
    def production_safety(self) -> "Settings":
        if self.app_env == "production":
            if self.app_debug:
                raise ValueError("Production debug mode is prohibited")
            if not self.csrf_secret:
                raise ValueError("Production requires a persistent CSRF signing secret")
            if not self.auth_origins or any(
                not origin.startswith("https://") for origin in self.auth_origins
            ):
                raise ValueError("Production authentication origins require HTTPS")
            if self.database_url is None:
                raise ValueError("Production requires database configuration")
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError("Production CORS origins require HTTPS")
            if not self.webhook_encryption_key or not self.credential_encryption_key:
                raise ValueError("Production requires webhook and credential encryption keys")
            if self.webhook_encryption_key == self.credential_encryption_key:
                raise ValueError("Webhook and credential encryption keys must be independent")
            if "allowed_hosts" not in self.model_fields_set or "testserver" in self.allowed_hosts:
                raise ValueError("Production requires explicit trusted ALLOWED_HOSTS")
        return self
