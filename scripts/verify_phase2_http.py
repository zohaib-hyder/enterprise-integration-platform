"""Real authenticated BFF connector checks. Keeps synthetic fixtures; prints no secrets.

Run from backend with both local servers running. Phase 1 verifier creates private
operator credentials if needed. The seed is executed twice through its real CLI.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
from app.core.config import Settings
from app.db.session import build_engine
from app.models.identity import AuditEvent, AuthSession, User
from app.models.integration import (
    IntegrationConnection,
    SyntheticATSRecord,
    SyntheticCalendarEvent,
    SyntheticCRMRecord,
    SyntheticEmail,
    SyntheticNotification,
)
from app.models.tenant import Tenant
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
    engine = build_engine(Settings())
    assert engine is not None
    seed_env = {
        **os.environ,
        "BOOTSTRAP_EMAIL": credentials["email"],
        "BOOTSTRAP_PASSWORD": credentials["password"],
        "BOOTSTRAP_DISPLAY_NAME": "Synthetic HTTP Administrator",
    }
    models = [
        IntegrationConnection,
        SyntheticCRMRecord,
        SyntheticATSRecord,
        SyntheticNotification,
        SyntheticEmail,
        SyntheticCalendarEvent,
    ]
    seed_counts = []
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-m", "app.seed_demo"],
            cwd=backend,
            env=seed_env,
            check=True,
            capture_output=True,
        )
        with Session(engine) as db:
            tenant_ids = db.scalars(
                select(Tenant.id).where(
                    Tenant.slug.in_(["demo-recruiting-alpha", "demo-recruiting-beta"])
                )
            ).all()
            seed_counts.append(
                [
                    db.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.tenant_id.in_(tenant_ids))
                    )
                    for model in models
                ]
            )
    assert seed_counts[0] == seed_counts[1]
    assert all(
        actual >= expected
        for actual, expected in zip(seed_counts[0], [10, 10, 10, 2, 2, 4], strict=True)
    )
    origin = "http://127.0.0.1:3000"
    sensitive = [credentials["password"], credentials["outsider_password"]]
    with httpx.Client(base_url=origin, timeout=20) as client:

        def mutate(method: str, path: str, data: dict | None = None) -> httpx.Response:
            response = client.get("/api/platform/auth/csrf")
            assert response.status_code == 200
            token = response.json()["csrf_token"]
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
        tenants = signed_in.json()["tenants"]
        alpha = next(t for t in tenants if t["slug"] == credentials["slug_a"])
        beta = next(t for t in tenants if t["slug"] == credentials["slug_b"])
        assert (
            mutate("POST", "tenant/switch", {"tenant_id": alpha["id"]}).status_code
            == 200
        )
        definitions = client.get("/api/platform/connector-definitions")
        assert definitions.status_code == 200 and len(definitions.json()) == 6
        rest = next(
            d for d in definitions.json() if d["connector_type"] == "generic_rest"
        )
        assert rest["enabled"] is False and rest["operations"] == []
        assert client.post("/api/platform/connections", json={}).status_code == 403
        assert (
            mutate(
                "POST",
                "connections",
                {
                    "name": "Unsafe config",
                    "connector_type": "synthetic_crm",
                    "config": {"api_key": "forbidden-provider-secret"},
                },
            ).status_code
            == 422
        )
        rows = client.get("/api/platform/connections?offset=0&limit=100").json()[
            "items"
        ]
        connections = {}
        for kind in ["crm", "ats", "slack", "email", "calendar"]:
            name = f"HTTP Phase 2 {kind.upper()}"
            row = next((r for r in rows if r["name"] == name), None)
            if row is None:
                response = mutate(
                    "POST",
                    "connections",
                    {"name": name, "connector_type": f"synthetic_{kind}"},
                )
                assert response.status_code == 201
                row = response.json()
            connection = row["id"]
            connections[kind] = connection
            assert (
                mutate(
                    "PATCH", f"connections/{connection}", {"enabled": True}
                ).status_code
                == 200
            )
            assert (
                mutate("POST", f"connections/{connection}/health").json()["status"]
                == "HEALTHY"
            )
            assert (
                client.get(
                    f"/api/platform/connections/{connection}/operations"
                ).status_code
                == 200
            )

        def execute(kind: str, name: str, payload: dict, expected: int = 200) -> dict:
            response = mutate(
                "POST",
                f"connections/{connections[kind]}/execute",
                {"schema_version": 1, "name": name, "payload": payload},
            )
            assert response.status_code == expected, (
                f"{kind} {name}: unexpected {response.status_code}"
            )
            return response.json()

        for kind, entity in [("crm", "contact"), ("ats", "candidate")]:
            data = {
                "name": "Synthetic HTTP Record",
                "email": "http-record@example.test",
            }
            data.update(
                {"company": "Fictional Harbor"}
                if kind == "crm"
                else {"stage": "NEW", "skills": ["Python"]}
            )
            record = execute(
                kind, entity + ".upsert", {"external_id": "http-record-1", "data": data}
            )["data"]
            assert (
                execute(kind, entity + ".get", {"id": record["id"]})["data"]["id"]
                == record["id"]
            )
            assert (
                execute(kind, entity + ".list", {"offset": 0, "limit": 1})["data"][
                    "total"
                ]
                >= 1
            )
        for kind, payload in [
            (
                "slack",
                {
                    "channel": "recruiting",
                    "text": "Synthetic HTTP message",
                    "idempotency_key": "http-slack-key-1",
                },
            ),
            (
                "email",
                {
                    "recipient": "recruiter@example.test",
                    "subject": "Synthetic HTTP",
                    "body": "Local only",
                    "idempotency_key": "http-email-key-1",
                },
            ),
        ]:
            first = execute(kind, "message.send", payload)["data"]
            second = execute(kind, "message.send", payload)["data"]
            assert first["id"] == second["id"] and second["duplicate"] is True
            assert second["status"] == "SIMULATED"
        event_payload = {
            "calendar": "interviews",
            "title": "Synthetic HTTP interview",
            "starts_at": "2031-01-20T15:00:00Z",
            "ends_at": "2031-01-20T16:00:00Z",
            "idempotency_key": "http-event-" + uuid.uuid4().hex,
        }
        event = execute("calendar", "event.create", event_payload)["data"]
        assert (
            execute("calendar", "event.create", event_payload)["data"]["duplicate"]
            is True
        )
        execute(
            "calendar",
            "event.create",
            {**event_payload, "idempotency_key": "http-conflict-" + uuid.uuid4().hex},
            409,
        )
        update = {k: v for k, v in event_payload.items() if k != "idempotency_key"}
        assert (
            execute(
                "calendar",
                "event.update",
                {
                    **update,
                    "id": event["id"],
                    "title": "Updated synthetic HTTP interview",
                },
            )["data"]["id"]
            == event["id"]
        )
        assert (
            execute("calendar", "event.cancel", {"id": event["id"]})["data"]["status"]
            == "CANCELLED"
        )
        for connection in connections.values():
            response = client.get(
                f"/api/platform/connections/{connection}/records?offset=0&limit=25"
            )
            assert response.status_code == 200 and response.json()["total"] >= 1
            assert (
                "idempotency_key" not in response.text
                and "request_hash" not in response.text
            )
        crm = connections["crm"]
        assert (
            mutate("PATCH", f"connections/{crm}", {"enabled": False}).json()["status"]
            == "DISABLED"
        )
        execute("crm", "contact.list", {}, 403)
        assert (
            mutate("PATCH", f"connections/{crm}", {"enabled": True}).status_code == 200
        )
        assert (
            mutate("POST", "tenant/switch", {"tenant_id": beta["id"]}).status_code
            == 200
        )
        for connection in connections.values():
            assert (
                client.get(f"/api/platform/connections/{connection}").status_code == 404
            )
            assert (
                client.get(
                    f"/api/platform/connections/{connection}/records"
                ).status_code
                == 404
            )
            assert (
                mutate(
                    "POST",
                    f"connections/{connection}/execute",
                    {"name": "contact.list"},
                ).status_code
                == 404
            )
        assert mutate("POST", "auth/logout").status_code == 200
        assert client.get("/api/platform/connections").status_code == 401
        assert client.get("/connections").status_code == 200
    with Session(engine) as db:
        sensitive.extend(db.scalars(select(User.password_hash)).all())
        sensitive.extend(db.scalars(select(AuthSession.token_hash)).all())
        audit = json.dumps([row.details for row in db.scalars(select(AuditEvent))])
        assert not any(value in audit for value in sensitive)
    for name in ["http-backend.log", "http-frontend.log"]:
        log = backend / ".local" / name
        if log.exists():
            content = log.read_text()
            assert not any(value in content for value in sensitive)
            assert "forbidden-provider-secret" not in content
    engine.dispose()
    print(
        "Phase 2 BFF checks passed: five connectors, durable records, delivery idempotency, calendar conflicts, tenant denial, CSRF, enable/disable and redaction."
    )
    print(
        f"Seed CLI repeatability verified; demo counts (connections/CRM/ATS/Slack/email/calendar): {seed_counts[0]}"
    )


if __name__ == "__main__":
    main()
