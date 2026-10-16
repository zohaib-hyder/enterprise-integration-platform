"""Create genuine held-sync and pending mock-review evidence in an isolated demo."""

import argparse
import json
import uuid
from pathlib import Path

import httpx
from app.connectors.webhook_sender import signed_request
from dotenv import dotenv_values
from release_demo import wait

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--demo-evidence", type=Path, required=True)
    args = parser.parse_args()
    if not args.env_file.resolve().is_relative_to(ROOT / ".local"):
        parser.error("Private configuration must remain inside repository .local")
    env = dotenv_values(args.env_file)
    if env.get("APP_ENV") != "test" or env.get("AI_OPENAI_ENABLED") != "false":
        parser.error("Synthetic test mode with mock AI is required")
    prefix = json.loads(args.demo_evidence.read_text())["prefix"]
    with httpx.Client(base_url="http://127.0.0.1:3300", timeout=30) as client:

        def get(path: str):
            r = client.get("/api/platform/" + path)
            assert r.status_code == 200, (path, r.status_code)
            return r.json()

        def post(path: str, data: dict, expected: int = 200):
            csrf = get("auth/csrf")["csrf_token"]
            r = client.post(
                "/api/platform/" + path,
                json=data,
                headers={
                    "Origin": "http://127.0.0.1:3300",
                    "X-CSRF-Token": csrf,
                },
            )
            assert r.status_code == expected, (path, r.status_code)
            return r.json()

        post(
            "auth/login",
            {"email": env["BOOTSTRAP_EMAIL"], "password": env["BOOTSTRAP_PASSWORD"]},
        )
        tenant = next(t for t in get("tenants") if t["slug"] == "demo-recruiting-alpha")
        post("tenant/switch", {"tenant_id": tenant["id"]})
        connections = {
            k: next(
                c
                for c in get("connections?limit=100")["items"]
                if c["name"] == prefix + k
            )["id"]
            for k in ("crm", "ats", "email")
        }
        config = next(
            c for c in get("sync/configurations")["items"] if c["name"] == prefix
        )
        execution = next(
            e
            for e in get("sync/executions?configuration_id=" + config["id"])["items"]
            if e["state"] == "SUCCEEDED"
        )
        plan = get("sync/plans/" + execution["plan_id"])
        destination = plan["evidence"]["destination_identity"]["external_id"]
        record = next(
            r
            for r in get(f"connections/{connections['ats']}/records?limit=100")["items"]
            if r["external_id"] == destination
        )
        post(
            f"connections/{connections['ats']}/execute",
            {
                "name": "requisition.upsert",
                "payload": {
                    "external_id": destination,
                    "data": {
                        **record["data"],
                        "title": "Synthetic recruiter independent edit",
                    },
                },
            },
        )
        data = {
            "title": "Synthetic CRM revised engineering requisition",
            "department": "Technology",
            "location": "Toronto",
            "status": "OPEN",
        }
        source = post(
            f"connections/{connections['crm']}/execute",
            {
                "name": "requisition.upsert",
                "payload": {"external_id": prefix, "data": data},
            },
        )["data"]
        hook = post(
            "webhook-endpoints",
            {"connection_id": connections["crm"], "source": "synthetic_crm"},
            201,
        )
        payload = {
            "schema_version": 1,
            "source": "synthetic_crm",
            "event_id": prefix + "-conflict",
            "event_type": "job.updated",
            "external_id": prefix,
            "occurred_at": source["updated_at"],
            "data": data,
        }
        body, headers = signed_request(
            hook["signing_secret"], json.dumps(payload).encode()
        )
        r = httpx.post(
            "http://127.0.0.1:18000/api/v1/webhooks/ingest/"
            + hook["endpoint"]["public_id"],
            content=body,
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 202
        conflict = wait(
            lambda: next(
                (
                    c
                    for c in get("sync/conflicts?status=OPEN")["items"]
                    if c["category"] == "DESTINATION_CHANGED"
                ),
                None,
            )
        )
        post("operations/incidents/refresh", {"hours": 24, "limit": 100})
        incidents = get("operations/incidents?limit=100")["items"]
        incident = next(i for i in incidents if i.get("correlation_id"))
        analysis = post(
            "ai/analyses",
            {
                "target_kind": "incident",
                "target_id": incident["id"],
                "analysis_type": "INCIDENT_EXPLANATION",
                "request_key": str(uuid.uuid4()),
            },
            201,
        )
        assert analysis["provider"] == "mock" and analysis["evidence"]
        evidence = {
            "scope": "Synthetic local portfolio evidence; no direct database state injection",
            "tenant_id": tenant["id"],
            "conflict_id": conflict["id"],
            "incident_id": incident["id"],
            "correlation_id": incident["correlation_id"],
            "analysis_id": analysis["id"],
            "mock_ai": True,
            "conflict_left_open": True,
            "review_left_pending": True,
        }
        (ROOT / "docs/evidence/phase13a-screenshot-fixtures.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
        post("auth/logout", {})
    print(
        "Genuine conflict, incident and pending mock review prepared; no secret printed."
    )


if __name__ == "__main__":
    main()
