import base64
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_connectors import create
from test_identity import login, mutation

from app.connectors.webhook_sender import deliver, event_bytes, local_target, signed_request
from app.core.config import Settings
from app.core.security import utcnow
from app.core.webhook_security import IngestionError, seal, signature, unseal
from app.main import create_app
from app.models.integration import IntegrationConnection
from app.models.tenant import Tenant
from app.models.webhook import EventReceipt, IncomingEvent, WebhookEndpoint, WebhookSecretVersion


@pytest.fixture
def hook_env(identity_env):
    env = identity_env
    settings = env["app"].state.settings
    settings.webhook_encryption_key = SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode())
    assert login(env).status_code == 200
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}
        ).status_code
        == 200
    )
    env["conn"] = create(env)
    response = mutation(
        env["client"],
        "POST",
        "webhook-endpoints",
        {"connection_id": env["conn"], "source": "synthetic_crm"},
    )
    assert response.status_code == 201, response.text
    env["hook"] = response.json()["endpoint"]
    env["secret"] = response.json()["signing_secret"]
    return env


def send(env, body=None, secret=None, timestamp=None, headers=None, public_id=None):
    body = (
        body
        if body is not None
        else event_bytes("synthetic_crm", "job.created", "provider-event-1")
    )
    timestamp = timestamp or str(int(time.time()))
    verification = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": signature(secret or env["secret"], timestamp, body),
    }
    verification.update(headers or {})
    return env["client"].post(
        "/api/v1/webhooks/ingest/" + (public_id or env["hook"]["public_id"]),
        content=body,
        headers=verification,
    )


def count(db, model):
    tenant = db.scalar(select(Tenant.id).where(Tenant.slug == "synthetic-alpha"))
    query = select(func.count()).select_from(model)
    return db.scalar(query.where(model.tenant_id == tenant) if tenant else query)


def test_valid_hmac_persists_before_ack_and_has_no_csrf_requirement(hook_env, db):
    env = hook_env
    env["client"].cookies.clear()
    response = send(
        env, headers={"X-Tenant-ID": str(env["b"].id), "Cookie": "platform_session=forged"}
    )
    assert response.status_code == 202
    assert response.json()["processing_status"] == "PENDING"
    event = db.get(IncomingEvent, uuid.UUID(response.json()["event_id"]))
    assert event.tenant_id == env["a"].id and event.connection_id == uuid.UUID(env["conn"])
    assert event.ingestion_status == "ACCEPTED" and event.processing_status == "PENDING"
    assert event.received_at.tzinfo and event.occurred_at.tzinfo
    assert count(db, EventReceipt) == 1


@pytest.mark.parametrize("scenario", ["invalid_signature", "altered_payload", "stale"])
def test_invalid_signed_scenarios_not_trusted(hook_env, db, scenario):
    body = event_bytes("synthetic_crm", "job.created", "provider-event-1")
    body, headers = signed_request(hook_env["secret"], body, scenario)
    response = hook_env["client"].post(
        "/api/v1/webhooks/ingest/" + hook_env["hook"]["public_id"], content=body, headers=headers
    )
    assert response.status_code == 401
    assert count(db, IncomingEvent) == count(db, EventReceipt) == 0


@pytest.mark.parametrize("offset", [-301, 301, -3600, 3600])
def test_timestamp_freshness(hook_env, db, offset):
    assert send(hook_env, timestamp=str(int(time.time()) + offset)).status_code == 401
    assert count(db, EventReceipt) == 0


def test_timestamp_tampering(hook_env):
    body = event_bytes("synthetic_crm", "job.created", "provider-event-1")
    timestamp = str(int(time.time()))
    assert (
        send(
            hook_env,
            body,
            timestamp=str(int(timestamp) + 1),
            headers={"X-Webhook-Signature": signature(hook_env["secret"], timestamp, body)},
        ).status_code
        == 401
    )


@pytest.mark.parametrize("timestamp", ["", "-1", "1.5", "NaN", "0" * 100, "not-a-time"])
def test_bad_timestamp(hook_env, timestamp):
    assert send(hook_env, headers={"X-Webhook-Timestamp": timestamp}).status_code == 401


