"""Real Phase 9 command-center demonstration on uniquely retained synthetic data.

First runs the Phase 8 HMAC/SIGKILL demo, then operates on its exact persisted
references. The single extra inconsistency is explicitly seeded and audited.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from app.connectors.webhook_sender import signed_request
from app.core.config import Settings
from app.core.security import password_hasher, utcnow
from app.db.session import build_engine
from app.models.identity import Membership, User
from app.models.integration import (
    IntegrationConnection,
    SyntheticATSRecord,
    SyntheticCRMRecord,
)
from app.models.notification import (
    NotificationDelivery,
    NotificationIntent,
    NotificationRule,
)
from app.models.operations import Incident
from app.models.synchronization import (
    SyncConfiguration,
    SyncConflict,
    SyncExecution,
    SyncOperation,
)
from app.models.tenant import Tenant  # noqa: F401 - register standalone verifier FK metadata
from app.models.workflow import WorkflowExecution
from app.services.audit import record
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main() -> None:
    backend = Path(__file__).resolve().parents[1] / "backend"
    credentials = json.loads((backend / ".local/http-verification.json").read_text())
    engine = build_engine(Settings())
    assert engine is not None
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("verify_phase8_http.py"))],
        cwd=backend,
        env={**os.environ, "PYTHONPATH": "."},
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert completed.returncode == 0, (
        "Phase 8 prerequisite live demonstration failed; inspect its retained worker log"
    )
    match = re.search(r"retained prefix (phase8-[a-z0-9]+-)", completed.stdout)
    assert match
    prefix = match[1]
    print(
        "PASS prerequisite: actual HMAC sync/delivery/calendar and SIGKILL demonstration "
        + prefix,
        flush=True,
    )
    workers = []
    log = (backend / ".local/phase9-worker.log").open("a")

    def start():
        process = subprocess.Popen(
            [sys.executable, "-m", "app.workers.runner", "--poll-seconds", ".2"],
            cwd=backend,
            env={**os.environ, "PYTHONPATH": "."},
            stdout=log,
            stderr=log,
        )
        workers.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)

    def wait(check, seconds=60):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            result = check()
            if result:
                return result
            time.sleep(0.15)
        raise AssertionError("Timed out waiting for real Phase 9 operational state")

    try:
        with httpx.Client(base_url="http://127.0.0.1:3000", timeout=30) as client:

            def get(path):
                result = client.get("/api/platform/" + path)
                assert result.status_code == 200, (path, result.status_code)
                return result.json()

            def mutate(path, data=None, method="POST", expected=200):
                csrf = get("auth/csrf")["csrf_token"]
                result = client.request(
                    method,
                    "/api/platform/" + path,
                    json=data or {},
                    headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": csrf},
                )
                assert result.status_code == expected, (path, result.status_code)
                return result.json()

            mutate(
                "auth/login",
                {"email": credentials["email"], "password": credentials["password"]},
            )
            tenants = get("tenants")
            alpha = next(t for t in tenants if t["slug"] == "demo-recruiting-alpha")
            beta = next(t for t in tenants if t["slug"] == "demo-recruiting-beta")
            mutate("tenant/switch", {"tenant_id": alpha["id"]})
            with Session(engine) as db:
                cfg = db.scalar(
                    select(SyncConfiguration).where(
                        SyncConfiguration.name == prefix + "sync"
                    )
                )
                assert cfg is not None
                cfg_id = cfg.id
                crm = cfg.source_connection_id
                ats = cfg.destination_connection_id
                slack = db.scalar(
                    select(IntegrationConnection.id).where(
                        IntegrationConnection.tenant_id == cfg.tenant_id,
                        IntegrationConnection.name == prefix + "slack",
                    )
                )
                sync = db.scalar(
                    select(SyncExecution)
                    .where(
                        SyncExecution.configuration_id == cfg.id,
                        SyncExecution.state == "SUCCEEDED",
                        SyncExecution.workflow_execution_id.is_not(None),
                    )
                    .order_by(SyncExecution.created_at)
                )
                job = db.get(WorkflowExecution, sync.workflow_execution_id)
                correlation = str(job.correlation_id)
                manual_rule = db.scalar(
                    select(NotificationRule.id).where(
                        NotificationRule.tenant_id == cfg.tenant_id,
                        NotificationRule.name == prefix + "SLACKMANUAL",
                    )
                )
            timeline = get("operations/timeline/" + correlation + "?limit=100")
            kinds = {x["event_type"] for x in timeline["items"]}
            assert {
                "event_receipts",
                "incoming_events",
                "transformation_attempts",
                "workflow_dispatch_records",
                "workflow_step_executions",
                "sync_plans",
                "sync_operations",
                "notification_intents",
                "notification_deliveries",
                "notification_delivery_attempts",
            } <= kinds
            assert all(
                item["provenance"] in {"CORRELATION_ID", "DURABLE_REFERENCE"}
                for item in timeline["items"]
            )
            print(
                "PASS 1: real signed CRM -> canonical -> workflow -> ATS -> Slack unified provenance",
                flush=True,
            )
            mutate("operations/incidents/refresh", {"hours": 24, "limit": 100})
            with Session(engine) as db:
                outage = db.scalar(
                    select(Incident)
                    .where(
                        Incident.tenant_id == uuid.UUID(alpha["id"]),
                        Incident.target_id == slack,
                        Incident.target_kind == "connection",
                    )
                    .order_by(Incident.created_at)
                )
                assert outage is not None
                outage_id = str(outage.id)
            before = get("operations/incidents/" + outage_id)
            mutate("operations/incidents/refresh", {"hours": 24, "limit": 100})
            after = get("operations/incidents/" + outage_id)
            assert before["occurrence_count"] == after["occurrence_count"]

            def preview(kind, identifier):
                return mutate(
                    "operations/recovery/preview",
                    {"target_kind": kind, "target_id": str(identifier)},
                )

            def request(p, action):
                return mutate(
                    "operations/recovery/requests",
                    {
                        "target_kind": p["target_kind"],
                        "target_id": p["target_id"],
                        "action": action,
                        "evidence_revision": p["evidence_revision"],
                        "request_key": str(uuid.uuid4()),
                        "reason": "Review exact synthetic operational evidence",
                    },
                    expected=201,
                )

            def confirm(r, expected=200):
                return mutate(
                    "operations/recovery/requests/" + r["id"] + "/confirm",
                    {"evidence_revision": r["evidence_revision"], "confirm": True},
                    expected=expected,
                )

            checked = confirm(
                request(preview("connection", slack), "RECHECK_CONNECTOR")
            )
            assert checked["status"] == "EXECUTED"
            print(
                "PASS 2: actual connector outage/retry incident deduplicated; reviewed synthetic health recheck",
                flush=True,
            )
            # New isolated source/workflow: the one-attempt fault is consumed by the first engine run.
            source = mutate(
                "connections",
                {
                    "name": prefix + "phase9-source",
                    "connector_type": "synthetic_crm",
                    "config": {},
                },
                expected=201,
            )["id"]
            definition = {
                "schema_version": 1,
                "trigger": {
                    "connection_id": source,
                    "source": "synthetic_crm",
                    "event_types": ["job.updated"],
                },
                "steps": [
                    {
                        "id": "notify",
                        "type": "MOCK_CONNECTOR_ACTION",
                        "config": {
                            "connection_id": str(slack),
                            "operation": "message.send",
                            "payload": {
                                "channel": "recruiting",
                                "text": "Synthetic Phase 9 controlled replay",
                            },
                        },
                        "retry": {"max_attempts": 1},
                    }
                ],
            }
            workflow = mutate(
                "workflows",
                {"name": prefix + "phase9-dead-letter", "definition": definition},
                expected=201,
            )
            mutate("workflows/" + workflow["id"] + "/activate", {"enabled": True})
            hook = mutate(
                "webhook-endpoints",
                {"connection_id": source, "source": "synthetic_crm"},
                expected=201,
            )

            def send(hook, data, external, event_id, occurred):
                body, headers = signed_request(
                    hook["signing_secret"],
                    json.dumps(
                        {
                            "schema_version": 1,
                            "source": "synthetic_crm",
                            "event_id": event_id,
                            "event_type": "job.updated",
                            "external_id": external,
                            "occurred_at": occurred,
                            "data": data,
                        }
                    ).encode(),
                )
                response = httpx.post(
                    "http://127.0.0.1:8000/api/v1/webhooks/ingest/"
                    + hook["endpoint"]["public_id"],
                    content=body,
                    headers=headers,
                    timeout=15,
                )
                assert response.status_code in {200, 202}

            mutate(
                f"connections/{slack}/failure-injection",
                {"enabled": True, "scenario": "HTTP_403", "attempts": 1},
                method="PUT",
            )
            process = start()
            send(
                hook,
                {
                    "title": "Synthetic operational incident",
                    "department": "Technology",
                    "location": "Toronto",
                    "status": "OPEN",
                },
                prefix + "replay-job",
                prefix + "dead-letter",
                utcnow().isoformat(),
            )

            def workflow_state(workflow_id, state):
                with Session(engine) as db:
                    row = db.scalar(
                        select(WorkflowExecution)
                        .where(
                            WorkflowExecution.workflow_id == uuid.UUID(workflow_id),
                            WorkflowExecution.state == state,
                        )
                        .order_by(WorkflowExecution.created_at.desc())
                    )
                    return str(row.id) if row else None

            failed = wait(lambda: workflow_state(workflow["id"], "DEAD_LETTERED"))
            stop(process)
            replay = request(preview("workflow", failed), "REPLAY_WORKFLOW")
            confirm(replay, expected=409)
            reviewer_password = secrets.token_urlsafe(28)
            reviewer_email = prefix + "reviewer@example.test"
            with Session(engine) as db, db.begin():
                user = User(
                    email=reviewer_email,
                    display_name="Synthetic independent operations reviewer",
                    password_hash=password_hasher.hash(reviewer_password),
                )
                db.add(user)
                db.flush()
                db.add(
                    Membership(
                        tenant_id=uuid.UUID(alpha["id"]),
                        user_id=user.id,
                        role="INTEGRATION_ENGINEER",
                    )
                )
                record(
                    db, "demo.reviewer_created", "user", user.id, uuid.UUID(alpha["id"])
                )
                reviewer_id = str(user.id)
            approval = next(
                a
                for a in get("operations/approvals?limit=100")["items"]
                if a["request_id"] == replay["id"]
            )
            with httpx.Client(base_url="http://127.0.0.1:3000", timeout=30) as other:

                def other_post(path, data):
                    token = other.get("/api/platform/auth/csrf").json()["csrf_token"]
                    return other.post(
                        "/api/platform/" + path,
                        json=data,
                        headers={
                            "Origin": "http://127.0.0.1:3000",
                            "X-CSRF-Token": token,
                        },
                    )

                assert (
                    other_post(
                        "auth/login",
                        {"email": reviewer_email, "password": reviewer_password},
                    ).status_code
                    == 200
                )
                assert (
                    other_post("tenant/switch", {"tenant_id": alpha["id"]}).status_code
                    == 200
                )
                assert (
                    other_post(
                        "operations/approvals/" + approval["id"] + "/decide",
                        {
                            "decision": "APPROVED",
                            "evidence_revision": replay["evidence_revision"],
                            "reason": "Independent review of exact synthetic replay receipts",
                        },
                    ).status_code
                    == 200
                )
            result = confirm(replay)
            assert result["result"]["outcome"] == "QUEUED"
            assert confirm(replay)["result"] == result["result"]
            process = start()
            wait(lambda: workflow_state(workflow["id"], "SUCCEEDED"))
            stop(process)
            print(
                "PASS 3: actual dead-letter -> read-only preview -> independent approval -> confirmed idempotent replay",
                flush=True,
            )

            def ats_writes():
                with Session(engine) as db:
                    return db.scalar(
                        select(func.count())
                        .select_from(SyncOperation)
                        .where(
                            SyncOperation.destination_connection_id == ats,
                            SyncOperation.outcome.in_(["CREATE", "UPDATE"]),
                        )
                    )

            mutate(
                f"connections/{slack}/failure-injection",
                {
                    "enabled": True,
                    "scenario": "HTTP_503",
                    "attempts": 1,
                    "retry_after": "10",
                },
                method="PUT",
            )
            baseline = ats_writes()
            intent = mutate(
                "notifications/manual",
                {
                    "rule_id": str(manual_rule),
                    "request_key": str(uuid.uuid4()),
                    "inputs": {"job_title": "Synthetic Phase 9 delivery retry"},
                },
                expected=201,
            )
            delivery = get("notifications/intents/" + intent["id"])["delivery"]
            delivery_id = delivery["id"]
            process = start()
            wait(
                lambda: (
                    get("notifications/deliveries/" + delivery_id)["status"]
                    == "RETRY_SCHEDULED"
                )
            )
            stop(process)
            retry = request(preview("delivery", delivery_id), "RETRY_NOTIFICATION")
            assert confirm(retry)["result"]["outcome"] == "SCHEDULE_PRESERVED"
            process = start()
            wait(
                lambda: (
                    get("notifications/deliveries/" + delivery_id)["status"]
                    == "SUCCEEDED"
                )
            )
            stop(process)
            assert ats_writes() == baseline
            print(
                "PASS 4: only failed notification scheduled/recovered; no ATS repeat",
                flush=True,
            )
            with Session(engine) as db:
                unknown = db.scalar(
                    select(NotificationDelivery).where(
                        NotificationDelivery.connection_id == slack,
                        NotificationDelivery.status == "UNKNOWN",
                    )
                )
                unknown_id = str(unknown.id)
            unsafe = request(preview("delivery", unknown_id), "RETRY_NOTIFICATION")
            blocked = confirm(unsafe, expected=409)
            assert blocked["status"] == "BLOCKED"
            reviewed = confirm(
                request(preview("delivery", unknown_id), "REVIEW_UNKNOWN_OUTCOME")
            )
            assert reviewed["result"]["outcome"] == "REVIEWED_UNRESOLVED"
            assert get("notifications/deliveries/" + unknown_id)["status"] == "UNKNOWN"
            print(
                "PASS 5: unknown outcome blocks unsafe retry; receipt review preserves unconfirmed state",
                flush=True,
            )
            # Real independent ATS edit followed by a signed CRM edit produces a Phase 7 conflict.
            with Session(engine) as db:
                a = db.scalar(
                    select(SyntheticATSRecord).where(
                        SyntheticATSRecord.connection_id == ats
                    )
                )
                a_external = a.external_id
                a_payload = dict(a.payload)
                c = db.scalar(
                    select(SyntheticCRMRecord).where(
                        SyntheticCRMRecord.connection_id == crm
                    )
                )
                c_external = c.external_id
                c_payload = dict(c.payload)
            mutate(
                f"connections/{ats}/execute",
                {
                    "name": "requisition.upsert",
                    "payload": {
                        "external_id": a_external,
                        "data": {
                            **a_payload,
                            "title": "Synthetic independently edited ATS title",
                        },
                    },
                },
            )
            source_record = mutate(
                f"connections/{crm}/execute",
                {
                    "name": "requisition.upsert",
                    "payload": {
                        "external_id": c_external,
                        "data": {
                            **c_payload,
                            "title": "Synthetic new CRM title requiring conflict review",
                        },
                    },
                },
            )["data"]
            conflict_hook = mutate(
                "webhook-endpoints",
                {"connection_id": str(crm), "source": "synthetic_crm"},
                expected=201,
            )
            process = start()
            send(
                conflict_hook,
                source_record["data"],
                c_external,
                prefix + "conflict",
                source_record["updated_at"],
            )

            def get_conflict():
                with Session(engine) as db:
                    row = db.scalar(
                        select(SyncConflict)
                        .join(
                            SyncExecution, SyncExecution.id == SyncConflict.execution_id
                        )
                        .where(
                            SyncExecution.configuration_id == cfg_id,
                            SyncConflict.status == "OPEN",
                        )
                    )
                    return str(row.id) if row else None

            conflict_id = wait(get_conflict)
            stop(process)
            mutate("operations/incidents/refresh", {"hours": 24, "limit": 100})
            with Session(engine) as db:
                incident = db.scalar(
                    select(Incident).where(
                        Incident.tenant_id == uuid.UUID(alpha["id"]),
                        Incident.target_kind == "conflict",
                        Incident.target_id == uuid.UUID(conflict_id),
                    )
                )
                assert incident is not None
                incident_id = str(incident.id)
            p = preview("incident", incident_id)
            assert next(
                a for a in p["actions"] if a["action"] == "REVIEW_SYNC_CONFLICT"
            )["eligible"]
            assert (
                confirm(request(p, "REVIEW_SYNC_CONFLICT"))["result"]["outcome"]
                == "REVIEW_REQUIRED"
            )
            conflict = get("sync/conflicts/" + conflict_id)
            mutate(
                "sync/conflicts/" + conflict_id + "/resolve",
                {
                    "action": "KEEP_DESTINATION",
                    "reason": "Explicitly keep reviewed synthetic ATS destination",
                    "expected_revision": conflict["conflict"]["revision"],
                    "expected_source_digest": conflict["current_source_digest"],
                    "expected_destination_digest": conflict[
                        "current_destination_digest"
                    ],
                    "request_key": str(uuid.uuid4()),
                },
            )
            print(
                "PASS 6: real sync conflict -> incident -> existing explicit digest/authority-safe review",
                flush=True,
            )
            with Session(engine) as db:
                recovered = db.scalar(
                    select(NotificationDelivery)
                    .join(
                        NotificationIntent,
                        NotificationIntent.id == NotificationDelivery.intent_id,
                    )
                    .where(
                        NotificationDelivery.connection_id == slack,
                        NotificationDelivery.status == "SUCCEEDED",
                        NotificationDelivery.attempt_count == 2,
                        NotificationIntent.sync_execution_id.is_(None),
                    )
                    .order_by(NotificationDelivery.created_at)
                )
                crash_corr = str(recovered.correlation_id)
            assert "LEASE_RECOVERED" in {
                i["status"] for i in get("operations/timeline/" + crash_corr)["items"]
            }
            print(
                "PASS 7: actual prerequisite SIGKILL/restart visible as durable lease-recovery timeline evidence",
                flush=True,
            )
            seeded = mutate(
                "notifications/manual",
                {
                    "rule_id": str(manual_rule),
                    "request_key": str(uuid.uuid4()),
                    "inputs": {
                        "job_title": "Synthetic deliberately overdue operational fixture"
                    },
                },
                expected=201,
            )
            seeded_delivery = get("notifications/intents/" + seeded["id"])["delivery"][
                "id"
            ]
            with Session(engine) as db, db.begin():
                row = db.get(NotificationDelivery, uuid.UUID(seeded_delivery))
                row.next_attempt_at = utcnow() - timedelta(minutes=10)
                record(
                    db,
                    "demo.seeded_overdue_delivery",
                    "delivery",
                    row.id,
                    row.tenant_id,
                )
            scan = mutate(
                "operations/reconciliation-runs",
                {"hours": 24, "limit": 100, "grace_seconds": 120},
                expected=201,
            )
            findings = get(
                "operations/reconciliation-runs/" + scan["id"] + "/findings?limit=100"
            )["items"]
            assert any(
                f["category"] == "DELIVERY_OVERDUE"
                and f["target_id"] == seeded_delivery
                for f in findings
            )
            assert (
                get("notifications/deliveries/" + seeded_delivery)["status"]
                == "PENDING"
            )
            print(
                "PASS 8: read-only reconciliation detects explicitly seeded overdue work; no automatic repair",
                flush=True,
            )
            operator = get("auth/me")["user"]["id"]
            for changes in [
                {"acknowledge": True},
                {"assigned_user_id": operator, "severity": "WARNING"},
                {"status": "RESOLVED"},
            ]:
                current = get("operations/incidents/" + incident_id)
                mutate(
                    "operations/incidents/" + incident_id,
                    {
                        "expected_revision": current["revision"],
                        "reason": "Reviewed actual synthetic conflict and preserved destination policy",
                        **changes,
                    },
                    method="PATCH",
                )
            history = get("operations/incidents/" + incident_id + "/activity")
            assert history["total"] >= 4
            print(
                "PASS 9: acknowledgement, assignment and resolution retain immutable actor/reason history",
                flush=True,
            )
            stale = request(
                preview("delivery", seeded_delivery), "ESCALATE_FOR_MANUAL_REVIEW"
            )
            mutate(
                "notifications/deliveries/" + seeded_delivery + "/cancel",
                {
                    "reason": "Change synthetic target before stale recovery confirmation"
                },
            )
            assert confirm(stale, expected=409)["status"] == "STALE"
            mutate("tenant/switch", {"tenant_id": beta["id"]})
            for path in [
                "operations/incidents/" + incident_id,
                "operations/timeline/" + correlation,
                "operations/recovery/requests/" + replay["id"],
            ]:
                assert client.get("/api/platform/" + path).status_code == 404
            mutate(
                "operations/recovery/preview",
                {"target_kind": "incident", "target_id": incident_id},
                expected=404,
            )
            assert (
                client.post(
                    "/api/platform/operations/recovery/preview", json={}
                ).status_code
                == 403
            )
            mutate("tenant/switch", {"tenant_id": alpha["id"]})
            assert credentials["password"] not in json.dumps(
                get("operations/overview?hours=24")
            )
            print(
                "PASS 10: cross-tenant incident/timeline/recovery denied; CSRF/Origin enforced; stale confirmation rejected",
                flush=True,
            )
            assert reviewer_id != operator
            print(
                "PASS all Phase 9 scenarios; retained source prefix " + prefix,
                flush=True,
            )
    finally:
        for process in workers:
            stop(process)
        log.close()
        engine.dispose()


if __name__ == "__main__":
    main()
