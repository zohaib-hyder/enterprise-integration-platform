"""Phase 11 direct-backend boundaries and adversarial tenant route matrix."""

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_identity import login

from app.core.config import Settings
from app.core.security import token_hash, utcnow
from app.models.identity import AuthSession, User


@pytest.mark.parametrize(
    "hosts",
    [
        [],
        ["*"],
        ["*.example.test"],
        ["https://example.test"],
        ["example.test/path"],
        ["localhost:8000"],
        ["evil\n.test"],
    ],
)
def test_host_configuration_rejects_unsafe_values(hosts):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, allowed_hosts=hosts)


def test_host_validation_and_headers(identity_env):
    client = identity_env["client"]
    assert client.get("/health", headers={"host": "evil.example.test"}).status_code == 400
    for path in ["/health", "/api/v1/system/info", "/api/v1/ai/analyses"]:
        response = client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        if path.startswith("/api/"):
            assert "default-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("chunked", [False, True])
def test_direct_backend_body_bounded_before_auth_or_decoding(identity_env, chunked):
    client = identity_env["client"]
    body = b"x" * 16385
    content = (part for part in [body[:8192], body[8192:]]) if chunked else body
    response = client.post(
        "/api/v1/auth/login", content=content, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert "x" * 100 not in response.text


def test_compressed_input_rejected(identity_env):
    response = identity_env["client"].post(
        "/api/v1/auth/login",
        content=b"compressed",
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )
    assert response.status_code == 415


@pytest.mark.parametrize(
    "route",
    [
        "connections/{id}",
        "connections/{id}/credentials",
        "connections/{id}/circuit",
        "webhook-endpoints/{id}",
        "webhook-endpoints/{id}/receipts",
        "events/{id}",
        "mappings/{id}",
        "mappings/{id}/versions",
        "executions/{id}",
        "executions/{id}/history",
        "sync/plans/{id}",
        "sync/executions/{id}",
        "sync/conflicts/{id}",
        "notifications/intents/{id}",
        "notifications/deliveries/{id}",
        "notifications/deliveries/{id}/attempts",
        "calendar/events/{id}",
        "calendar/requests/{id}",
        "operations/incidents/{id}",
        "operations/incidents/{id}/activity",
        "operations/recovery/requests/{id}",
        "ai/analyses/{id}",
        "ai/analyses/{id}/evidence",
        "ai/recommendations/{id}/reviews",
    ],
)
def test_guessed_uuid_cannot_expose_nested_domains(identity_env, route):
    env = identity_env
    assert login(env).status_code == 200
    response = env["client"].get("/api/v1/" + route.format(id=uuid.uuid4()))
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("mode", ["expired", "revoked", "deactivated"])
def test_unauthorized_session_reuse(identity_env, db, mode):
    env = identity_env
    login(env)
    raw = env["client"].cookies["platform_session"]
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw)))
    if mode == "expired":
        session.expires_at = utcnow() - timedelta(seconds=1)
    elif mode == "revoked":
        session.revoked_at = utcnow()
    else:
        db.get(User, session.user_id).is_active = False
    db.commit()
    for path in ["auth/me", "connections", "operations/incidents", "ai/analyses"]:
        assert env["client"].get("/api/v1/" + path).status_code == 401


def test_webhook_abuse_limit_is_bounded_and_does_not_ingest():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.http_security import RequestBodyLimit

    app = FastAPI()
    calls = []

    @app.post("/api/v1/webhooks/ingest/{public_id}")
    def destination(public_id: str):
        calls.append(public_id)
        return {"received": True}

    app.add_middleware(RequestBodyLimit, webhook_limit=2)
    with TestClient(app) as client:
        assert client.post("/api/v1/webhooks/ingest/one", json={}).status_code == 200
        assert client.post("/api/v1/webhooks/ingest/two", json={}).status_code == 200
        response = client.post("/api/v1/webhooks/ingest/three", json={})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert calls == ["one", "two"]
