"""Release startup must reject incomplete/unsafe production configuration."""

import base64
import secrets

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def production() -> dict:
    return {
        "_env_file": None,
        "app_env": "production",
        "database_url": "postgresql+psycopg://fixture:synthetic@localhost/fixture",
        "csrf_secret": secrets.token_urlsafe(48),
        "auth_origins": ["https://workspace.example.test"],
        "allowed_hosts": ["backend", "workspace.example.test"],
        "webhook_encryption_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "credential_encryption_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_debug", True),
        ("database_url", None),
        ("database_url", "sqlite:///unsafe"),
        ("csrf_secret", None),
        ("csrf_secret", "short"),
        ("webhook_encryption_key", None),
        ("credential_encryption_key", None),
        ("webhook_encryption_key", "invalid-key"),
        ("credential_encryption_key", "invalid-key"),
        ("allowed_hosts", ["*"]),
        ("allowed_hosts", ["testserver"]),
        ("allowed_hosts", []),
        ("cors_origins", ["*"]),
        ("cors_origins", ["http://workspace.example.test"]),
        ("auth_origins", ["http://workspace.example.test"]),
        ("auth_origins", []),
    ],
)
def test_production_rejects_unsafe_configuration(field: str, value: object) -> None:
    config = production()
    config[field] = value
    with pytest.raises(ValidationError):
        Settings(**config)


def test_explicit_production_hosts_required() -> None:
    config = production()
    del config["allowed_hosts"]
    with pytest.raises(ValidationError, match="explicit trusted"):
        Settings(**config)


def test_independent_keys_required() -> None:
    config = production()
    config["credential_encryption_key"] = config["webhook_encryption_key"]
    with pytest.raises(ValidationError, match="independent"):
        Settings(**config)


def test_production_safety_and_secret_redaction() -> None:
    values = production()
    config = Settings(**values)
    assert not config.ai_openai_enabled
    assert not config.app_debug
    assert values["csrf_secret"] not in repr(config)
    assert values["webhook_encryption_key"] not in repr(config)
    assert values["credential_encryption_key"] not in repr(config)
