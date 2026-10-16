"""Bounded local measurements, no SLA or production throughput claim."""

import json
import platform
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from app.connectors.webhook_sender import signed_request
from app.main import app  # noqa:F401
from app.workflows import engine as worker
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]


def main():
    f = json.loads((ROOT / "backend/.local/phase11-fixtures.json").read_text())
    engine = create_engine(f["database_url"], pool_size=3, max_overflow=0)
    results = []
    prefix = "perf-" + uuid.uuid4().hex[:8]
    with httpx.Client(base_url="http://127.0.0.1:3000", timeout=30) as client:

        def get(path):
            r = client.get("/api/platform/" + path)
            assert r.status_code == 200, (path, r.status_code)
            return r.json()

        def post(path, data):
            csrf = get("auth/csrf")["csrf_token"]
            return client.post(
                "/api/platform/" + path,
                json=data,
                headers={"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": csrf},
            )

        assert (
            post(
                "auth/login", {"email": f["email"], "password": f["password"]}
            ).status_code
            == 200
        )
        assert post("tenant/switch", {"tenant_id": f["alpha"]}).status_code == 200

        def measure(name, fn, n=20, concurrency=1):
            def once(i):
                start = time.perf_counter()
                try:
                    r = fn(i)
                    okay = not isinstance(r, httpx.Response) or r.status_code in (
                        200,
                        201,
                        202,
                    )
                except Exception as exc:  # noqa: BLE001 - log only exception type, never message
                    print(
                        "measurement_error",
                        name,
                        type(exc).__name__,
                        getattr(getattr(exc, "orig", None), "sqlstate", None),
                        flush=True,
                    )
                    okay = False
                units = (int(r) if isinstance(r, (bool, int)) else 1) if okay else 0
                return (time.perf_counter() - start) * 1000, not okay, units

            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                samples = list(pool.map(once, range(n)))
            duration = time.perf_counter() - start
            values = sorted(x[0] for x in samples)
            results.append(
                {
                    "name": name,
                    "samples": n,
                    "concurrency": concurrency,
                    "duration_seconds": round(duration, 4),
                    "p50_ms": round(statistics.median(values), 3),
                    "p95_ms": round(
                        values[max(0, __import__("math").ceil(0.95 * n) - 1)], 3
                    ),
                    "p99_ms": None,
                    "errors": sum(x[1] for x in samples),
                    "completed_units": sum(x[2] for x in samples),
                    "throughput_per_second": round(
                        sum(x[2] for x in samples) / duration, 3
                    ),
                    "p99_reason": "Sample below 100; p99 intentionally omitted.",
                }
            )

        measure("health", lambda _: httpx.get("http://127.0.0.1:8000/health"), 30, 2)
        measure(
            "authenticated_connections_bff",
            lambda _: client.get("/api/platform/connections"),
            20,
            2,
        )
        measure(
            "operations_overview_bff",
            lambda _: client.get("/api/platform/operations/overview"),
            20,
            2,
        )
        measure(
            "incident_listing_bff",
            lambda _: client.get("/api/platform/operations/incidents"),
            20,
            2,
        )
        measure(
            "mock_ai_analysis_bff",
            lambda _: post(
                "ai/analyses",
                {
                    "analysis_type": "INCIDENT_EXPLANATION",
                    "target_kind": "incident",
                    "target_id": f["incident"],
                    "request_key": str(uuid.uuid4()),
                },
            ),
            5,
        )
        # Existing services; setup is outside measured operation windows.
        import sys

        sys.path.insert(0, str(ROOT / "backend/tests"))
        from app.models.synchronization import SyncPlan
        from app.services import operations
        from test_notifications import configured, manual, tick
        from test_synchronization import apply_plan, context, make_plan

        with Session(engine) as db:
            event_id = db.scalar(
                select(SyncPlan.event_id)
                .where(
                    SyncPlan.tenant_id == uuid.UUID(f["alpha"]),
                    SyncPlan.configuration_id == uuid.UUID(f["sync_configuration"]),
                )
                .order_by(SyncPlan.created_at.desc())
                .limit(1)
            )
        ids = (uuid.UUID(f["sync_configuration"]), event_id, None)
        measure(
            "sync_plan_and_existing_route_apply",
            lambda _: apply_plan(engine, make_plan(engine, ids)),
            5,
            2,
        )
        notify = configured(engine)
        for _ in range(8):
            manual(engine, notify)
        measure("notification_queue_processing", lambda _: tick(engine, notify[3]), 8)
        with Session(engine) as db, db.begin():
            worker_ids = [worker.register_worker(db) for _ in range(10)]
        measure("event_dispatch", lambda _: dispatch(engine, f["alpha"]), 5)
        measure(
            "workflow_claim_execute",
            lambda i: claim_run(engine, worker_ids[i]),
            10,
            2,
        )
        queries = []

        def counted(*args):
            queries.append(1)

        event.listen(engine, "before_cursor_execute", counted)
        with Session(engine) as db:
            operations.overview(db, context(db), 24)
        event.remove(engine, "before_cursor_execute", counted)
        # Actual signed ingestion through its existing local sender and a new endpoint.
        hook = post(
            "webhook-endpoints",
            {
                "connection_id": f["connections"]["synthetic_crm"],
                "source": "synthetic_crm",
            },
        ).json()
        assert "signing_secret" in hook

        def ingest(i):
            body, headers = signed_request(
                hook["signing_secret"],
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "synthetic_crm",
                        "event_id": prefix + str(i),
                        "event_type": "job.updated",
                        "external_id": "demo-job-1",
                        "occurred_at": "2030-01-15T12:00:00Z",
                        "data": {
                            "title": "Synthetic bounded load",
                            "department": "Technology",
                            "location": "Toronto",
                            "status": "OPEN",
                        },
                    }
                ).encode(),
            )
            return httpx.post(
                "http://127.0.0.1:8000/api/v1/webhooks/ingest/"
                + hook["endpoint"]["public_id"],
                content=body,
                headers=headers,
            )

        measure("signed_webhook_ingestion", ingest, 20, 2)
        with engine.connect() as conn:
            plan = conn.execute(
                text(
                    "EXPLAIN (FORMAT JSON) SELECT id FROM operation_incidents WHERE tenant_id=:tenant ORDER BY created_at DESC,id DESC LIMIT 25"
                ),
                {"tenant": uuid.UUID(f["alpha"])},
            ).scalar()
        report = {
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "hardware": "User-reported MacBook Air M1 / 8GB RAM",
                "database": "PostgreSQL 17 local Docker, existing limits 1 CPU/256MB",
                "resource_observation": "Full backend regressions may run concurrently; no process RSS/CPU sampler. No claim of production capacity.",
            },
            "schema": f["schema"],
            "results": results,
            "operations_overview_query_count": len(queries),
            "incident_explain": plan,
            "limitations": [
                "End-to-end BFF includes CSRF/session serialization and local HTTP client overhead.",
                "No p99 or SLA claim with these sample sizes.",
                "Workflow measurements include empty polls after backlog drains; inspect semantics before extrapolating.",
            ],
        }
        (ROOT / "docs/evidence/phase11-performance.json").write_text(
            json.dumps(report, indent=2)
        )
        print(
            json.dumps(
                {
                    "measurements": len(results),
                    "errors": sum(x["errors"] for x in results),
                    "overview_queries": len(queries),
                }
            )
        )
    engine.dispose()


def dispatch(engine, tenant):
    with Session(engine) as db, db.begin():
        return worker.dispatch(db, uuid.UUID(tenant))


def claim_run(engine, wid):
    with Session(engine) as db, db.begin():
        job = worker.claim(db, wid)
    if job:
        worker.run_claim(engine, job)
    return bool(job)


if __name__ == "__main__":
    main()
