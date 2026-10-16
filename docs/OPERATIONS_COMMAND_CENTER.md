# Operations command center

Phase 9 adds `/operations` over the existing Northstar Staffing tenant data. It is a deterministic operations tool, not AI diagnosis. It introduces seven metadata tables: incidents, incident evidence, immutable activity, recovery requests, recovery approvals, reconciliation runs and findings. Existing business models, workers, retries, receipts and provider simulators remain authoritative. PostgreSQL is still the only Docker service.

## Data sources and metrics

`GET /api/v1/operations/overview?hours=24` accepts exactly 1, 24, 168 or 720 hours. Counts are real SQL aggregates over the active tenant and indexed time predicates. Window counts include retained webhook receipt outcomes, unique accepted events, workflow/sync/delivery states, dead letters and recovery request outcomes. A webhook receipt represents a retained tenant-attributable attempt, including duplicates; it is not a unique accepted event. Pre-attribution/signature failures that have no durable tenant receipt are not counted as if the platform had stored them.

Backlog, expired execution leases, open conflicts/alerts and scheduled delivery retries are **current snapshots**, independent of the selected creation window. FAILED/UNKNOWN delivery counts describe current states of records created in that window. The failure visualization instead groups actual failed workflow history events and classified delivery attempts by recorded hour/day; later success does not erase those failure records.

WorkerLease is global infrastructure without a tenant ID. Operations only counts workers referenced by the active tenant's current workflow/delivery leases. A heartbeat older than 30 seconds or a stopped worker is stale; no associated lease means UNKNOWN, not a claim that an idle global worker is healthy or dead. Connector health is a bounded sample of up to 100 tenant-owned connections; disabled takes precedence and checks older than five minutes are marked stale. The UI labels this bound explicitly. This view is not a live external provider probe.

## Timeline provenance

`GET /operations/timeline/{correlation_id}` reads the tenant's persisted correlation IDs and explicit references through receipts/events, normalization, dispatch/executions/steps/history, sync plans/operations/conflicts, notification intents/actions/attempts/receipts, linked calendar coordination, alerts and recovery activity. Missing direct correlation columns are traversed through existing foreign keys; no synthetic linkage columns or timestamp matching are needed. A standalone calendar action is not attributed to an unrelated CRM operation.

Each item exposes only event type, timestamp, status, source, UUID reference, safe summary, internal domain link and provenance (`CORRELATION_ID` or `DURABLE_REFERENCE`). No raw webhook body, template payload, secret, credential ciphertext or provider response is copied into the timeline. Shared correlation is grouping evidence, not independent proof of causation. Equal database timestamps use stable type/ID ordering, not an invented sub-transaction clock. States are projections of retained records, not invented historical status transitions.

Source reads cap at 100 rows each, with at most 100 items per response and an explicit bounded flag. Pagination operates on that bounded projection; it does not promise an unbounded export or snapshot consistency while the source is changing. Unavailable historical references are shown as unavailable evidence with escalation, not fabricated causes. Cross-tenant or wholly unknown correlations return 404.

## Navigation and permissions

The new section links the existing Workflows, Executions, Failures, Synchronization, Notifications, Alerts and Calendar pages. It does not replace those domain editors. All roles with `operations:read` can inspect safe metadata, timelines and runbooks. Existing `executions:recover` permits incident acknowledgement/notes/lifecycle, bounded scans and explicitly eligible low-risk requests. Admins/integration engineers manage assignment/severity, connection actions and reviewed replay/token refresh. Independent approval never bypasses technical receipt, authority or ownership guards.

The existing same-origin BFF exposes exact operations paths and bounded filters only. Origin/CSRF remains required for mutations, including POST preview. Pure preview does not mutate business or operations records; materializing an explicit reviewed request records the preview audit. See [INCIDENT_MANAGEMENT.md](INCIDENT_MANAGEMENT.md), [CONTROLLED_RECOVERY.md](CONTROLLED_RECOVERY.md), [OPERATIONAL_RECONCILIATION.md](OPERATIONAL_RECONCILIATION.md) and [RUNBOOKS.md](RUNBOOKS.md).

## Local commands

From the repository root, separate terminals:

```sh
cd backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

```sh
cd frontend
npm run build
npm run start
```

For ordinary processing, from backend:

```sh
.venv/bin/python -m app.workers.runner --poll-seconds 0.5
```

For the controlled demo, stop independently launched workers first. The verifier owns/stops its workers and runs Phase 8's real HMAC/SIGKILL prerequisite before the Phase 9 cases:

```sh
cd backend
PYTHONPATH=. .venv/bin/python ../scripts/verify_phase9_http.py
```

The verifier reads the existing ignored private synthetic operator file (prepared by the Phase 1 HTTP verifier if absent). It creates a uniquely named synthetic independent reviewer, produces actual engine/receipt evidence and explicitly seeds/audits one overdue action timestamp. It retains fixtures and never resets the volume. Normal development can use `npm run dev` instead of the built frontend.