@pytest.mark.parametrize("supplied", ["", "sha1=abc", "v1=" + "z" * 64, "v1=" + "a" * 63])
def test_signature_format(hook_env, supplied):
    assert send(hook_env, headers={"X-Webhook-Signature": supplied}).status_code == 401


def test_unknown_and_disabled_endpoint(hook_env, db):
    env = hook_env
    unknown = send(env, public_id="A" * 43)
    assert unknown.status_code == 401 and "tenant" not in unknown.text
    assert (
        mutation(
            env["client"], "PATCH", "webhook-endpoints/" + env["hook"]["id"], {"enabled": False}
        ).status_code
        == 200
    )
    assert send(env).status_code == 401 and count(db, IncomingEvent) == 0


def test_disabled_connection_denies_ingress(hook_env):
    env = hook_env
    assert (
        mutation(
            env["client"], "PATCH", "connections/" + env["conn"], {"enabled": False}
        ).status_code
        == 200
    )
    assert send(env).status_code == 401


def test_rotation_grace_and_expiry(hook_env, db):
    env = hook_env
    rotated = mutation(
        env["client"],
        "POST",
        "webhook-endpoints/" + env["hook"]["id"] + "/rotate-secret",
        {"grace_seconds": 60},
    )
    assert rotated.status_code == 200
    assert rotated.json()["secret_version"] == 2
    assert send(env).status_code == 202
    assert send(env, secret=rotated.json()["signing_secret"]).status_code == 200
    previous = db.scalar(select(WebhookSecretVersion).where(WebhookSecretVersion.version == 1))
    previous.valid_until = utcnow() - timedelta(seconds=1)
    db.flush()
    assert send(env).status_code == 401
    assert send(env, secret=rotated.json()["signing_secret"]).status_code == 200


def test_multiple_rotations_retain_only_bounded_previous_version(hook_env):
    env = hook_env
    path = "webhook-endpoints/" + env["hook"]["id"] + "/rotate-secret"
    second = mutation(env["client"], "POST", path, {"grace_seconds": 900}).json()
    third = mutation(env["client"], "POST", path, {"grace_seconds": 900}).json()
    assert send(env).status_code == 401
    assert send(env, secret=second["signing_secret"]).status_code == 202
    assert send(env, secret=third["signing_secret"]).status_code == 200
    assert mutation(env["client"], "POST", path, {"grace_seconds": 901}).status_code == 422


def test_zero_grace_immediately_retires_previous_secret(hook_env):
    response = mutation(
        hook_env["client"],
        "POST",
        "webhook-endpoints/" + hook_env["hook"]["id"] + "/rotate-secret",
        {"grace_seconds": 0},
    )
    assert response.status_code == 200 and send(hook_env).status_code == 401
    assert send(hook_env, secret=response.json()["signing_secret"]).status_code == 202


def test_missing_encryption_key_fails_closed(hook_env):
    env = hook_env
    env["app"].state.settings.webhook_encryption_key = None
    assert send(env).status_code == 503
    assert (
        mutation(
            env["client"],
            "POST",
            "webhook-endpoints",
            {"connection_id": env["conn"], "source": "synthetic_crm"},
        ).status_code
        == 503
    )
    assert (
        mutation(
            env["client"], "POST", "webhook-endpoints/" + env["hook"]["id"] + "/rotate-secret", {}
        ).status_code
        == 503
    )
    assert env["client"].get("/health").status_code == 200


@pytest.mark.parametrize(
    "key",
    [
        "",
        "secret",
        "!" * 44,
        base64.urlsafe_b64encode(b"x" * 31).decode(),
        base64.urlsafe_b64encode(b"x" * 33).decode(),
    ],
)
def test_invalid_encryption_configuration_is_safe(key):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, webhook_encryption_key=key)
    assert "input_value" not in str(error.value)


