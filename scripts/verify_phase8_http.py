"""Real BFF/HMAC/single-worker Phase 8 demo; retains uniquely prefixed synthetic data.

SIGKILL injects a pause after the synthetic action and before its checkpoint, in a
verification-only subprocess. The product worker has no artificial pause hook.
"""

import json
import os
import signal
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
from app.models.integration import SyntheticNotification
from app.models.notification import NotificationDelivery, NotificationIntent
from app.models.synchronization import SyncExecution, SyncOperation
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def main() -> None:
    backend = Path(__file__).resolve().parents[1] / "backend"
    credentials = json.loads(
        Path(
            os.environ.get(
                "PLATFORM_VERIFICATION_CREDENTIALS",
                str(backend / ".local/http-verification.json"),
            )
        ).read_text()
    )
    engine = build_engine(Settings())
    assert engine is not None
    prefix = "phase8-" + uuid.uuid4().hex[:10] + "-"
    log = (backend / ".local/phase8-worker.log").open("a")
    workers = []

    def start(crash_id=None):
        code = """from app.workers.runner import main; main()"""
        marker = backend / ".local" / f"{prefix}crash-marker"
        if crash_id:
            code = f"""import time,uuid
from pathlib import Path
from app.services import notifications
from app.workers.runner import main
original=notifications.complete
def paused(db,row,result):
    if row.id == uuid.UUID({str(crash_id)!r}):
        Path({str(marker)!r}).write_text(str(row.id))
        time.sleep(60)
    original(db,row,result)
notifications.complete=paused
main()
"""
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                "--poll-seconds",
                ".2",
                "--lease-seconds",
                "15",
            ],
            cwd=backend,
            env={**os.environ, "PYTHONPATH": "."},
            stdout=log,
            stderr=log,
        )
        workers.append(process)
        return process, marker

    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)

    def wait(check, seconds=70):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            result = check()
            if result:
                return result
            time.sleep(0.15)
        raise AssertionError("Timed out waiting for real Phase 8 state")

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
                assert response.status_code == expected, (path, response.status_code)
                return response.json()

            mutate(
                "auth/login",
                {"email": credentials["email"], "password": credentials["password"]},
            )
            tenants = get("tenants")
            alpha = next(t for t in tenants if t["slug"] == "demo-recruiting-alpha")
            beta = next(t for t in tenants if t["slug"] == "demo-recruiting-beta")
            mutate("tenant/switch", {"tenant_id": alpha["id"]})

            def conn(kind):
                return mutate(
                    "connections",
                    {
                        "name": prefix + kind,
                        "connector_type": "synthetic_" + kind,
                        "config": {},
                    },
                    expected=201,
                )["id"]

            crm, ats, slack, email, calendar = [
                conn(k) for k in ["crm", "ats", "slack", "email", "calendar"]
            ]
            mapping = mutate(
                "mappings",
                {
                    "name": prefix + "mapping",
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

            def rule(channel, trigger):
                t = mutate(
                    "notifications/templates",
                    {
                        "name": prefix + channel + trigger,
                        "channel": channel,
                        "subject": "Synthetic {job_title}"
                        if channel == "EMAIL"
                        else "",
                        "body": "{job_title}: {sync_outcome} · {requisition_reference} · {correlation_id}",
                    },
                    expected=201,
                )
                version = get(f"notifications/templates/{t['id']}/versions")["items"][
                    0
                ]["id"]
                mutate(
                    f"notifications/templates/{t['id']}/activate",
                    {"version_id": version},
                )
                return mutate(
                    "notifications/rules",
                    {
                        "name": prefix + channel + trigger,
                        "enabled": True,
                        "trigger": trigger,
                        "connection_id": slack if channel == "SLACK" else email,
                        "destination": "recruiting"
                        if channel == "SLACK"
                        else "recruiter@example.test",
                        "template_version_id": version,
                        "sync_configuration_id": cfg["id"]
                        if trigger != "MANUAL"
                        else None,
                    },
                    expected=201,
                )

            rule("SLACK", "SYNC_CREATE")
            rule("EMAIL", "SYNC_CREATE")
            rule("EMAIL", "SYNC_UPDATE")
            manual_rule = rule("SLACK", "MANUAL")

            def inject(connection, scenario, attempts=1):
                mutate(
                    f"connections/{connection}/failure-injection",
                    {
                        "enabled": True,
                        "scenario": scenario,
                        "attempts": attempts,
                        "retry_after": "2" if scenario == "HTTP_503" else None,
                    },
                    method="PUT",
                )

            def sync_count():
                with Session(engine) as db:
                    return db.scalar(
                        select(func.count())
                        .select_from(SyncOperation)
                        .where(
                            SyncOperation.destination_connection_id == uuid.UUID(ats)
                        )
                    )

            def source(data):
                return mutate(
                    f"connections/{crm}/execute",
                    {
                        "name": "requisition.upsert",
                        "payload": {"external_id": prefix + "job", "data": data},
                    },
                )["data"]

            def send(record, data, label, event_type="job.updated"):
                payload = {
                    "schema_version": 1,
                    "source": "synthetic_crm",
                    "event_id": prefix + label,
                    "event_type": event_type,
                    "external_id": record["external_id"],
                    "occurred_at": record["updated_at"],
                    "data": data,
                }
                body, headers = signed_request(
                    hook["signing_secret"], json.dumps(payload).encode()
                )
                response = httpx.post(
                    "http://127.0.0.1:8000/api/v1/webhooks/ingest/"
                    + hook["endpoint"]["public_id"],
                    content=body,
                    headers=headers,
                    timeout=10,
                )
                assert response.status_code in {200, 202}
                return response.status_code

            def rows():
                with Session(engine) as db:
                    return [
                        (r.id, r.channel, r.status, r.attempt_count)
                        for r in db.scalars(
                            select(NotificationDelivery).where(
                                NotificationDelivery.connection_id.in_(
                                    [
                                        uuid.UUID(slack),
                                        uuid.UUID(email),
                                        uuid.UUID(calendar),
                                    ]
                                )
                            )
                        )
                    ]

            def delivered(channel, minimum):
                return (
                    sum(c == channel and s == "SUCCEEDED" for _, c, s, _ in rows())
                    >= minimum
                )

            inject(email, "HTTP_503")
            process, _ = start()
            job = {
                "title": "Data: Synthetic Phase 8 engineer",
                "department": "Technology",
                "location": "Toronto",
                "status": "OPEN",
            }
            record = source(job)
            send(record, job, "create", "job.created")
            wait(lambda: delivered("SLACK", 1))
            wait(lambda: delivered("EMAIL", 1))
            assert sync_count() == 1
            with Session(engine) as db:
                message = db.scalar(
                    select(SyntheticNotification).where(
                        SyntheticNotification.connection_id == uuid.UUID(slack)
                    )
                )
                assert "[redacted]" in message.text
            initial = rows()
            assert next(a for _, c, _, a in initial if c == "SLACK") == 1
            assert next(a for _, c, _, a in initial if c == "EMAIL") == 2
            print(
                "PASS 1/4: signed CREATE -> ATS and Slack; failed email alone retried",
                flush=True,
            )
            changed = {**job, "title": "Synthetic Phase 8 senior engineer"}
            record2 = source(changed)
            send(record2, changed, "update")
            wait(lambda: delivered("EMAIL", 2))
            assert sync_count() == 2
            print("PASS 2: ATS UPDATE -> email delivery", flush=True)
            rule("SLACK", "SYNC_UPDATE")
            inject(slack, "HTTP_503")
            changed2 = {**changed, "location": "Vancouver"}
            record3 = source(changed2)
            send(record3, changed2, "retry")
            wait(lambda: delivered("SLACK", 2))
            wait(lambda: delivered("EMAIL", 3))
            assert sync_count() == 3
            print("PASS 3: Slack transient retry; ATS outcome not repeated", flush=True)
            before = rows()
            assert send(record, job, "create", "job.created") == 200
            time.sleep(1)
            assert len(rows()) == len(before) and sync_count() == 3
            print(
                "PASS 5: duplicate webhook creates no duplicate intent or provider effect",
                flush=True,
            )
            stop(process)

            def queue_manual():
                intent = mutate(
                    "notifications/manual",
                    {
                        "rule_id": manual_rule["id"],
                        "request_key": str(uuid.uuid4()),
                        "inputs": {"job_title": "Synthetic recovery case"},
                    },
                    expected=201,
                )
                return get("notifications/intents/" + intent["id"])["delivery"]["id"]

            crash_id = queue_manual()
            crasher, marker = start(crash_id)
            wait(marker.exists, 20)
            os.kill(crasher.pid, signal.SIGKILL)
            crasher.wait(timeout=10)
            with Session(engine) as db:
                row = db.get(NotificationDelivery, uuid.UUID(crash_id))
                assert row.status == "RUNNING"
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(SyntheticNotification)
                        .where(
                            SyntheticNotification.connection_id == uuid.UUID(slack),
                            SyntheticNotification.idempotency_key
                            == row.idempotency_key,
                        )
                    )
                    == 0
                )
            process, _ = start()
            wait(
                lambda: (
                    get("notifications/deliveries/" + crash_id)["status"] == "SUCCEEDED"
                )
            )
            assert (
                get("notifications/deliveries/" + crash_id)["attempt_count"] == 2
                and sync_count() == 3
            )
            print(
                "PASS 6: SIGKILL after provider action before checkpoint -> rollback/lease recovery, one committed delivery",
                flush=True,
            )
            inject(slack, "READ_TIMEOUT")
            unknown = queue_manual()
            wait(
                lambda: (
                    get("notifications/deliveries/" + unknown)["status"] == "UNKNOWN"
                )
            )
            result = mutate(
                "notifications/deliveries/" + unknown + "/review",
                {"reason": "Inspect uncertain synthetic provider receipt"},
            )
            assert result["resolved"] is False
            mutate(
                "notifications/deliveries/" + unknown + "/retry",
                {"reason": "Verify unsafe resend is rejected"},
                expected=409,
            )
            time.sleep(0.5)
            assert get("notifications/deliveries/" + unknown)["attempt_count"] == 1
            print(
                "PASS 7: unknown timeout held for review; blind resend rejected",
                flush=True,
            )
            start_time = datetime.now(UTC) + timedelta(days=365)
            end_time = start_time + timedelta(hours=1)
            caldata = {
                "connection_id": calendar,
                "calendar": "interviews",
                "title": "Synthetic Phase 8 interview",
                "starts_at": start_time.isoformat(),
                "ends_at": end_time.isoformat(),
                "participants": ["recruiter@example.test", "candidate@example.test"],
                "request_key": str(uuid.uuid4()),
            }

            def calendar_wait(request):
                return wait(
                    lambda: (
                        r
                        if (r := get("calendar/requests/" + request["id"]))["delivery"][
                            "status"
                        ]
                        in {"SUCCEEDED", "FAILED"}
                        else None
                    )
                )

            request = mutate("calendar/events", caldata, expected=201)
            assert (
                mutate("calendar/events", caldata, expected=201)["id"] == request["id"]
            )
            created = calendar_wait(request)
            assert created["delivery"]["status"] == "SUCCEEDED"
            event_id = created["request"]["event_id"]
            event = get("calendar/events/" + event_id)
            overlap = mutate(
                "calendar/events",
                {
                    **caldata,
                    "title": "Overlapping synthetic interview",
                    "request_key": str(uuid.uuid4()),
                },
                expected=201,
            )
            held = calendar_wait(overlap)
            assert held["delivery"]["error_category"] == "CONFLICT"
            availability = get(
                "calendar/availability?"
                + str(
                    httpx.QueryParams(
                        {
                            "connection_id": calendar,
                            "calendar": "interviews",
                            "starts_at": start_time.isoformat(),
                            "ends_at": end_time.isoformat(),
                        }
                    )
                )
            )
            assert not availability["available"]
            print(
                "PASS 9: overlapping calendar action held safely with persisted conflict",
                flush=True,
            )
            reschedule = {k: v for k, v in caldata.items() if k != "connection_id"}
            reschedule.update(
                starts_at=(start_time + timedelta(hours=2)).isoformat(),
                ends_at=(end_time + timedelta(hours=2)).isoformat(),
                request_key=str(uuid.uuid4()),
                expected_updated_at=event["updated_at"],
            )
            moved = calendar_wait(
                mutate(
                    "calendar/events/" + event_id + "/reschedule",
                    reschedule,
                    expected=201,
                )
            )
            assert moved["delivery"]["status"] == "SUCCEEDED"
            event = get("calendar/events/" + event_id)
            cancelled = calendar_wait(
                mutate(
                    "calendar/events/" + event_id + "/cancel",
                    {
                        "request_key": str(uuid.uuid4()),
                        "expected_updated_at": event["updated_at"],
                    },
                    expected=201,
                )
            )
            assert cancelled["delivery"]["status"] == "SUCCEEDED"
            assert get("calendar/events/" + event_id)["status"] == "CANCELLED"
            print(
                "PASS 8: stable calendar event created, rescheduled and cancelled",
                flush=True,
            )
            mutate("tenant/switch", {"tenant_id": beta["id"]})
            for path in [
                "notifications/deliveries/" + crash_id,
                "notifications/deliveries/" + unknown + "/attempts",
                "calendar/events/" + event_id,
                "calendar/requests/" + request["id"],
            ]:
                assert client.get("/api/platform/" + path).status_code == 404
            assert (
                client.post("/api/platform/calendar/events", json={}).status_code == 403
            )
            mutate("tenant/switch", {"tenant_id": alpha["id"]})
            with Session(engine) as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(NotificationIntent)
                        .where(
                            NotificationIntent.sync_execution_id.in_(
                                select(SyncExecution.id).where(
                                    SyncExecution.configuration_id
                                    == uuid.UUID(cfg["id"])
                                )
                            )
                        )
                    )
                    == 5
                )
            print(
                "PASS 10: cross-tenant evidence denied; Origin enforced; scoped outbox count verified",
                flush=True,
            )
            safe = get("notifications/metrics")
            assert credentials["password"] not in json.dumps(safe)
            print("PASS all Phase 8 scenarios; retained prefix " + prefix, flush=True)
    finally:
        for process in workers:
            stop(process)
        log.close()
        engine.dispose()


if __name__ == "__main__":
    main()
