"""Repeatable local reliability demonstration through BFF, HMAC ingress and real workers.

Run from backend with PYTHONPATH=.; requires running API + built frontend. Each run
retains a uniquely named synthetic evidence set. Stops only its own workers; no reset.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from app.connectors.webhook_sender import signed_request
from app.core.config import Settings
from app.db.session import build_engine
from app.models.reliability import (
    ConnectionCredential,
    ConnectorObservation,
    CredentialRefreshAttempt,
)
from app.models.workflow import (
    WorkflowActionReceipt,
    WorkflowStepExecution,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main() -> None:
    backend = Path(__file__).resolve().parents[1] / "backend"
    credentials_file = Path(
        os.environ.get(
            "PLATFORM_VERIFICATION_CREDENTIALS",
            str(backend / ".local/http-verification.json"),
        )
    )
    if not credentials_file.exists():
        raise SystemExit(
            "Run verify_phase1_http.py with both local servers first to prepare private synthetic operator credentials."
        )
    operator = json.loads(credentials_file.read_text())
    engine = build_engine(Settings())
    assert engine is not None
    prefix = "phase6-" + uuid.uuid4().hex[:10] + "-"
    sensitive = [operator["password"]]
    workers = []
    log_path = backend / ".local/phase6-worker.log"
    log = log_path.open("a")

    def start():
        process = subprocess.Popen(
            [sys.executable, "-m", "app.workers.runner", "--poll-seconds", ".2"],
            cwd=backend,
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONPATH": "."},
        )
        workers.append(process)
        return process

    def stop(process):
        process.terminate()
        process.wait(timeout=15)

    def wait(fn, predicate, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = fn()
            if predicate(value):
                return value
            time.sleep(0.05)
        raise AssertionError("Timed out waiting for actual reliability workflow state")

    try:
        with httpx.Client(base_url="http://127.0.0.1:3000", timeout=20) as client:

            def mutate(path, data=None, method="POST"):
                token = client.get("/api/platform/auth/csrf").json()["csrf_token"]
                return client.request(
                    method,
                    "/api/platform/" + path,
                    json=data or {},
                    headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": token},
                )

            assert (
                mutate(
                    "auth/login",
                    {"email": operator["email"], "password": operator["password"]},
                ).status_code
                == 200
            )
            tenants = client.get("/api/platform/tenants").json()
            alpha = next(t for t in tenants if t["slug"] == "demo-recruiting-alpha")
            beta = next(t for t in tenants if t["slug"] == "demo-recruiting-beta")
            assert (
                mutate("tenant/switch", {"tenant_id": alpha["id"]}).status_code == 200
            )
            slack = mutate(
                "connections",
                {
                    "name": prefix + "notifications",
                    "connector_type": "synthetic_slack",
                    "config": {"allowed_channels": ["recruiting"]},
                },
            )
            assert slack.status_code == 201
            slack_id = slack.json()["id"]
            workflows = {}
            connections = {}
            hooks = {}
            records = {}
            job = {
                "title": "Synthetic Phase 6 Job",
                "department": "Technology",
                "location": "Toronto",
                "status": "OPEN",
            }
            for kind in ["rate", "circuit", "refresh", "revoked", "repeated"]:
                response = mutate(
                    "connections",
                    {
                        "name": prefix + kind,
                        "connector_type": "synthetic_crm",
                        "config": {},
                    },
                )
                assert response.status_code == 201
                conn = response.json()["id"]
                connections[kind] = conn
                upsert = mutate(
                    f"connections/{conn}/execute",
                    {
                        "name": "requisition.upsert",
                        "payload": {"external_id": "phase6-job", "data": job},
                    },
                )
                assert upsert.status_code == 200
                records[kind] = upsert.json()["data"]["id"]
                endpoint = mutate(
                    "webhook-endpoints",
                    {"connection_id": conn, "source": "synthetic_crm"},
                )
                assert endpoint.status_code == 201
                hooks[kind] = endpoint.json()
                sensitive.append(hooks[kind]["signing_secret"])
                policy = {
                    "connect_timeout_ms": 1000,
                    "read_timeout_ms": 3000,
                    "operation_deadline_ms": 5000,
                    "max_attempts": 5,
                    "base_delay_seconds": 1,
                    "max_delay_seconds": 10,
                    "jitter_fraction": 0,
                    "circuit_failure_threshold": 1 if kind == "circuit" else 5,
                    "circuit_cooldown_seconds": 3,
                    "repeated_failure_threshold": 2,
                    "credential_mode": "OAUTH"
                    if kind in ["refresh", "revoked"]
                    else "OPTIONAL",
                }
                assert (
                    mutate(f"connections/{conn}/reliability", policy, "PUT").status_code
                    == 200
                )
                if kind in ["refresh", "revoked"]:
                    for token_type in ["OAUTH_ACCESS_TOKEN", "OAUTH_REFRESH_TOKEN"]:
                        value = "sim_" + kind + "_" + uuid.uuid4().hex
                        sensitive.append(value)
                        expiration = datetime.now(UTC) + timedelta(
                            seconds=-1 if token_type == "OAUTH_ACCESS_TOKEN" else 3600
                        )
                        stored = mutate(
                            f"connections/{conn}/credentials",
                            {
                                "credential_type": token_type,
                                "value": value,
                                "expires_at": expiration.isoformat(),
                            },
                        )
                        assert stored.status_code == 201 and value not in stored.text
                        if kind == "revoked" and token_type == "OAUTH_REFRESH_TOKEN":
                            assert (
                                mutate(
                                    f"connections/{conn}/credentials/{stored.json()['id']}/revoke"
                                ).status_code
                                == 200
                            )
                    metadata = client.get(
                        f"/api/platform/connections/{conn}/credentials"
                    ).json()
                    assert all(
                        "value" not in row and row["masked_value"] == "••••••••"
                        for row in metadata["items"]
                    )
                if kind == "repeated":
                    continue
                definition = {
                    "schema_version": 1,
                    "trigger": {
                        "connection_id": conn,
                        "source": "synthetic_crm",
                        "event_types": ["job.updated"],
                        "event_id_prefix": prefix + kind,
                    },
                    "steps": [
                        {
                            "id": "read",
                            "type": "MOCK_CONNECTOR_ACTION",
                            "config": {
                                "connection_id": conn,
                                "operation": "requisition.get",
                                "payload": {"id": records[kind]},
                            },
                            "retry": {"max_attempts": 5, "jitter_fraction": 0},
                        },
                        {
                            "id": "notify",
                            "type": "MOCK_CONNECTOR_ACTION",
                            "config": {
                                "connection_id": slack_id,
                                "operation": "message.send",
                                "payload": {
                                    "channel": "recruiting",
                                    "text": "Synthetic reliability demo; no external Slack message",
                                },
                            },
                        },
                        {
                            "id": "result",
                            "type": "RECORD_RESULT",
                            "config": {
                                "summary": "Synthetic reliability scenario completed"
                            },
                        },
                    ],
                }
                wf = mutate(
                    "workflows", {"name": prefix + kind, "definition": definition}
                )
                assert wf.status_code == 201
                workflows[kind] = wf.json()["id"]
                assert (
                    mutate(
                        f"workflows/{workflows[kind]}/activate", {"enabled": True}
                    ).status_code
                    == 200
                )

            def inject(kind, scenario, attempts=1, retry_after=None):
                response = mutate(
                    f"connections/{connections[kind]}/failure-injection",
                    {
                        "enabled": True,
                        "scenario": scenario,
                        "attempts": attempts,
                        "retry_after": retry_after,
                    },
                    "PUT",
                )
                assert response.status_code == 200

            def deliver(kind):
                body = json.dumps(
                    {
                        "schema_version": 1,
                        "source": "synthetic_crm",
                        "event_id": prefix + kind,
                        "event_type": "job.updated",
                        "external_id": "phase6-job",
                        "occurred_at": "2030-01-15T12:00:00Z",
                        "data": job,
                    }
                ).encode()
                body, headers = signed_request(hooks[kind]["signing_secret"], body)
                return httpx.post(
                    "http://127.0.0.1:8000/api/v1/webhooks/ingest/"
                    + hooks[kind]["endpoint"]["public_id"],
                    content=body,
                    headers=headers,
                    timeout=10,
                )

            def execution(kind):
                rows = client.get(
                    "/api/platform/executions?workflow_id=" + workflows[kind]
                ).json()["items"]
                return rows[0] if rows else None

            # Provider 429 and a real local budget both defer durably. Budget is configured
            # after setup consumption; the existing fixed-window counter is not erased.
            rate = connections["rate"]
            assert (
                mutate(
                    f"connections/{rate}/rate-limits",
                    {"scope": "CONNECTION", "limit": 1, "window_seconds": 3},
                    "PUT",
                ).status_code
                == 200
            )
            # First generate an admitted synthetic 429 to verify Retry-After, then let the
            # next request encounter the persisted local window if it is still exhausted.
            inject("rate", "HTTP_429", retry_after="2")
            process = start()
            assert deliver("rate").status_code == 202
            retry = wait(
                lambda: execution("rate"),
                lambda r: r and r["state"] == "RETRY_SCHEDULED",
            )
            retries = client.get(
                f"/api/platform/executions/{retry['id']}/retries"
            ).json()
            assert retries["total"] >= 1
            stop(process)
            process = start()  # restart while a persisted next-attempt time exists
            rate_success = wait(
                lambda: execution("rate"), lambda r: r and r["state"] == "SUCCEEDED"
            )
            assert deliver("rate").status_code == 200
            # Persist OPEN across a real worker stop/restart, then controlled half-open recovery.
            inject("circuit", "HTTP_503")
            assert deliver("circuit").status_code == 202
            wait(
                lambda: client.get(
                    f"/api/platform/connections/{connections['circuit']}/circuit"
                ).json(),
                lambda r: r["state"] == "OPEN",
            )
            stop(process)
            assert (
                client.get(
                    f"/api/platform/connections/{connections['circuit']}/circuit"
                ).json()["state"]
                == "OPEN"
            )
            process = start()
            circuit_success = wait(
                lambda: execution("circuit"), lambda r: r and r["state"] == "SUCCEEDED"
            )
            history = client.get(
                f"/api/platform/connections/{connections['circuit']}/circuit/history"
            ).json()["items"]
            assert {"OPEN", "HALF_OPEN", "CLOSED"} <= {r["to_state"] for r in history}
            # Expired access -> one local refresh -> success; revoked refresh -> one terminal failure.
            assert deliver("refresh").status_code == 202
            refresh_success = wait(
                lambda: execution("refresh"), lambda r: r and r["state"] == "SUCCEEDED"
            )
            assert deliver("revoked").status_code == 202
            revoked = wait(
                lambda: execution("revoked"),
                lambda r: r and r["state"] == "DEAD_LETTERED",
            )
            assert (
                client.get(f"/api/platform/executions/{revoked['id']}/retries").json()[
                    "total"
                ]
                == 0
            )
            inject("repeated", "HTTP_503", attempts=2)
            for _ in range(2):
                response = mutate(
                    f"connections/{connections['repeated']}/execute",
                    {"name": "requisition.list", "payload": {}},
                )
                assert response.status_code == 503
            alerts = client.get("/api/platform/alerts?limit=100").json()["items"]
            target = next(
                a
                for a in alerts
                if a["category"] == "REPEATED_CONNECTOR_FAILURE"
                and a["context"].get("connection_id") == connections["repeated"]
            )
            assert (
                mutate(f"alerts/{target['id']}/acknowledge").json()["status"]
                == "ACKNOWLEDGED"
            )
            assert (
                mutate(f"alerts/{target['id']}/resolve").json()["status"] == "RESOLVED"
            )
            data = client.get("/api/platform/operations/metrics").json()
            assert (
                data["rate_limit_count"] >= 1
                and data["circuit_transition_count"] >= 3
                and data["credential_refresh_count"] >= 2
            )
            assert (
                data["credential_refresh_success_count"] >= 1
                and data["retry_count"] >= 1
            )
            with Session(engine) as db:
                conn_ids = [uuid.UUID(v) for v in connections.values()]
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(CredentialRefreshAttempt)
                        .where(
                            CredentialRefreshAttempt.connection_id.in_(conn_ids),
                            CredentialRefreshAttempt.outcome == "SUCCEEDED",
                        )
                    )
                    == 1
                )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(CredentialRefreshAttempt)
                        .where(
                            CredentialRefreshAttempt.connection_id.in_(conn_ids),
                            CredentialRefreshAttempt.outcome == "FAILED",
                        )
                    )
                    == 1
                )
                roots = [
                    uuid.UUID(r["id"])
                    for r in [rate_success, circuit_success, refresh_success]
                ]
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(WorkflowActionReceipt)
                        .where(
                            WorkflowActionReceipt.root_execution_id.in_(roots),
                            WorkflowActionReceipt.step_id == "notify",
                        )
                    )
                    == 3
                )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(WorkflowStepExecution)
                        .where(
                            WorkflowStepExecution.execution_id
                            == uuid.UUID(revoked["id"]),
                            WorkflowStepExecution.state == "FAILED",
                        )
                    )
                    == 1
                )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(ConnectorObservation)
                        .where(
                            ConnectorObservation.tenant_id == uuid.UUID(alpha["id"]),
                            ConnectorObservation.created_at
                            >= datetime.fromisoformat(data["since"]),
                        )
                    )
                    == data["operation_count"]
                )
                for row in db.scalars(
                    select(ConnectionCredential).where(
                        ConnectionCredential.connection_id.in_(conn_ids)
                    )
                ):
                    assert all(
                        secret.encode() not in row.encrypted_payload
                        for secret in sensitive
                    )
            assert mutate("tenant/switch", {"tenant_id": beta["id"]}).status_code == 200
            assert (
                client.get(f"/api/platform/connections/{rate}/reliability").status_code
                == 404
            )
            assert (
                client.get(
                    f"/api/platform/connections/{connections['refresh']}/credentials"
                ).status_code
                == 404
            )
            assert client.get("/api/platform/alerts/" + target["id"]).status_code == 404
            assert mutate("auth/logout").status_code == 200
        log.flush()
        worker_log = log_path.read_text()
        assert all(value not in worker_log for value in sensitive)
        print(
            "PASS: actual worker rate-limit/retry/restart/success; persistent OPEN/half-open recovery; encrypted token refresh and revoked-refresh terminal failure; duplicate-action suppression; repeated-failure alerts/ack/resolve; persisted metrics correctness; tenant denial and worker-log redaction."
        )
        print("Synthetic evidence prefix: " + prefix)
    finally:
        for process in workers:
            if process.poll() is None:
                stop(process)
        log.close()
        engine.dispose()


if __name__ == "__main__":
    main()