def test_ciphertext_and_context_binding(hook_env, db):
    row = db.scalar(select(WebhookSecretVersion))
    settings = hook_env["app"].state.settings
    assert hook_env["secret"].encode() not in row.ciphertext
    assert unseal(settings, row).decode() == hook_env["secret"]
    with patch.object(row, "tenant_id", uuid.uuid4()), pytest.raises(IngestionError):
        unseal(settings, row)
    settings.webhook_encryption_key = SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode())
    assert send(hook_env).status_code == 503
    assert (
        mutation(
            hook_env["client"],
            "POST",
            "webhook-endpoints/" + hook_env["hook"]["id"] + "/rotate-secret",
            {},
        ).status_code
        == 503
    )


@pytest.mark.parametrize(
    "body,category",
    [
        (b"{", "MALFORMED_JSON"),
        (b"{}", "INVALID_SOURCE"),
        (b"[]", "INVALID_SCHEMA"),
        (b'{"source":"unknown"}', "INVALID_SOURCE"),
        (b'{"source":"synthetic_ats"}', "INVALID_SOURCE"),
        (b'{"source":"synthetic_crm"}', "INVALID_SCHEMA"),
        (b'{"source":"synthetic_crm","source":"synthetic_crm"}', "MALFORMED_JSON"),
        (b"\xff", "MALFORMED_JSON"),
        (b'{"x":NaN}', "MALFORMED_JSON"),
    ],
)
def test_signed_invalid_schema_records_only_sanitized_receipt(hook_env, db, body, category):
    response = send(hook_env, body=body)
    assert response.status_code == 422 and response.json()["category"] == category
    assert count(db, IncomingEvent) == 0
    row = db.scalar(select(EventReceipt))
    assert row.outcome == "REJECTED" and row.event_id is None and row.error_category == category
    assert not hasattr(row, "payload")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", None),
        ("event_id", ""),
        ("occurred_at", "2026-10-13T00:00:00"),
        ("tenant_id", "arbitrary"),
        ("event_type", "candidate.created"),
        ("event_type", "arbitrary.run"),
        ("data", {"api_key": "do-not-echo-secret"}),
    ],
)
def test_event_schema_rejects_unallowed_fields(hook_env, field, value):
    data = json.loads(event_bytes("synthetic_crm", "job.created", "provider-event-1"))
    data[field] = value
    response = send(hook_env, body=json.dumps(data).encode())
    assert response.status_code == 422 and "do-not-echo-secret" not in response.text


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/xml", "application/json;charset=latin1", ""]
)
def test_unsupported_content_type(hook_env, db, content_type):
    assert send(hook_env, headers={"Content-Type": content_type}).status_code == 415
    assert count(db, EventReceipt) == 0


def test_oversized_body_declared_and_streamed(hook_env, db):
    assert send(hook_env, body=b"x" * 32769).status_code == 413
    response = hook_env["client"].post(
        "/api/v1/webhooks/ingest/" + hook_env["hook"]["public_id"],
        content=iter([b"x" * 20000, b"y" * 20000]),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413 and count(db, IncomingEvent) == 0


def test_identical_duplicate_conflict_and_distinct_identity(hook_env, db):
    first = send(hook_env)
    duplicate = send(hook_env)
    assert first.status_code == 202 and duplicate.status_code == 200
    assert first.json()["event_id"] == duplicate.json()["event_id"]
    # Canonical payload identity tolerates JSON whitespace/key order; signature does not.
    data = json.loads(event_bytes("synthetic_crm", "job.created", "provider-event-1"))
    assert send(hook_env, body=json.dumps(data, indent=2).encode()).status_code == 200
    data["data"]["title"] = "Different synthetic title"
    assert send(hook_env, body=json.dumps(data).encode()).status_code == 409
    data["event_id"] = "provider-event-2"
    assert send(hook_env, body=json.dumps(data).encode()).status_code == 202
    assert count(db, IncomingEvent) == 2
    assert count(db, EventReceipt) == 5
    detail = hook_env["client"].get("/api/v1/events/" + first.json()["event_id"]).json()
    assert detail["duplicate_count"] == 2


@pytest.mark.parametrize("role", ["TENANT_ADMIN", "INTEGRATION_ENGINEER", "OPERATIONS_USER"])
def test_management_rbac_and_read_scope(hook_env, role):
    env = hook_env
    assert send(env).status_code == 202
    mutation(env["client"], "POST", "auth/logout")
    assert login(env, role).status_code == 200
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}
        ).status_code
        == 200
    )
    for path in [
        "events",
        "webhook-endpoints",
        "webhook-endpoints/" + env["hook"]["id"],
        "webhook-endpoints/" + env["hook"]["id"] + "/receipts",
        "webhook-endpoints/" + env["hook"]["id"] + "/status",
    ]:
        assert env["client"].get("/api/v1/" + path).status_code == 200
    expected = 403 if role == "OPERATIONS_USER" else 200
    assert (
        mutation(
            env["client"], "POST", "webhook-endpoints/" + env["hook"]["id"] + "/rotate-secret", {}
        ).status_code
        == expected
    )
    assert (
        mutation(
            env["client"], "PATCH", "webhook-endpoints/" + env["hook"]["id"], {"enabled": False}
        ).status_code
        == expected
    )
    result = mutation(
        env["client"],
        "POST",
        "webhook-endpoints",
        {"connection_id": env["conn"], "source": "synthetic_crm"},
    )
    assert result.status_code == (403 if role == "OPERATIONS_USER" else 201)


