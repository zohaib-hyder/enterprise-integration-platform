# Worker operation and recovery

From the repository root, ensure local PostgreSQL is healthy, apply migrations and start the worker in its own terminal:

```sh
docker compose up -d postgres
cd backend
.venv/bin/alembic upgrade head
.venv/bin/python -m app.workers.runner
# Optional: --poll-seconds 0.5 --lease-seconds 30
```

Keep the backend API and frontend running in separate terminals to inspect Workflows, Executions and Failures. Worker health displays actual recent global infrastructure heartbeats (15-second freshness window) and tenant-specific state counts; stale heartbeat is not proof that a business action failed. A step is bounded to at most 10 seconds and the minimum lease is 15 seconds.

SIGINT/SIGTERM stop further polling, allow the current bounded local effect transaction to complete and mark the owned worker stopped. SIGKILL leaves the committed lease until expiration. Restart with the same command; the new UUID worker claims expired RUNNING work with a new fencing token. The old token cannot write checkpoints. SUCCEEDED/SKIPPED steps remain untouched. An interrupted unfinished attempt consumes its attempt budget, preventing unlimited crash retries; exhaustion creates a dead letter.

WAITING and RETRY_SCHEDULED due times survive process termination. Never edit cursor/steps/state, delete checkpoints or reset a volume to recover. Inspect persisted history first, correct tenant permissions/connection configuration if needed, and use an explicitly confirmed replay of an eligible failed/dead-lettered execution. The previous execution/history remains intact.

For repeatable synthetic fixtures, use `python -m app.seed_demo` with existing operator credentials. It creates four definitions across two demo tenants, delivers signed events through the same ingestion service and drives real engine transactions. Its temporary in-process driver is marked stopped afterward; start the foreground worker for ongoing processing. The fixed fixtures produce two successes, two retry successes and two permanent dead letters on an untouched demo. Re-running preserves operator changes and existing executions rather than resetting them.

Actual process verification (requires API, built frontend and private local credentials created by Phase 1 verification):

```sh
# From backend
PYTHONPATH=. .venv/bin/python ../scripts/verify_phase5_http.py
```

This retains synthetic evidence in PostgreSQL. It starts and stops only its own workers, submits HMAC-signed localhost webhooks, verifies retries and receipts, blocks an effect transaction using a compatible test-owned tenant lock, kills the leased worker, releases the lock and verifies recovery after lease expiry. It does not delete data or run a provider API. PostgreSQL concurrency tests create/drop only randomly named test-owned schemas, never application tables or the persistent volume.
