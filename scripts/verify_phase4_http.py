"""Real authenticated BFF canonical/mapping checks. Keeps synthetic evidence only."""

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from app.connectors.webhook_sender import event_bytes, signed_request
from app.core.config import Settings
from app.db.session import build_engine
from app.models.canonical import (
    CanonicalRecord,
    ExternalRecordMapping,
    QuarantinedRecord,
    TransformationAttempt,
    TransformationMapping,
    TransformationMappingVersion,
)
from app.models.identity import AuditEvent
from app.models.tenant import Tenant
from app.models.webhook import IncomingEvent
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main() -> None:
    backend = Path(__file__).resolve().parents[1] / "backend"
    credentials_file = backend / ".local/http-verification.json"
    if not credentials_file.exists():
        subprocess.run(
            [sys.executable, "../scripts/verify_phase1_http.py"],
            cwd=backend,
            check=True,
        )
    credentials = json.loads(credentials_file.read_text())
    engine = build_engine(Settings())
    assert engine is not None
    sensitive = [credentials["password"], credentials["outsider_password"]]
    seed_counts = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-m", "app.seed_demo"],
            cwd=backend,
            env={
                **os.environ,
                "BOOTSTRAP_EMAIL": credentials["email"],
                "BOOTSTRAP_PASSWORD": credentials["password"],
                "BOOTSTRAP_DISPLAY_NAME": "Synthetic HTTP Administrator",
            },
            capture_output=True,
            text=True,
            check=True,
        )
        assert credentials["password"] not in completed.stdout
        with Session(engine) as db:
            demo_ids = list(
                db.scalars(
                    select(Tenant.id).where(
                        Tenant.slug.in_(
                            ["demo-recruiting-alpha", "demo-recruiting-beta"]
                        )
                    )
                )
            )
            seed_counts.append(
                [
                    db.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.tenant_id.in_(demo_ids))
                    )
                    for model in [
                        CanonicalRecord,
                        TransformationMapping,
                        TransformationMappingVersion,
                        ExternalRecordMapping,
                        TransformationAttempt,
                        QuarantinedRecord,
                    ]
                ]
            )
    assert seed_counts[0] == seed_counts[1]
    assert all(
        actual >= expected
        for actual, expected in zip(seed_counts[0], [20, 8, 8, 22, 24, 4], strict=True)
    )
    origin = "http://127.0.0.1:3000"
    with httpx.Client(base_url=origin, timeout=20) as client:

        def mutate(
            path: str, data: dict | None = None, method: str = "POST"
        ) -> httpx.Response:
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

        login = mutate(
            "auth/login",
            {"email": credentials["email"], "password": credentials["password"]},
        )
        assert login.status_code == 200
        sensitive.extend(cookie.value for cookie in client.cookies.jar)
        alpha = next(
            row
            for row in login.json()["tenants"]
            if row["slug"] == credentials["slug_a"]
        )
        beta = next(
            row
            for row in login.json()["tenants"]
            if row["slug"] == credentials["slug_b"]
        )
        assert mutate("tenant/switch", {"tenant_id": alpha["id"]}).status_code == 200
        assert (
            len(client.get("/api/platform/canonical-schemas").json()["entities"]) == 6
        )
        assert len(client.get("/api/platform/mapping-templates").json()) == 4
        assert client.post("/api/platform/mappings", json={}).status_code == 403
        # Dedicated run connections preserve immutable evidence from previous verifier runs.
        suffix = uuid.uuid4().hex[:12]
        connections = {}
        for kind in ["crm", "ats"]:
            response = mutate(
                "connections",
                {
                    "connector_type": "synthetic_" + kind,
                    "name": f"HTTP Phase 4 {kind.upper()} {suffix}",
                },
            )
            assert response.status_code == 201
            connections[kind] = response.json()["id"]
        mappings = {}
        for kind, source_kind in [
            ("crm", "contact"),
            ("crm", "requisition"),
            ("ats", "candidate"),
            ("ats", "requisition"),
        ]:
            response = mutate(
                "mappings",
                {
                    "connection_id": connections[kind],
                    "source_kind": source_kind,
                    "name": f"Synthetic HTTP {kind} {source_kind}",
                },
            )
            assert response.status_code == 201
            mappings[kind, source_kind] = response.json()
        for index, number in enumerate(
            ["1e-999999999", "1e+999999999", "0e+999999999"]
        ):
            numeric = mutate(
                f"mappings/{mappings['ats', 'candidate']['id']}/normalize",
                {
                    "external_id": f"bounded-numeric-{index}",
                    "source_updated_at": "2031-01-15T12:00:00Z",
                    "payload": {
                        "data": {
                            "name": "Synthetic Numeric Candidate",
                            "email": "numeric@example.test",
                            "stage": "NEW",
                            "years_experience": number,
                        }
                    },
                },
            )
            assert (
                numeric.status_code == 422
                and numeric.json()["outcome"] == "QUARANTINED"
            )
            assert any(
                issue["category"] == "INVALID_VALUE"
                for issue in numeric.json()["issues"]
            )
        contact_mapping = mappings["crm", "contact"]
        original = client.get(
            f"/api/platform/mappings/{contact_mapping['id']}/versions"
        ).json()["items"][0]
        timestamp = datetime(2031, 1, 15, 12, tzinfo=UTC)
        data = {
            "external_id": "real-bff-contact-1",
            "source_updated_at": timestamp.isoformat(),
            "payload": {
                "data": {
                    "name": " Synthetic HTTP Contact ",
                    "email": "HTTP-CONTACT@EXAMPLE.TEST ",
                    "company": " Fictional Harbor ",
                }
            },
        }
        path = f"mappings/{contact_mapping['id']}"
        preview = mutate(path + "/preview", data)
        assert preview.status_code == 200 and preview.json()["persisted"] is False
        assert (
            preview.json()["canonical"]["normalized_data"]["company"]
            == "Fictional Harbor"
        )
        first = mutate(path + "/normalize", data)
        duplicate = mutate(path + "/normalize", data)
        assert (
            first.status_code == duplicate.status_code == 200
            and first.json()["id"] == duplicate.json()["id"]
        )
        canonical_id = first.json()["canonical_id"]
        assert (
            first.json()["result_snapshot"]["normalized_data"]["email"] == "[REDACTED]"
        )
        definition = original["definition"]
        definition["fields"][2]["source"] = None
        definition["fields"][2]["default"] = "Fictional Updated Company"
        new_version = mutate(path + "/versions", {"definition": definition})
        assert new_version.status_code == 201 and new_version.json()["version"] == 2
        assert (
            mutate(
                path + "/activate", {"mapping_version_id": new_version.json()["id"]}
            ).status_code
            == 200
        )
        updated = mutate(
            path + "/normalize",
            {
                **data,
                "source_updated_at": (timestamp + timedelta(seconds=2)).isoformat(),
            },
        )
        assert (
            updated.status_code == 200
            and updated.json()["mapping_version_id"] == new_version.json()["id"]
        )
        assert (
            client.get(
                "/api/platform/transformation-attempts/" + first.json()["id"]
            ).json()["mapping_version_id"]
            == original["id"]
        )
        stale = mutate(
            path + "/normalize",
            {
                **data,
                "source_updated_at": (timestamp + timedelta(seconds=1)).isoformat(),
            },
        )
        assert stale.status_code == 409 and "STALE_UPDATE" in {
            issue["category"] for issue in stale.json()["issues"]
        }
        collision = mutate(
            "external-identities",
            {
                "canonical_id": canonical_id,
                "connection_id": connections["crm"],
                "source_kind": "contact",
                "external_id": "real-bff-contact-1",
            },
        )
        assert collision.status_code == 409
        invalid = mutate(
            path + "/normalize",
            {
                "external_id": "bad-synthetic-contact",
                "payload": {"data": {"name": "", "email": "invalid-synthetic-address"}},
            },
        )
        assert invalid.status_code == 422 and invalid.json()["outcome"] == "QUARANTINED"
        # Independent CRM/ATS namespaces become shared only through explicit linking.
        job_data = {
            "external_id": "crm-job-1",
            "source_updated_at": timestamp.isoformat(),
            "payload": {
                "data": {
                    "title": "Synthetic HTTP Job",
                    "department": "Technology",
                    "location": "Toronto",
                    "status": "OPEN",
                }
            },
        }
        crm_job = mutate(
            f"mappings/{mappings['crm', 'requisition']['id']}/normalize", job_data
        )
        assert crm_job.status_code == 200
        assert (
            mutate(
                "external-identities",
                {
                    "canonical_id": crm_job.json()["canonical_id"],
                    "connection_id": connections["ats"],
                    "source_kind": "requisition",
                    "external_id": "ats-job-1",
                },
            ).status_code
            == 201
        )
        ats_path = f"mappings/{mappings['ats', 'requisition']['id']}/normalize"
        assert (
            mutate(ats_path, {**job_data, "external_id": "ats-job-1"}).status_code
            == 200
        )
        conflict = mutate(
            ats_path,
            {
                **job_data,
                "external_id": "ats-job-1",
                "source_updated_at": (timestamp + timedelta(seconds=1)).isoformat(),
                "payload": {
                    "data": {**job_data["payload"]["data"], "commercial_rate": "999.00"}
                },
            },
        )
        assert conflict.status_code == 409 and "AUTHORITY_CONFLICT" in {
            issue["category"] for issue in conflict.json()["issues"]
        }
        endpoint = mutate(
            "webhook-endpoints",
            {"connection_id": connections["crm"], "source": "synthetic_crm"},
        ).json()
        sensitive.append(endpoint["signing_secret"])
        raw, headers = signed_request(
            endpoint["signing_secret"],
            event_bytes("synthetic_crm", "job.created", "real-canonical-event-1"),
        )
        accepted = httpx.post(
            "http://127.0.0.1:8000/api/v1/webhooks/ingest/"
            + endpoint["endpoint"]["public_id"],
            content=raw,
            headers=headers,
            timeout=10,
            trust_env=False,
        )
        assert accepted.status_code == 202
        event_id = accepted.json()["event_id"]
        with Session(engine) as db:
            preserved = db.get(IncomingEvent, uuid.UUID(event_id)).payload
        assert mutate(f"events/{event_id}/preview-transformation").status_code == 200
        normalized = mutate(f"events/{event_id}/normalize")
        repeated = mutate(f"events/{event_id}/normalize")
        assert (
            normalized.status_code == repeated.status_code == 200
            and normalized.json()["id"] == repeated.json()["id"]
        )
        assert normalized.json()["event_id"] == event_id
        for read_path in [
            "canonical-records",
            "external-identities",
            "invalid-records",
            "transformation-conflicts",
            "field-authority",
            "transformation-attempts",
        ]:
            response = client.get("/api/platform/" + read_path)
            assert response.status_code == 200
            assert (
                "HTTP-CONTACT@EXAMPLE.TEST" not in response.text
                and endpoint["signing_secret"] not in response.text
            )
        assert client.get("/mappings").status_code == 200
        assert mutate("tenant/switch", {"tenant_id": beta["id"]}).status_code == 200
        for read_path in [
            path,
            f"{path}/versions",
            f"canonical-records/{canonical_id}",
            "transformation-attempts/" + first.json()["id"],
        ]:
            assert client.get("/api/platform/" + read_path).status_code == 404
        assert mutate(f"events/{event_id}/normalize").status_code == 404
        assert (
            mutate(
                "external-identities",
                {
                    "canonical_id": canonical_id,
                    "connection_id": connections["crm"],
                    "source_kind": "contact",
                    "external_id": "cross-tenant-id",
                },
            ).status_code
            == 404
        )
        assert mutate("tenant/switch", {"tenant_id": alpha["id"]}).status_code == 200
        assert (
            mutate(
                "webhook-endpoints/" + endpoint["endpoint"]["id"],
                {"enabled": False},
                method="PATCH",
            ).status_code
            == 200
        )
        assert mutate("auth/logout").status_code == 200
        assert client.get("/api/platform/canonical-records").status_code == 401
    with Session(engine) as db:
        row = db.get(IncomingEvent, uuid.UUID(event_id))
        assert row.payload == preserved and row.processing_status == "PENDING"
        assert (
            db.get(TransformationMappingVersion, uuid.UUID(original["id"])).definition[
                "fields"
            ][2]["source"]
            == "data.company"
        )
        audit = json.dumps([row.details for row in db.scalars(select(AuditEvent))])
        assert not any(value in audit for value in sensitive)
    for name in ["http-backend.log", "http-frontend.log"]:
        log = backend / ".local" / name
        if log.exists():
            assert not any(value in log.read_text() for value in sensitive)
    engine.dispose()
    print(
        "Phase 4 BFF/HTTP passed: preview, normalization, immutable version history, identity collision, field authority, stale protection, accepted-event preservation, quarantine, tenant denial and redaction."
    )
    print(
        f"Repeated seed counts (canonical/mappings/versions/identities/attempts/invalid): {seed_counts[0]}"
    )


if __name__ == "__main__":
    main()