def test_cross_tenant_events_endpoints_and_connection_ids(hook_env):
    env = hook_env
    event = send(env).json()["event_id"]
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)}
        ).status_code
        == 200
    )
    for path in [
        "webhook-endpoints/" + env["hook"]["id"],
        "webhook-endpoints/" + env["hook"]["id"] + "/receipts",
        "events/" + event,
        "events/" + event + "/receipts",
    ]:
        assert env["client"].get("/api/v1/" + path).status_code == 404
    assert env["client"].get("/api/v1/events").json()["total"] == 0
    assert (
        mutation(
            env["client"],
            "POST",
            "webhook-endpoints",
            {"connection_id": env["conn"], "source": "synthetic_crm"},
        ).status_code
        == 404
    )
    assert (
        mutation(
            env["client"], "POST", "webhook-endpoints/" + env["hook"]["id"] + "/rotate-secret", {}
        ).status_code
        == 404
    )


def test_secret_and_payload_redaction_in_reads_and_logs(hook_env, db):
    env = hook_env
    ats = create(env, "synthetic_ats", name="Synthetic ATS")
    disclosure = mutation(
        env["client"],
        "POST",
        "webhook-endpoints",
        {"connection_id": ats, "source": "synthetic_ats"},
    ).json()
    with patch("app.main.logger.info") as logs:
        response = send(
            env,
            body=event_bytes("synthetic_ats", "candidate.created", "candidate-event-1"),
            secret=disclosure["signing_secret"],
            public_id=disclosure["endpoint"]["public_id"],
        )
    assert response.status_code == 202
    for path in [
        "webhook-endpoints",
        "webhook-endpoints/" + disclosure["endpoint"]["id"],
        "events/" + response.json()["event_id"],
    ]:
        output = env["client"].get("/api/v1/" + path).text
        assert disclosure["signing_secret"] not in output and env["secret"] not in output
        assert "ciphertext" not in output and "nonce" not in output
        assert "webhook-candidate@example.test" not in output
    assert "[REDACTED]" in env["client"].get("/api/v1/events/" + response.json()["event_id"]).text
    assert disclosure["signing_secret"] not in str(logs.call_args_list)
    assert disclosure["endpoint"]["public_id"] not in str(logs.call_args_list)


def test_ingestion_commit_failure_rolls_back_without_success_ack(hook_env, db):
    with patch.object(Session, "commit", side_effect=RuntimeError("private db error")):
        response = send(hook_env)
    assert response.status_code == 500 and "private db error" not in response.text
    assert count(db, IncomingEvent) == count(db, EventReceipt) == 0


