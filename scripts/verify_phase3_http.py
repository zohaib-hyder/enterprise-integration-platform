"""Real BFF endpoint management + signed local HTTP ingestion. No credential output.

Run from backend with both local servers running. Creates only synthetic endpoints
and events; keeps them for inspection. One-time signing secrets stay in memory.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
from app.connectors.webhook_sender import event_bytes, signed_request
from app.core.config import Settings
from app.db.session import build_engine
from app.models.identity import AuditEvent
from app.models.webhook import EventReceipt, IncomingEvent, WebhookSecretVersion
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main() -> None:
    backend = Path(__file__).resolve().parents[1] / "backend"
    credential_file = backend / ".local/http-verification.json"
    if not credential_file.exists():
        subprocess.run(
            [sys.executable, "../scripts/verify_phase1_http.py"],
            cwd=backend,
            check=True,
        )
    credentials = json.loads(credential_file.read_text())
    origin = "http://127.0.0.1:3000"
    sensitive = [credentials["password"], credentials["outsider_password"]]
    with httpx.Client(base_url=origin, timeout=20) as client:

        def mutate(method: str, path: str, data: dict | None = None) -> httpx.Response:
            csrf = client.get("/api/platform/auth/csrf")
            assert csrf.status_code == 200
            token = csrf.json()["csrf_token"]
            sensitive.append(token)
            return client.request(
                method,
                "/api/platform/" + path,
                json=data or {},
                headers={"Origin": origin, "X-CSRF-Token": token},
            )

        signed_in = mutate(
            "POST",
            "auth/login",
            {"email": credentials["email"], "password": credentials["password"]},
        )
        assert signed_in.status_code == 200
        sensitive.extend(cookie.value for cookie in client.cookies.jar)
        alpha = next(
            t for t in signed_in.json()["tenants"] if t["slug"] == credentials["slug_a"]
        )
        beta = next(
            t for t in signed_in.json()["tenants"] if t["slug"] == credentials["slug_b"]
        )
        assert (
            mutate("POST", "tenant/switch", {"tenant_id": alpha["id"]}).status_code
            == 200
        )
        connections = client.get("/api/platform/connections?offset=0&limit=100").json()[
            "items"
        ]
        connection = next(
            (
                r
                for r in connections
                if r["connector_type"] == "synthetic_crm" and r["status"] == "ENABLED"
            ),
            None,
        )
        if connection is None:
            created = mutate(
                "POST",
                "connections",
                {"connector_type": "synthetic_crm", "name": "HTTP Phase 3 CRM"},
            )
            assert created.status_code == 201
            connection = created.json()
        assert (
            client.post("/api/platform/webhook-endpoints", json={}).status_code == 403
        )
        created = mutate(
            "POST",
            "webhook-endpoints",
            {"connection_id": connection["id"], "source": "synthetic_crm"},
        )
        assert created.status_code == 201
        disclosure = created.json()
        endpoint, original = disclosure["endpoint"], disclosure["signing_secret"]
        sensitive.append(original)
        assert created.headers.get("cache-control") == "no-store"
        base = f"webhook-endpoints/{endpoint['id']}"
        for path in ["webhook-endpoints", base, base + "/status", base + "/receipts"]:
            response = client.get("/api/platform/" + path)
            assert response.status_code == 200 and original not in response.text
            assert "ciphertext" not in response.text
        target = "http://127.0.0.1:8000/api/v1/webhooks/ingest/" + endpoint["public_id"]
        body = event_bytes("synthetic_crm", "job.created", "real-http-event-1")

        def send(
            secret: str, content: bytes = body, scenario: str = "valid"
        ) -> httpx.Response:
            raw, headers = signed_request(secret, content, scenario)
            return httpx.post(
                target, content=raw, headers=headers, timeout=10, trust_env=False
            )

        accepted = send(original)
        duplicate = send(original)
        assert accepted.status_code == 202 and duplicate.status_code == 200
        event_id = accepted.json()["event_id"]
        assert duplicate.json()["event_id"] == event_id
        for scenario in ["invalid_signature", "stale", "altered_payload"]:
            assert send(original, scenario=scenario).status_code == 401
        changed = json.loads(body)
        changed["data"]["title"] = "Conflicting synthetic job"
        assert send(original, json.dumps(changed).encode()).status_code == 409
        assert send(original, b"{").status_code == 422
        rotation = mutate("POST", base + "/rotate-secret", {"grace_seconds": 60})
        assert rotation.status_code == 200
        current = rotation.json()["signing_secret"]
        sensitive.append(current)
        assert current != original and rotation.json()["secret_version"] == 2
        assert send(original).status_code == 200 and send(current).status_code == 200
        retired = mutate("POST", base + "/rotate-secret", {"grace_seconds": 0})
        assert retired.status_code == 200
        newest = retired.json()["signing_secret"]
        sensitive.append(newest)
        assert send(original).status_code == 401 and send(current).status_code == 401
        assert send(newest).status_code == 200
        # Exercise the real CLI with credentials supplied only to its private child environment.
        command = [
            sys.executable,
            "-m",
            "app.connectors.webhook_sender",
            "--target",
            target,
            "--source",
            "synthetic_crm",
            "--event-type",
            "job.updated",
            "--event-id",
            "real-cli-event-2",
            "--scenario",
            "duplicate",
        ]
        completed = subprocess.run(
            command,
            cwd=backend,
            env={**os.environ, "WEBHOOK_SIGNING_SECRET": newest},
            capture_output=True,
            text=True,
            check=True,
        )
        output = [json.loads(line) for line in completed.stdout.splitlines()]
        assert [row["http_status"] for row in output] == [202, 200]
        assert (
            output[0]["event_id"] == output[1]["event_id"]
            and newest not in completed.stdout
        )
        query = "events?source=synthetic_crm&event_type=job.created&status=PENDING&since=2020-01-01T00%3A00%3A00Z&limit=100"
        inbox = client.get("/api/platform/" + query)
        assert inbox.status_code == 200 and any(
            row["id"] == event_id for row in inbox.json()["items"]
        )
        detail = client.get("/api/platform/events/" + event_id)
        assert detail.status_code == 200 and detail.json()["duplicate_count"] == 4
        assert (
            detail.json()["payload"]["data"]["title"] == "Synthetic Webhook Requisition"
        )
        history = client.get("/api/platform/" + base + "/receipts?offset=0&limit=100")
        assert history.status_code == 200 and history.json()["total"] == 9
        health = client.get("/api/platform/" + base + "/status").json()
        assert health["receipt_counts"] == {
            "ACCEPTED": 2,
            "DUPLICATE": 5,
            "CONFLICT": 1,
            "REJECTED": 1,
        }
        assert client.get("/events").status_code == 200
        assert (
            mutate("POST", "tenant/switch", {"tenant_id": beta["id"]}).status_code
            == 200
        )
        for path in [
            base,
            base + "/receipts",
            "events/" + event_id,
            "events/" + event_id + "/receipts",
        ]:
            assert client.get("/api/platform/" + path).status_code == 404
        assert mutate("POST", base + "/rotate-secret", {}).status_code == 404
        assert (
            mutate("POST", "tenant/switch", {"tenant_id": alpha["id"]}).status_code
            == 200
        )
        assert mutate("PATCH", base, {"enabled": False}).status_code == 200
        assert send(newest).status_code == 401
        assert mutate("POST", "auth/logout").status_code == 200
        assert client.get("/api/platform/events").status_code == 401
    engine = build_engine(Settings())
    assert engine is not None
    with Session(engine) as db:
        endpoint_id = uuid.UUID(endpoint["id"])
        assert (
            db.scalar(
                select(func.count())
                .select_from(IncomingEvent)
                .where(IncomingEvent.endpoint_id == endpoint_id)
            )
            == 2
        )
        keys = db.scalars(
            select(WebhookSecretVersion).where(
                WebhookSecretVersion.endpoint_id == endpoint_id
            )
        ).all()
        assert len(keys) == 3 and all(
            secret.encode() not in key.ciphertext
            for key in keys
            for secret in sensitive
        )
        audit = json.dumps([row.details for row in db.scalars(select(AuditEvent))])
        assert not any(secret in audit for secret in sensitive)
        receipt_total = db.scalar(
            select(func.count())
            .select_from(EventReceipt)
            .where(EventReceipt.endpoint_id == endpoint_id)
        )
        assert receipt_total == 9
    settings = Settings()
    sensitive.append(settings.webhook_encryption_key.get_secret_value())
    for name in ["http-backend.log", "http-frontend.log"]:
        log = backend / ".local" / name
        if log.exists():
            assert not any(secret in log.read_text() for secret in sensitive)
    engine.dispose()
    print(
        "Phase 3 real HTTP passed: BFF endpoint creation; signed ingress; durable duplicate/conflict handling; stale/tampered rejection; bounded rotation; actual inbox filters/details/history; tenant denial; CLI compatibility; redaction."
    )
    print(
        "Verification endpoint is disabled; two accepted synthetic events remain pending. No signing secrets were written to disk or printed."
    )


if __name__ == "__main__":
    main()
