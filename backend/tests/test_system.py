import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def client():
    with TestClient(
        create_app(Settings(_env_file=None, app_env="test", database_url=None))
    ) as client:
        yield client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    uuid.UUID(response.headers["X-Correlation-ID"])


def test_info_is_safe(client):
    body = client.get("/api/v1/system/info").json()
    assert body == {
        "name": "Northstar Integration Platform",
        "version": "0.1.0",
        "api_version": "v1",
        "environment": "test",
        "phase": 12,
    }


def test_readiness_without_database(client):
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "down"}}


def test_database_failure_is_redacted(client):
    with patch("app.api.routes.check_database", side_effect=SQLAlchemyError("secret-password")):
        response = client.get("/ready")
    assert response.status_code == 503
    assert "secret-password" not in response.text


def test_readiness_success(client):
    with patch("app.api.routes.check_database", return_value=True):
        assert client.get("/ready").json()["status"] == "ready"


@pytest.mark.parametrize(
    "origin",
    ["*", "https://*.example.com", "http://localhost:3000/path", "https://user:pass@example.com"],
)
def test_unsafe_cors_rejected(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_origins=[origin])


def test_secure_defaults_and_secret_repr(monkeypatch):
    # Defaults must be tested without inherited deployment/CI configuration.
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    config = Settings(_env_file=None)
    assert config.cors_origins == []
    assert config.database_url is None
    secret = Settings(_env_file=None, database_url="postgresql+psycopg://user:secret@localhost/db")
    assert "secret@" not in repr(secret)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")


def test_correlation_id_and_logs(client):
    correlation = str(uuid.uuid4())
    with patch("app.main.logger.info") as log:
        response = client.get("/health?token=private", headers={"X-Correlation-ID": correlation})
    assert response.headers["X-Correlation-ID"] == correlation
    emitted = log.call_args.args[0]
    assert "private" not in emitted
    assert correlation in emitted
    assert '"route": "/health"' in emitted


def test_safe_unhandled_error(client):
    @client.app.get("/test-error")
    def fail():
        raise RuntimeError("private")

    response = client.get("/test-error")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_server_error"
    assert "private" not in response.text
