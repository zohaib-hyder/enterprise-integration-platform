"""Bounded real HTTP portfolio demo against the isolated local release only."""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx
from app.connectors.webhook_sender import signed_request
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def wait(check, seconds: int = 90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.25)
    raise RuntimeError("Bounded synthetic demo condition did not complete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".local/release/release.env"
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=ROOT / "docs/evidence/phase12-release-demo.json",
    )
    args = parser.parse_args()
    if not args.env_file.resolve().is_relative_to(ROOT / ".local"):
        parser.error("Private fixture configuration must stay under repository .local")
    if not args.evidence.resolve().is_relative_to(ROOT):
        parser.error("Evidence must stay inside the repository")
    env = dotenv_values(args.env_file)
    assert env["APP_ENV"] == "test" and env["AI_OPENAI_ENABLED"] == "false"
    prefix = "release-" + uuid.uuid4().hex[:8]
    results = []

    def ready():
        try:
            return (
                httpx.get("http://127.0.0.1:3300/login", timeout=3).status_code == 200
                and httpx.get("http://127.0.0.1:18000/ready", timeout=3).status_code
                == 200
            )
        except httpx.HTTPError:
            return False

    wait(ready)
    with httpx.Client(base_url="http://127.0.0.1:3300", timeout=20) as client:

        def get(path: str):
            response = client.get("/api/platform/" + path)
            assert response.status_code == 200, (path, response.status_code)
            return response.json()

        def post(path: str, data: dict, expected: int = 200, method: str = "POST"):
            csrf = get("auth/csrf")["csrf_token"]
            response = client.request(
                method,
                "/api/platform/" + path,
                json=data,
                headers={"Origin": "http://127.0.0.1:3300", "X-CSRF-Token": csrf},
            )
            assert response.status_code == expected, (path, response.status_code)
            return response.json()

        post(
            "auth/login",
            {"email": env["BOOTSTRAP_EMAIL"], "password": env["BOOTSTRAP_PASSWORD"]},
        )
        tenant = next(t for t in get("tenants") if t["slug"] == "demo-recruiting-alpha")
        post("tenant/switch", {"tenant_id": tenant["id"]})
        results.append("tenant_login")
        connections = {}
        for kind in ("crm", "ats", "slack", "email"):
            connections[kind] = post(
                "connections",
                {
                    "name": prefix + kind,
                    "connector_type": "synthetic_" + kind,
                    "config": {},
                },
                201,
            )["id"]
        results.append("synthetic_connector_configuration")
        mapping = post(
            "mappings",
            {
                "name": prefix,
                "connection_id": connections["crm"],
                "source_kind": "requisition",
            },
            201,
        )
        version = get(f"mappings/{mapping['id']}/versions")["items"][0]["id"]
        config = post(
            "sync/configurations",
            {
                "name": prefix,
                "source_connection_id": connections["crm"],
                "destination_connection_id": connections["ats"],
                "definition": {"mapping_version_id": version},
            },
            201,
        )
        config = post(
            f"sync/configurations/{config['id']}/activate",
            {"enabled": True, "expected_revision": config["revision"]},
        )
        for channel in ("SLACK", "EMAIL"):
            template = post(
                "notifications/templates",
                {
                    "name": prefix + channel,
                    "channel": channel,
                    "subject": "Synthetic {job_title}" if channel == "EMAIL" else "",
                    "body": "{job_title}: {sync_outcome} · {requisition_reference}",
                },
                201,
            )
            tv = get(f"notifications/templates/{template['id']}/versions")["items"][0][
                "id"
            ]
            post(
                f"notifications/templates/{template['id']}/activate", {"version_id": tv}
            )
            post(
                "notifications/rules",
                {
                    "name": prefix + channel,
                    "enabled": True,
                    "trigger": "SYNC_CREATE",
                    "connection_id": connections[channel.lower()],
                    "destination": "recruiting"
                    if channel == "SLACK"
                    else "recruiter@example.test",
                    "template_version_id": tv,
                    "sync_configuration_id": config["id"],
                },
                201,
            )
        post(
            f"connections/{connections['email']}/failure-injection",
            {
                "enabled": True,
                "scenario": "HTTP_503",
                "attempts": 1,
                "retry_after": "8",
            },
            method="PUT",
        )
        hook = post(
            "webhook-endpoints",
            {"connection_id": connections["crm"], "source": "synthetic_crm"},
            201,
        )
        data = {
            "title": "Synthetic North American staffing engineer",
            "department": "Technology",
            "location": "Toronto",
            "status": "OPEN",
        }
        record = post(
            f"connections/{connections['crm']}/execute",
            {
                "name": "requisition.upsert",
                "payload": {"external_id": prefix, "data": data},
            },
        )["data"]
        payload = {
            "schema_version": 1,
            "source": "synthetic_crm",
            "event_id": prefix,
            "event_type": "job.created",
            "external_id": prefix,
            "occurred_at": record["updated_at"],
            "data": data,
        }
        body, headers = signed_request(
            hook["signing_secret"], json.dumps(payload).encode()
        )
        url = (
            "http://127.0.0.1:18000/api/v1/webhooks/ingest/"
            + hook["endpoint"]["public_id"]
        )
        response = httpx.post(url, content=body, headers=headers, timeout=15)
        assert response.status_code == 202
        results.append("signed_webhook_ingestion")

        def deliveries():
            return [
                r
                for r in get("notifications/deliveries?limit=100")["items"]
                if r["connection_id"] in (connections["slack"], connections["email"])
            ]

        failed = wait(
            lambda: next(
                (
                    r
                    for r in deliveries()
                    if r["channel"] == "EMAIL" and r["status"] == "RETRY_SCHEDULED"
                ),
                None,
            )
        )
        results.append("injected_email_partial_failure")
        post(
            f"notifications/deliveries/{failed['id']}/retry",
            {
                "reason": "Synthetic release operator reviews the failed email action only."
            },
        )
        results.append("authorized_targeted_recovery")
        wait(lambda: len([r for r in deliveries() if r["status"] == "SUCCEEDED"]) == 2)
        results.append("slack_and_email_delivery")
        executions = wait(
            lambda: [
                r
                for r in get("sync/executions?configuration_id=" + config["id"])[
                    "items"
                ]
                if r["state"] == "SUCCEEDED"
            ]
        )
        assert len(executions) == 1
        results.extend(["canonical_transformation", "crm_to_ats_sync"])
        wait(
            lambda: any(
                r["state"] == "SUCCEEDED"
                for r in get("executions?workflow_id=" + config["workflow_id"])["items"]
            )
        )
        results.append("durable_workflow_execution")
        duplicate = httpx.post(url, content=body, headers=headers, timeout=15)
        assert duplicate.status_code == 200
        assert (
            len(get("sync/executions?configuration_id=" + config["id"])["items"]) == 1
        )
        results.append("webhook_replay_idempotency")
        get("operations/overview")
        get("operations/health")
        results.append("operations_dashboard")
        before = get("operations/recovery/requests")["total"]
        analysis = post(
            "ai/analyses",
            {
                "target_kind": "connection",
                "target_id": connections["email"],
                "analysis_type": "CONNECTOR_FAILURE",
                "request_key": str(uuid.uuid4()),
            },
            201,
        )
        assert analysis["provider"] == "mock" and analysis["evidence"]
        results.append("authorized_mock_ai_evidence")
        recommendation = next(
            r
            for r in analysis["recommendations"]
            if r["action"] == "ESCALATE_FOR_MANUAL_REVIEW"
        )
        review = post(
            f"ai/recommendations/{recommendation['id']}/review",
            {
                "expected_version": recommendation["version"],
                "decision": "ACCEPTED",
                "reason": "Synthetic reviewer inspected current authorized release evidence.",
            },
        )
        assert review["preview_only"] and review["recovery_preview"]
        assert get("operations/recovery/requests")["total"] == before
        results.append("human_review_preview_without_execution")
        post("auth/logout", {})
    evidence = {
        "prefix": prefix,
        "scope": "Isolated Compose test-mode HTTP rehearsal; synthetic connectors and mock AI only",
        "passed": len(results),
        "checks": results,
    }
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        f"PASS: {len(results)} release demonstration checks; no real communication or AI execution."
    )


if __name__ == "__main__":
    main()
