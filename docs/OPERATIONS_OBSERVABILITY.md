# Operations observability

The Connections panels and Alerts page read actual persisted PostgreSQL data. There is no fake success rate, extrapolated throughput, invented historical observation, distributed tracing, Prometheus service or external alert delivery.

## Metric definitions

GET `/api/v1/operations/metrics?hours=24` requires operations:read and derives the current tenant from the authenticated session. Hours is bounded 1–168 and clipped to the configured retention horizon. Returned since/checked_at and basis explain the window.

| Metric | Persisted basis |
|---|---|
| operation_count | Committed connector observations in the window, including gate deferrals |
| admitted_operation_count | Observations admitted to the local connector/fault simulation after credential checks |
| success/failure/deferred_count | Recorded outcome classifications; not inferred business writes |
| operation_retry_count | Observations with a workflow step attempt greater than one |
| retry_count | Actual Phase 5 RetrySchedule rows created in the window |
| rate_limit_count | Observations classified RATE_LIMITED, local quota or explicit synthetic HTTP 429 |
| circuit_transition_count | Persisted actual CLOSED/OPEN/HALF_OPEN transitions |
| credential_refresh_count/success_count | Actual refresh attempts; NOT_NEEDED is not an attempt |
| latency average/maximum | Measured monotonic local admission/operation/outcome processing, excluding commit; null with no samples |
| workflow_backlog | Current tenant execution state counts, not window-limited |
| dead_letter_count | Dead letters created in the window |
| worker_lease_recovery_count | Actual LEASE_RECOVERED history rows in the window |
| connection_health | Current persisted connection-health state counts |

A transaction that cannot acquire database ownership or is killed/rolled back before evidence commits may have no observation. No missing attempts are synthesized. Successful idempotency lookups are actual operation responses, but an existing workflow action receipt bypasses the connector completely and does not add observations. Seeding invokes real local operations and may add observation counts even when business fixtures remain idempotent. Window counts from old workflow tables are real existing evidence; migration does not fabricate old connector observations/alerts.

## Retention

`OPERATION_RETENTION_DAYS` defaults to 7 (1–30); `OPERATION_RETENTION_MAX_ROWS` defaults to 5000 per tenant (100–20000). Lazy bounded cleanup after observed operations removes expired/excess Phase 6 connector observations, circuit transitions, refresh attempts and alert lifecycle rows in batches up to 100 per table. Old rate windows are cleaned after 240 seconds. The cap is enforced as operations arrive; inactive tenants are not background-scanned. Counts are from retained evidence, not lifetime metrics.

Original business data, credentials, inbox/receipts, canonical state, workflow execution/history/retries, dead letters and Phase 1 audit records are never pruned by this maintenance. Alert summary rows retain first/last seen and actual occurrence counts. Active alerts are not deleted to hide an incident. Retention coverage/limits accompany metrics. Appropriate tenant/connection/time indexes bound local reads; no scale or load benchmark is claimed.

## Alert lifecycle

Categories: CONNECTION_UNHEALTHY, CREDENTIAL_EXPIRED, REFRESH_FAILED, RATE_LIMIT_EXCEEDED, CIRCUIT_OPEN, WORKFLOW_DEAD_LETTERED, WORKER_RECOVERY and REPEATED_CONNECTOR_FAILURE. Severity is code-owned INFO/WARNING/CRITICAL. Context allowlists both field names and safe values/UUIDs; no payload, exception text, name/email, nonce, token or secret is copied into an alert or audit.

A unique tenant/category/resource key deduplicates connection incidents and groups workflow dead letters/recoveries by definition. Actual source operation/execution/history IDs deduplicate repeated emission of the same signal. Recurrence updates last_seen and occurrence_count; a new failure reopens a resolved row. OPEN → ACKNOWLEDGED → RESOLVED actions record actor/correlation and audit history. Repeated acknowledgement/resolution is idempotent. Acknowledgement does not repair a connection or reset a breaker.

Admins/engineers may acknowledge and resolve. Operations users may inspect and acknowledge, **not resolve or configure reliability/credentials/faults**. All backend permissions are revalidated under tenant locking; hiding UI controls is not authorization. Successful local operations automatically resolve applicable unhealthy/expired/refresh/breaker/repeated-failure alerts. Rate-limit, workflow-dead-letter and worker-recovery alerts remain operator-managed; a terminal workflow is not made successful by resolving its alert.

APIs:

- GET `/api/v1/alerts` with bounded pagination, status/severity/category filters
- GET `/alerts/{id}` and `/history`
- POST `/alerts/{id}/acknowledge` and `/resolve` with current tenant, required permission, Origin and CSRF
- GET `/operations/metrics`

Connections provides current controls and masked credentials; Alerts provides actual metrics, filters, safe context and history. Both show loading, empty, unauthorized and error states. The BFF forwards only the exact allowed paths/filters and safe Retry-After, correlation and operation identifiers; no secret reveal, arbitrary proxy or tenant override exists.

## Local demonstration and limits

Run `PYTHONPATH=. .venv/bin/python ../scripts/verify_phase6_http.py` from backend with both local servers running and existing private Phase 1 synthetic operator credentials. It verifies actual worker state, alerts, refresh records, action receipts and metric counts against PostgreSQL. Each uniquely named run is reviewable evidence; it does not delete/reset earlier data. Circuit/global worker heartbeats are operational signals, not proof of a real provider connection. The current single-slot/coarse tenant locking design favors transparent local recovery over throughput. Independent load/security audit, browser visual E2E, real OAuth/provider transport, SLA monitoring and external alert delivery remain unimplemented/unverified.
