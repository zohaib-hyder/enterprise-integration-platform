"""Repeatable Phase 7 BFF/HMAC/worker demonstration; retained uniquely named data."""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from app.connectors.webhook_sender import signed_request
from app.core.config import Settings
from app.db.session import build_engine
from app.models.integration import SyntheticATSRecord
from app.models.synchronization import SyncExecution, SyncOperation, SyncPlan
from app.models.workflow import RetrySchedule, WorkflowExecution
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main():
    backend = Path(__file__).resolve().parents[1] / "backend"
    credentials = json.loads((backend / ".local/http-verification.json").read_text())
    engine = build_engine(Settings())
    assert engine is not None
    prefix = "phase7-" + uuid.uuid4().hex[:10] + "-"
    workers = []
    log = (backend / ".local/phase7-worker.log").open("a")
    sensitive = [credentials["password"]]

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

    def wait(check, seconds=45):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            value = check()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError("Timed out waiting for actual Phase 7 persisted state")

    try:
        with httpx.Client(base_url="http://127.0.0.1:3000", timeout=20) as client:

            def get(path):
                response = client.get("/api/platform/" + path)
                assert response.status_code == 200, (path, response.status_code)
                return response.json()

            def mutate(path, data=None, method="POST", expected=200):
                csrf = get("auth/csrf")["csrf_token"]
                response = client.request(
                    method,
                    "/api/platform/" + path,
                    json=data or {},
                    headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": csrf},
                )
                assert response.status_code == expected, (
                    path,
                    response.status_code,
                    response.text
                    if not any(secret in response.text for secret in sensitive)
                    else "redacted",
                )
                return response.json()

            mutate(
                "auth/login",
                {"email": credentials["email"], "password": credentials["password"]},
            )
            tenants = get("tenants")
            alpha = next(t for t in tenants if t["slug"] == "demo-recruiting-alpha")
            beta = next(t for t in tenants if t["slug"] == "demo-recruiting-beta")
            mutate("tenant/switch", {"tenant_id": alpha["id"]})
            crm = mutate(
                "connections",
                {
                    "name": prefix + "CRM",
                    "connector_type": "synthetic_crm",
                    "config": {},
                },
                expected=201,
            )["id"]
            ats = mutate(
                "connections",
                {
                    "name": prefix + "ATS",
                    "connector_type": "synthetic_ats",
                    "config": {},
                },
                expected=201,
            )["id"]
            mapping = mutate(
                "mappings",
                {
                    "name": prefix + "requisitions",
                    "connection_id": crm,
                    "source_kind": "requisition",
                },
                expected=201,
            )
            mv = get(f"mappings/{mapping['id']}/versions")["items"][0]["id"]
            cfg = mutate(
                "sync/configurations",
                {
                    "name": prefix + "sync",
                    "source_connection_id": crm,
                    "destination_connection_id": ats,
                    "definition": {"mapping_version_id": mv},
                },
                expected=201,
            )
            cfg = mutate(
                f"sync/configurations/{cfg['id']}/activate",
                {"enabled": True, "expected_revision": cfg["revision"]},
            )
            hook = mutate(
                "webhook-endpoints",
                {"connection_id": crm, "source": "synthetic_crm"},
                expected=201,
            )
            sensitive.append(hook["signing_secret"])
            job = {
                "title": "Synthetic Phase 7 requisition",
                "department": "Technology",
                "location": "Toronto",
                "status": "OPEN",
            }

            def source_write(data, external="job-1"):
                return mutate(
                    f"connections/{crm}/execute",
                    {
                        "name": "requisition.upsert",
                        "payload": {"external_id": external, "data": data},
                    },
                )["data"]

            def deliver(record, data, label, event_type="job.updated", occurred=None):
                payload = {
                    "schema_version": 1,
                    "source": "synthetic_crm",
                    "event_id": prefix + label,
                    "event_type": event_type,
                    "external_id": record["external_id"],
                    "occurred_at": occurred or record["updated_at"],
                    "data": data,
                }
                body = json.dumps(payload).encode()
                body, headers = signed_request(hook["signing_secret"], body)
                response = httpx.post(
                    "http://127.0.0.1:8000/api/v1/webhooks/ingest/"
                    + hook["endpoint"]["public_id"],
                    content=body,
                    headers=headers,
                    timeout=10,
                )
                assert response.status_code in {200, 202}
                return payload, response.status_code

            def state_for(label, state="SUCCEEDED"):
                with Session(engine) as db:
                    from app.models.webhook import IncomingEvent

                    rows = db.execute(
                        select(SyncExecution, SyncPlan)
                        .join(SyncPlan, SyncPlan.id == SyncExecution.plan_id)
                        .join(IncomingEvent, IncomingEvent.id == SyncPlan.event_id)
                        .where(
                            SyncExecution.configuration_id == uuid.UUID(cfg["id"]),
                            IncomingEvent.provider_event_id == prefix + label,
                            SyncExecution.state == state,
                        )
                    ).all()
                    return rows[0] if rows else None

            def destination_record():
                with Session(engine) as db:
                    row = db.scalar(
                        select(SyntheticATSRecord).where(
                            SyntheticATSRecord.connection_id == uuid.UUID(ats),
                            SyntheticATSRecord.kind == "requisition",
                        )
                    )
                    return (
                        {
                            "external_id": row.external_id,
                            "data": dict(row.payload),
                            "id": str(row.id),
                        }
                        if row
                        else None
                    )

            record = source_write(job)
            process = start()
            payload, status = deliver(
                record,
                job,
                "create",
                "job.created",
                occurred=datetime.fromisoformat(record["updated_at"])
                .astimezone(timezone(timedelta(hours=-5)))
                .isoformat(),
            )
            assert status == 202
            _, plan = wait(lambda: state_for("create"))
            assert plan.decision == "CREATE"
            dest = destination_record()
            assert dest and dest["data"]["title"] == job["title"]
            print(
                "PASS signed webhook → durable workflow → canonical CREATE → ATS identity"
            )
            # ATS-owned status changes independently; sync preserves it.
            mutate(
                f"connections/{ats}/execute",
                {
                    "name": "requisition.upsert",
                    "payload": {
                        "external_id": dest["external_id"],
                        "data": {**dest["data"], "status": "CLOSED"},
                    },
                },
            )
            job = {**job, "title": "Updated synthetic commercial title"}
            record = source_write(job)
            deliver(record, job, "update")
            _, plan = wait(lambda: state_for("update"))
            assert plan.decision == "UPDATE"
            assert destination_record()["data"]["status"] == "CLOSED"
            print("PASS authorized UPDATE preserves ATS recruiting status")
            with Session(engine) as db:
                before = db.scalar(
                    select(func.count())
                    .select_from(SyncOperation)
                    .join(SyncExecution, SyncExecution.id == SyncOperation.execution_id)
                    .where(SyncExecution.configuration_id == uuid.UUID(cfg["id"]))
                )
            body = json.dumps(payload).encode()
            body, headers = signed_request(hook["signing_secret"], body)
            duplicate = httpx.post(
                "http://127.0.0.1:8000/api/v1/webhooks/ingest/"
                + hook["endpoint"]["public_id"],
                content=body,
                headers=headers,
                timeout=10,
            )
            assert duplicate.status_code == 200
            time.sleep(0.5)
            with Session(engine) as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(SyncOperation)
                        .join(
                            SyncExecution,
                            SyncExecution.id == SyncOperation.execution_id,
                        )
                        .where(SyncExecution.configuration_id == uuid.UUID(cfg["id"]))
                    )
                    == before
                )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(SyntheticATSRecord)
                        .where(SyntheticATSRecord.connection_id == uuid.UUID(ats))
                    )
                    == 1
                )
            print("PASS duplicate event creates no extra ATS record or action")
            old = (
                datetime.fromisoformat(record["updated_at"]) - timedelta(seconds=60)
            ).isoformat()
            deliver(
                record, {**job, "title": "Stale obsolete title"}, "stale", occurred=old
            )
            _, stale = wait(lambda: state_for("stale", "HELD"))
            assert "STALE_SOURCE" in stale.evidence["conflict_reasons"]
            print("PASS stale source held with durable evidence")
            dest = destination_record()
            mutate(
                f"connections/{ats}/execute",
                {
                    "name": "requisition.upsert",
                    "payload": {
                        "external_id": dest["external_id"],
                        "data": {**dest["data"], "title": "Independent ATS edit"},
                    },
                },
            )
            job = {**job, "title": "Reviewed current CRM title"}
            record = source_write(job)
            deliver(record, job, "conflict")
            _, held = wait(lambda: state_for("conflict", "HELD"))
            assert "DESTINATION_CHANGED" in held.evidence["conflict_reasons"]
            conflict = next(
                c
                for c in get("sync/conflicts?status=OPEN&limit=100")["items"]
                if c["plan_id"] == str(held.id)
                and c["category"] == "DESTINATION_CHANGED"
            )
            review = get(f"sync/conflicts/{conflict['id']}")
            decision = {
                "action": "ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS",
                "reason": "Reviewed synthetic current source and destination records",
                "expected_revision": conflict["revision"],
                "expected_source_digest": review["current_source_digest"],
                "expected_destination_digest": review["current_destination_digest"],
                "request_key": str(uuid.uuid4()),
            }
            resolution = mutate(f"sync/conflicts/{conflict['id']}/resolve", decision)
            assert (
                mutate(f"sync/conflicts/{conflict['id']}/resolve", decision)["id"]
                == resolution["id"]
            )
            wait(lambda: destination_record()["data"]["title"] == job["title"])
            assert destination_record()["data"]["status"] == "CLOSED"
            print("PASS explicit optimistic review re-plans and queues existing worker")
            mutate(
                f"connections/{ats}/failure-injection",
                {
                    "enabled": True,
                    "scenario": "HTTP_503",
                    "attempts": 1,
                    "ttl_seconds": 300,
                },
                "PUT",
            )
            job = {**job, "title": "Recovered CRM title"}
            record = source_write(job)
            deliver(record, job, "retry")

            def retrying():
                with Session(engine) as db:
                    from app.models.webhook import IncomingEvent

                    return db.scalar(
                        select(WorkflowExecution)
                        .join(
                            IncomingEvent,
                            IncomingEvent.id == WorkflowExecution.event_id,
                        )
                        .where(
                            IncomingEvent.provider_event_id == prefix + "retry",
                            WorkflowExecution.state == "RETRY_SCHEDULED",
                        )
                    )

            retried = wait(retrying)
            stop(process)
            process = start()
            wait(lambda: state_for("retry"))
            with Session(engine) as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(RetrySchedule)
                        .where(RetrySchedule.execution_id == retried.id)
                    )
                    >= 1
                )
            print(
                "PASS transient ATS failure → durable retry → worker restart → success"
            )
            stop(process)
            # Reconciliation stays read-only even while it sees a new missing source.
            source_write({**job, "title": "Missing destination requisition"}, "job-2")
            dest = destination_record()
            mutate(
                f"connections/{ats}/execute",
                {
                    "name": "requisition.upsert",
                    "payload": {
                        "external_id": dest["external_id"],
                        "data": {**dest["data"], "location": "Montreal"},
                    },
                },
            )
            with Session(engine) as db:
                before = db.scalar(
                    select(func.count())
                    .select_from(SyncOperation)
                    .join(SyncExecution, SyncExecution.id == SyncOperation.execution_id)
                    .where(SyncExecution.configuration_id == uuid.UUID(cfg["id"]))
                )
            run = mutate(
                f"sync/configurations/{cfg['id']}/reconcile",
                {"limit": 100},
                expected=201,
            )
            findings = get(f"sync/reconciliation-runs/{run['id']}/findings?limit=100")[
                "items"
            ]
            categories = {f["category"] for f in findings}
            assert {"MISSING_DESTINATION", "DIVERGENT_AUTHORIZED_FIELDS"} <= categories
            finding = next(
                f for f in findings if f["category"] == "MISSING_DESTINATION"
            )
            preview = mutate(f"sync/findings/{finding['id']}/plan")
            assert preview["plan"]["decision"] == "CREATE"
            with Session(engine) as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(SyncOperation)
                        .join(
                            SyncExecution,
                            SyncExecution.id == SyncOperation.execution_id,
                        )
                        .where(SyncExecution.configuration_id == uuid.UUID(cfg["id"]))
                    )
                    == before
                )
            print("PASS real read-only reconciliation and optional CREATE preview")
            mutate("tenant/switch", {"tenant_id": beta["id"]})
            for path in [
                f"sync/configurations/{cfg['id']}",
                f"sync/plans/{held.id}",
                f"sync/conflicts/{conflict['id']}",
                f"sync/reconciliation-runs/{run['id']}",
            ]:
                assert client.get("/api/platform/" + path).status_code == 404
            assert all(
                c["id"] != cfg["id"] for c in get("sync/configurations")["items"]
            )
            mutate("tenant/switch", {"tenant_id": alpha["id"]})
            assert client.get("/synchronization").status_code == 200
            for path in [
                "sync/configurations",
                "sync/executions",
                "sync/operations",
                "sync/conflicts",
                "sync/reconciliation-runs",
                "sync/status",
            ]:
                response = client.get("/api/platform/" + path)
                assert not any(secret in response.text for secret in sensitive)
            assert not any(
                secret in (backend / ".local/phase7-worker.log").read_text()
                for secret in sensitive
            )
            print("PASS cross-tenant denial and response/log secret redaction")
            print("PASS Phase 7 HTTP evidence retained under " + prefix)
    finally:
        for process in workers:
            if process.poll() is None:
                stop(process)
        log.close()
        engine.dispose()


if __name__ == "__main__":
    main()
