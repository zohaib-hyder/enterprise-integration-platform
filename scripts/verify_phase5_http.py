"""Actual BFF + signed HTTP + subprocess worker recovery. Synthetic persistent evidence only.

Run from backend: PYTHONPATH=. .venv/bin/python ../scripts/verify_phase5_http.py
Requires the API, built frontend and existing private Phase 1 verification credentials.
Only processes spawned here are stopped. No database reset or deletion.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from app.connectors.webhook_sender import signed_request
from app.core.config import Settings
from app.core.webhook_security import unseal
from app.db.session import build_engine
from app.models.integration import (
    IntegrationConnection,
    SyntheticCRMRecord,
    SyntheticNotification,
)
from app.models.tenant import Tenant
from app.models.webhook import WebhookEndpoint, WebhookSecretVersion
from app.models.workflow import (
    WorkflowActionReceipt,
    WorkflowExecution,
    WorkflowExecutionEvent,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main() -> None:
    backend = Path(__file__).resolve().parents[1] / "backend"
    credentials = json.loads((backend / ".local/http-verification.json").read_text())
    settings = Settings()
    engine = build_engine(settings)
    assert engine is not None
    prefix = "phase5-" + uuid.uuid4().hex[:12] + "-"
    processes = []
    log_path = backend / ".local/phase5-worker.log"
    log = log_path.open("a")

    def start_worker():
        process = subprocess.Popen(
            [sys.executable, "-m", "app.workers.runner", "--lease-seconds", "15"],
            cwd=backend,
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONPATH": "."},
        )
        processes.append(process)
        return process

    def await_value(fn, predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = fn()
            if predicate(value):
                return value
            time.sleep(0.05)
        raise AssertionError("Timed out waiting for durable workflow state")

    try:
        with httpx.Client(base_url="http://127.0.0.1:3000", timeout=20) as client:

            def mutate(path, data=None):
                token = client.get("/api/platform/auth/csrf").json()["csrf_token"]
                return client.post(
                    "/api/platform/" + path,
                    json=data or {},
                    headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": token},
                )

            assert (
                mutate(
                    "auth/login",
                    {
                        "email": credentials["email"],
                        "password": credentials["password"],
                    },
                ).status_code
                == 200
            )
            tenants = client.get("/api/platform/tenants").json()
            alpha = next(t for t in tenants if t["slug"] == "demo-recruiting-alpha")
            beta = next(t for t in tenants if t["slug"] == "demo-recruiting-beta")
            assert (
                mutate("tenant/switch", {"tenant_id": alpha["id"]}).status_code == 200
            )
            with Session(engine) as db:
                crm = db.scalar(
                    select(IntegrationConnection).where(
                        IntegrationConnection.tenant_id == uuid.UUID(alpha["id"]),
                        IntegrationConnection.name == "Demo CRM",
                    )
                )
                slack = db.scalar(
                    select(IntegrationConnection).where(
                        IntegrationConnection.tenant_id == uuid.UUID(alpha["id"]),
                        IntegrationConnection.name == "Demo SLACK",
                    )
                )
                job = db.scalar(
                    select(SyntheticCRMRecord).where(
                        SyntheticCRMRecord.connection_id == crm.id,
                        SyntheticCRMRecord.external_id == "demo-job-1",
                    )
                )
                endpoint = db.scalar(
                    select(WebhookEndpoint).where(
                        WebhookEndpoint.connection_id == crm.id,
                        WebhookEndpoint.status == "ACTIVE",
                    )
                )
                secret_version = db.scalar(
                    select(WebhookSecretVersion).where(
                        WebhookSecretVersion.endpoint_id == endpoint.id,
                        WebhookSecretVersion.valid_until.is_(None),
                    )
                )
                secret = unseal(settings, secret_version)
                crm_id, slack_id, job_payload, public_id = (
                    str(crm.id),
                    str(slack.id),
                    dict(job.payload),
                    endpoint.public_id,
                )
                baseline_notifications = db.scalar(
                    select(func.count())
                    .select_from(SyntheticNotification)
                    .where(SyntheticNotification.tenant_id == alpha["id"])
                )
            mappings = client.get("/api/platform/mappings?limit=100").json()["items"]
            mapping = next(
                m
                for m in mappings
                if m["connection_id"] == crm_id and m["source_kind"] == "requisition"
            )
            versions = client.get(
                f"/api/platform/mappings/{mapping['id']}/versions"
            ).json()["items"]
            mapping_version = next(
                v["id"] for v in versions if v["version"] == mapping["active_version"]
            )
            workflow_ids = {}
            for kind in ["success", "transient", "exhausted", "interrupted"]:
                specs = [
                    {
                        "id": "normalize",
                        "type": "NORMALIZE_EVENT",
                        "config": {"mapping_version_id": mapping_version},
                    },
                    {
                        "id": "condition",
                        "type": "CONDITION",
                        "config": {
                            "reference": {
                                "source": "step",
                                "step_id": "normalize",
                                "path": "normalized_data.title",
                            },
                            "operator": "EXISTS",
                        },
                    },
                    {
                        "id": "notify",
                        "type": "MOCK_CONNECTOR_ACTION",
                        "config": {
                            "connection_id": slack_id,
                            "operation": "message.send",
                            "payload": {
                                "channel": "recruiting",
                                "text": "Synthetic workflow HTTP verification",
                            },
                            "fault": {
                                "mode": "TRANSIENT"
                                if kind in ["transient", "exhausted"]
                                else "NONE",
                                "fail_first_attempts": 3 if kind == "exhausted" else 1,
                            },
                        },
                        "retry": {"jitter_fraction": 0},
                    },
                    {"id": "result", "type": "RECORD_RESULT", "config": {}},
                ]
                if kind == "interrupted":
                    specs = [{"id": "result", "type": "RECORD_RESULT", "config": {}}]
                definition = {
                    "schema_version": 1,
                    "trigger": {
                        "connection_id": crm_id,
                        "source": "synthetic_crm",
                        "event_types": ["job.updated"],
                        "event_id_prefix": prefix + kind,
                    },
                    "steps": specs,
                }
                assert mutate("workflows/validate", definition).status_code == 200
                created = mutate(
                    "workflows", {"name": prefix + kind, "definition": definition}
                )
                assert created.status_code == 201
                workflow_ids[kind] = created.json()["id"]
                assert (
                    mutate(
                        "workflows/" + workflow_ids[kind] + "/activate",
                        {"enabled": True},
                    ).status_code
                    == 200
                )
            assert (
                client.post("/api/platform/workflow-dispatch", json={}).status_code
                == 403
            )

            def deliver(kind):
                body = json.dumps(
                    {
                        "schema_version": 1,
                        "source": "synthetic_crm",
                        "event_id": prefix + kind,
                        "event_type": "job.updated",
                        "external_id": "demo-job-1",
                        "occurred_at": "2030-01-15T12:00:00Z",
                        "data": job_payload,
                    }
                ).encode()
                signed_body, headers = signed_request(secret, body)
                return httpx.post(
                    "http://127.0.0.1:8000/api/v1/webhooks/ingest/" + public_id,
                    content=signed_body,
                    headers=headers,
                )

            def execution(kind):
                rows = client.get(
                    "/api/platform/executions?workflow_id=" + workflow_ids[kind]
                ).json()["items"]
                return rows[0] if rows else None

            process = start_worker()
            for kind in ["success", "transient", "exhausted"]:
                assert deliver(kind).status_code == 202
            succeeded = await_value(
                lambda: execution("success"), lambda x: x and x["state"] == "SUCCEEDED"
            )
            transient = await_value(
                lambda: execution("transient"),
                lambda x: x and x["state"] == "SUCCEEDED",
            )
            exhausted = await_value(
                lambda: execution("exhausted"),
                lambda x: x and x["state"] == "DEAD_LETTERED",
            )
            assert deliver("success").status_code == 200
            assert deliver("transient").status_code == 200
            assert (
                client.get(
                    f"/api/platform/executions/{transient['id']}/retries"
                ).json()["total"]
                == 1
            )
            assert (
                client.get(
                    f"/api/platform/executions/{exhausted['id']}/retries"
                ).json()["total"]
                == 2
            )
            assert client.get("/api/platform/dead-letters").status_code == 200
            assert (
                mutate(
                    f"executions/{exhausted['id']}/replay", {"confirmed": False}
                ).status_code
                == 422
            )
            replay = mutate(f"executions/{exhausted['id']}/replay", {"confirmed": True})
            assert replay.status_code == 201
            replay_id = replay.json()["id"]
            await_value(
                lambda: client.get("/api/platform/executions/" + replay_id).json(),
                lambda x: x["state"] == "SUCCEEDED",
            )
            assert (
                mutate(
                    f"executions/{exhausted['id']}/replay", {"confirmed": True}
                ).json()["id"]
                == replay_id
            )
            with Session(engine) as db:
                notifications = db.scalar(
                    select(func.count())
                    .select_from(SyntheticNotification)
                    .where(SyntheticNotification.tenant_id == alpha["id"])
                )
                assert notifications - baseline_notifications == 3
                roots = [uuid.UUID(e["id"]) for e in [succeeded, transient, exhausted]]
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(WorkflowActionReceipt)
                        .where(WorkflowActionReceipt.root_execution_id.in_(roots))
                    )
                    == 3
                )
            process.terminate()
            process.wait(timeout=15)

            # Dispatch while worker is stopped, then deliberately block its effect transaction.
            assert deliver("interrupted").status_code == 202
            assert mutate("workflow-dispatch").status_code == 200
            interrupted = execution("interrupted")
            with engine.connect() as connection:
                transaction = connection.begin()
                connection.execute(
                    select(Tenant)
                    .where(Tenant.id == alpha["id"])
                    .with_for_update(key_share=True)
                )
                process = start_worker()

                def durable_state():
                    with Session(engine) as db:
                        row = db.get(WorkflowExecution, uuid.UUID(interrupted["id"]))
                        return row.state, row.lease_token

                old = await_value(
                    durable_state, lambda x: x[0] == "RUNNING", seconds=10
                )
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                transaction.rollback()
            assert old[1] is not None
            process = start_worker()
            recovered = await_value(
                lambda: execution("interrupted"),
                lambda x: x and x["state"] == "SUCCEEDED",
                seconds=30,
            )
            with Session(engine) as db:
                assert db.scalar(
                    select(WorkflowExecutionEvent.id).where(
                        WorkflowExecutionEvent.execution_id == recovered["id"],
                        WorkflowExecutionEvent.event_type == "LEASE_RECOVERED",
                    )
                )
            assert client.get("/api/platform/worker-status").json()["active_workers"]
            assert mutate("tenant/switch", {"tenant_id": beta["id"]}).status_code == 200
            assert (
                client.get("/api/platform/executions/" + succeeded["id"]).status_code
                == 404
            )
            assert (
                client.get(
                    "/api/platform/workflows/" + workflow_ids["success"]
                ).status_code
                == 404
            )
            assert (
                mutate(
                    "executions/" + exhausted["id"] + "/replay", {"confirmed": True}
                ).status_code
                == 404
            )
            assert mutate("auth/logout").status_code == 200
        log.flush()
        text = log_path.read_text()
        for sensitive in [credentials["password"], secret.decode(), public_id]:
            assert sensitive not in text
        print(
            "PASS: real BFF CRUD/activation, signed ingress, actual worker success/retry/exhaustion, duplicate action suppression, confirmed checkpoint replay, SIGKILL/expired-lease restart, cross-tenant denial, CSRF and worker-log redaction"
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        log.close()
        engine.dispose()


if __name__ == "__main__":
    main()
