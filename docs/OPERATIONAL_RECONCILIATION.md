# Read-only operational reconciliation

POST `/api/v1/operations/reconciliation-runs` accepts hours 1/24/168/720, per-source limit 1–100 and a pending-work grace of 30–3,600 seconds (default 120). It persists a run, immutable findings and audit metadata. It does not dispatch events, change workflow/delivery state, recreate intents, change identities or repair business records.

| Finding | Evidence and threshold |
| --- | --- |
| ACCEPTED_EVENT_UNDISPATCHED | Accepted event older than grace, an actually matching active trigger/version (including source/event type/prefix), and no durable dispatch record |
| WORKFLOW_STALLED | Pending/waiting/scheduled execution past its persisted due time plus grace |
| WORKER_LEASE_EXPIRED | Running workflow or delivery with an expired durable lease |
| ORPHANED_RETRY_SCHEDULE | Scheduled retry whose execution/step is no longer in the required retrying states; ownership FKs already prevent ordinary physical deletion orphans |
| NOTIFICATION_INTENT_MISSING | Committed CREATE/UPDATE operation and provably applicable, unchanged pre-outcome enabled rule, but no expected stable outbox key |
| DELIVERY_OVERDUE | Pending/scheduled delivery beyond due time plus grace, not normal fresh pending work |
| TERMINAL_FAILURE_WITHOUT_ALERT | Terminal delivery/dead-lettered workflow without the expected retained alert/reference evidence |
| SYNC_IDENTITY_MISSING | Real ATS operation without its expected canonical/connection/external-ID association |
| UNKNOWN_OUTCOME | Persisted delivery with no confirmed result; no auto resend |
| STALE_INCIDENT_STATE | Closed/mitigated incident whose explicit source has a newer unresolved state |

The missing-intent check never assumes today's rule configuration was active historically. It requires current rule/version modification times no later than the authoritative operation. Indeterminate historical rule transitions are counted as skipped. Phase 8 did not store a complete activation history; the scanner cannot reconstruct one. It does not report normal pre-rule outcomes as lost outbox work.

All candidates use tenant/time predicates, explicit references, bounded source pages and deterministic conditions. Summary includes actual per-category counts, sampled row counts, truncated sources, threshold and historical skips. Re-running creates another read-only evidence snapshot; it does not mutate earlier findings. A bounded sample is not an exhaustive consistency guarantee. Current source changes or existing Phase 6 telemetry retention can make older evidence unavailable; an absent retained reference is evidence to investigate, not proof of a historical event that never occurred.

GET run/findings APIs paginate stored metadata. A finding can be supplied to the same read-only recovery preview, which follows its explicit target reference. The planner either proposes an existing safe owning-service action or escalation. There is no bulk repair or background reconciler. Tenant locks favor a consistent local scan over throughput; worst-case rule checks remain bounded but can be slower for larger inventories. Operations users may request a scan; actual repair actions still enforce their own stronger permissions and approvals.

The HTTP demo deliberately creates/audits one **new synthetic overdue delivery timestamp** while its workers are stopped, confirms the finding and verifies it remains pending. Tests additionally suppress the outbox hook inside a fixture-owned schema to establish a real committed sync operation with a missing intent, without altering public application data. No production evidence or successful business outcomes are deleted to simulate failures.