def test_csrf_origin_and_algorithm_preserved(hook_env):
    env = hook_env
    assert env["client"].post("/api/v1/webhook-endpoints", json={}).status_code == 403
    assert (
        mutation(
            env["client"],
            "POST",
            "webhook-endpoints",
            {"connection_id": env["conn"], "source": "synthetic_crm", "algorithm": "none"},
        ).status_code
        == 422
    )
    assert (
        mutation(
            env["client"],
            "POST",
            "webhook-endpoints",
            {"connection_id": env["conn"], "source": "synthetic_crm"},
            Origin="https://attacker.example.test",
        ).status_code
        == 403
    )


def test_filters_pagination_and_delivery_categories(hook_env):
    first = send(hook_env).json()["event_id"]
    assert send(hook_env).status_code == 200
    assert send(hook_env, body=b"{").status_code == 422
    client = hook_env["client"]
    assert (
        client.get(
            "/api/v1/events?source=synthetic_crm&event_type=job.created&status=PENDING&limit=1"
        ).json()["total"]
        == 1
    )
    assert client.get("/api/v1/events?source=synthetic_ats").json()["total"] == 0
    assert client.get("/api/v1/events?since=2099-01-01T00:00:00Z").json()["total"] == 0
    assert (
        client.get("/api/v1/events?since=2020-01-01T00:00:00Z&until=2021-01-01T00:00:00Z").json()[
            "total"
        ]
        == 0
    )
    assert (
        client.get(
            "/api/v1/events?since=2099-01-01T00:00:00Z&until=2020-01-01T00:00:00Z"
        ).status_code
        == 422
    )
    assert client.get("/api/v1/events?limit=101").status_code == 422
    assert client.get("/api/v1/events/" + first + "/receipts?offset=1&limit=1").json()["total"] == 2
    status = client.get("/api/v1/webhook-endpoints/" + hook_env["hook"]["id"] + "/status").json()
    assert status["receipt_counts"] == {"ACCEPTED": 1, "DUPLICATE": 1, "REJECTED": 1}
    assert status["error_counts"] == {"MALFORMED_JSON": 1}


@pytest.mark.parametrize(
    "scenario,statuses",
    [
        ("valid", [202]),
        ("duplicate", [202, 200]),
        ("invalid_signature", [401]),
        ("stale", [401]),
        ("altered_payload", [401]),
    ],
)
def test_authorized_local_simulation_uses_same_verifier(hook_env, scenario, statuses):
    response = mutation(
        hook_env["client"],
        "POST",
        "webhook-endpoints/" + hook_env["hook"]["id"] + "/simulate",
        {"event_type": "job.created", "event_id": "simulation-event-1", "scenario": scenario},
    )
    assert response.status_code == 200
    assert [row["http_status"] for row in response.json()["results"]] == statuses
    assert response.json()["delivery_mode"] == "local_in_process"
    assert hook_env["secret"] not in response.text


@pytest.mark.parametrize(
    "target",
    [
        "https://attacker.example.test",
        "http://169.254.169.254",
        "http://10.0.0.1:8000",
        "http://127.0.0.1:9000",
        "http://localhost:8000/admin",
        "http://user:secret@localhost:8000/api/v1/webhooks/ingest/" + "A" * 43,
        "http://localhost:8000/api/v1/webhooks/ingest/" + "A" * 43 + "?secret=bad",
    ],
)
def test_sender_target_allowlist_rejects_without_network(target):
    with patch("httpx.Client") as network, pytest.raises(ValueError):
        deliver(target, b"{}", {})
    network.assert_not_called()


def test_localhost_sender_pins_numeric_loopback_and_signs_raw_bytes():
    assert local_target("http://localhost:8000/api/v1/webhooks/ingest/" + "A" * 43).startswith(
        "http://127.0.0.1:8000/"
    )
    body = event_bytes("synthetic_ats", "candidate.updated", "provider-event-1")
    raw, headers = signed_request("synthetic-secret", body)
    assert headers["X-Webhook-Signature"] == signature(
        "synthetic-secret", headers["X-Webhook-Timestamp"], raw
    )


