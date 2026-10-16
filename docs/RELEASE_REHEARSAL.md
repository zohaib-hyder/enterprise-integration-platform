# Isolated local release rehearsal

The actual release topology uses project `enterprise-integration-release`, separate release_database/release_private volumes, internal networks and localhost ports 3300 (frontend) / 18000 (API rehearsal override). It never attaches the original application volume. PostgreSQL remains 17.11. APP_ENV=test and generated independent private credentials permit local HTTP; real production requires TLS and Secure cookies, which are separately covered by production configuration/cookie regressions. The rehearsal does not claim production HTTPS validation.

Follow DEPLOYMENT_GUIDE.md for build, database startup, migration, bootstrap, synthetic seeding and health-checked startup. Then from backend/:

```sh
PYTHONPATH=. .venv/bin/python ../scripts/release_demo.py
```

The bounded HTTP demo logs in through the real Next BFF, switches tenant, configures fresh synthetic CRM/ATS/Slack/email connections and mapping/sync/template/rules, submits an actually signed CRM webhook to API ingress and waits for existing worker processing. It checks canonical transformation, workflow completion, ATS synchronization, injected email failure, authorized action-only retry, both notification outcomes, duplicate-event idempotency, operations dashboard/health, authorized mock AI evidence and human recommendation review yielding only a recovery preview. It asserts no recovery request is created by AI acceptance. Every run appends uniquely prefixed synthetic data; it does not reset fixtures. No real communication or paid inference occurs.

After successful work, stop worker gracefully, verify its lease stopped_at and own-worker probe behavior, restart and verify a new healthy lease. Exercise SIGKILL/restart only on the named isolated worker, preserving database/receipts. Stop/start API/frontend and reuse a retained synthetic session to verify durable state. Do not run docker/system-wide prune, delete volumes, reset original data or target unrelated containers. Retained isolated restore databases and failed run artifacts remain for inspection.

Execution details, pass counts, image identities and restart evidence are recorded in PHASE_12_REPORT.md / evidence/phase12-release-demo.json and phase12-verification.json. Incomplete/blocked scenarios must remain labeled, never inferred from static configuration. No remote CI, registry publication or deployment is part of this rehearsal.

## Operator diagnosis

| Symptom | Existing evidence / response |
|---|---|
| Rejected webhook | Correlation ID, endpoint receipts and error category; check signature/timestamp/size/disabled state, not raw secret logs |
| Stalled workflow | Executions timeline, due retry, worker heartbeat/lease/fencing; distinguish idle polling from a stuck claim |
| Connector outage | Connection reliability, quota/circuit history and alerts; respect half-open probe and retry-after |
| Expired credential | Credential metadata/refresh history and typed expiry/revocation; rotate through authorized lifecycle |
| Sync conflict | Source/destination versions, field authority and persisted conflict evidence; resolve with current revision |
| Notification failure | Per-action delivery attempts/receipts; retry only an eligible failed action, preserve ATS success; UNKNOWN stays held |
| Worker crash | Own lease/heartbeat, stopped_at and worker recovery alert; wait for lease/fencing, never blindly repeat writes |
| AI provider failure | AI usage telemetry, provider error classification and evidence/review state; mock default and fallback are explicit, no autonomous execution |

Structured request logs retain correlation IDs and safe route/status/category information. Operations views derive actual persisted metrics, incidents and retry/dead-letter/AI usage data. No external monitoring service is added. Configure deployment log rotation/retention; never publish raw backup/session/trace/env artifacts.

## Capacity review

Phase 11's ten short measurements had zero errors after fixing a real lock-order deadlock. Dispatch sampled empty polls and workflow sampling completed one step, so neither is saturated throughput. Tenant locking serializes work within each tenant; admission is bounded to 32 candidates with SKIP LOCKED, and fairness beyond that batch is not established. Workers are single-slot, API/worker pools each allow 2+2 connections, and operations overview used 22 SQL statements. Polling, coarse locks, connection budgets and query/retention behavior need representative measurement before scaling. No new optimization is introduced without evidence and the workflow engine is not rewritten. Local memory/CPU service caps are rehearsal constraints, not capacity guarantees.


## Non-destructive demo reset

A normal demo rerun appends uniquely prefixed records. For a clean demo view, create a **new database inside the isolated release PostgreSQL**, retaining the old one. Never use the original development service or volume.

1. Stop release worker/API/frontend with the exact project/file/env arguments from DEPLOYMENT_GUIDE.md; keep release PostgreSQL running.
2. Copy the mode-600 `.local/release/release.env` to a new private archived filename under `.local/release` so the previous database URL/keys remain recoverable.
3. Choose a new unused `phase12_demo_<unique-suffix>` name and create it (createdb refuses an existing name):

```sh
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml exec -T postgres sh -c 'exec createdb -U "$POSTGRES_USER" phase12_demo_CHOOSE_UNIQUE_SUFFIX'
```

4. Privately edit only the database-name portion of DATABASE_URL in `.local/release/release.env` to that new name. Keep POSTGRES_DB and all credentials/keys/origins unchanged; POSTGRES_DB remains the initial database used by container health and is not a request to reset storage. Do not print the secret URL.
5. Run the documented migration, bootstrap and seed commands against that new URL, then `up -d --wait --wait-timeout 240` for the release/rehearsal files and rerun release_demo.py. The same script ports/project remain valid.
6. To return to the previous demo, stop application services, restore the archived private env file and start them again. Both databases remain retained; no deletion or volume reset is needed.

This clean-view procedure is prepared; the executed Phase 12 recovery evidence uses its separately generated restore database, not an asserted execution of every manual reset step.