def test_concurrent_duplicate_ingestion_and_database_uniqueness(pg_engine):
    settings = Settings(
        _env_file=None, webhook_encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    tenant_id, connection_id, endpoint_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    public_id = "B" * 43
    secret, nonce, ciphertext = seal(settings, tenant_id, endpoint_id, 1)
    with Session(pg_engine) as db, db.begin():
        db.add(
            Tenant(
                id=tenant_id, name="Synthetic concurrent inbox", slug="inbox-" + uuid.uuid4().hex
            )
        )
        db.flush()
        db.add(
            IntegrationConnection(
                id=connection_id,
                tenant_id=tenant_id,
                connector_type="synthetic_crm",
                name="Concurrent CRM",
                public_config={"schema_version": 1},
            )
        )
        db.flush()
        db.add(
            WebhookEndpoint(
                id=endpoint_id,
                tenant_id=tenant_id,
                connection_id=connection_id,
                source="synthetic_crm",
                public_id=public_id,
            )
        )
        db.flush()
        db.add(
            WebhookSecretVersion(
                tenant_id=tenant_id,
                endpoint_id=endpoint_id,
                connection_id=connection_id,
                version=1,
                nonce=nonce,
                ciphertext=ciphertext,
            )
        )
    barrier = Barrier(2)
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.engine = pg_engine
        body, headers = signed_request(
            secret, event_bytes("synthetic_crm", "job.created", "race-event-1")
        )

        def run(_):
            barrier.wait()
            return client.post(
                "/api/v1/webhooks/ingest/" + public_id, content=body, headers=headers
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        assert sorted(r.status_code for r in results) == [200, 202]
        assert results[0].json()["event_id"] == results[1].json()["event_id"]
    with Session(pg_engine) as db:
        row = db.scalar(select(IncomingEvent).where(IncomingEvent.endpoint_id == endpoint_id))
        assert (
            db.scalar(
                select(func.count())
                .select_from(IncomingEvent)
                .where(IncomingEvent.endpoint_id == endpoint_id)
            )
            == 1
        )
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(
                IncomingEvent(
                    tenant_id=tenant_id,
                    endpoint_id=endpoint_id,
                    connection_id=connection_id,
                    source="synthetic_crm",
                    provider_event_id="race-event-1",
                    event_type="job.created",
                    payload={},
                    payload_digest="a" * 64,
                    occurred_at=utcnow(),
                    correlation_id=uuid.uuid4(),
                )
            )
            db.flush()
        with pytest.raises(IntegrityError), db.begin_nested():
            other = Tenant(name="Synthetic ownership denial", slug="other-" + uuid.uuid4().hex)
            db.add(other)
            db.flush()
            db.add(
                EventReceipt(
                    tenant_id=other.id,
                    endpoint_id=endpoint_id,
                    connection_id=connection_id,
                    event_id=row.id,
                    outcome="ACCEPTED",
                    payload_digest="a" * 64,
                    correlation_id=uuid.uuid4(),
                    duration_ms=1,
                )
            )
            db.flush()


def test_event_identity_scoped_to_endpoint_and_distinct_ids(hook_env, db):
    env = hook_env
    first = send(env)
    second_hook = mutation(
        env["client"],
        "POST",
        "webhook-endpoints",
        {"connection_id": env["conn"], "source": "synthetic_crm"},
    ).json()
    second = send(
        env, secret=second_hook["signing_secret"], public_id=second_hook["endpoint"]["public_id"]
    )
    distinct = send(
        env, body=event_bytes("synthetic_crm", "job.created", "different-event-same-business-data")
    )
    assert first.status_code == second.status_code == distinct.status_code == 202
    assert (
        len({first.json()["event_id"], second.json()["event_id"], distinct.json()["event_id"]}) == 3
    )
    assert count(db, IncomingEvent) == 3


def test_endpoint_credential_creation_commit_failure_never_discloses(hook_env, db):
    client = hook_env["client"]
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    before = count(db, WebhookEndpoint)
    with patch.object(Session, "commit", side_effect=RuntimeError("private db failure")):
        response = client.post(
            "/api/v1/webhook-endpoints",
            json={"connection_id": hook_env["conn"], "source": "synthetic_crm"},
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": token},
        )
    assert response.status_code == 500 and "signing_secret" not in response.text
    assert count(db, WebhookEndpoint) == before
