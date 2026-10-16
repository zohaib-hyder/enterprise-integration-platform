# Incidents and deterministic correlation

OperationalAlert remains the source for deduplicated alerts and its existing acknowledge/resolve permissions. An incident is a separate operator case with cross-domain evidence, assignee, lifecycle, reasons and revision. It does not replace or silently resolve the source alert, workflow, sync conflict or delivery.

Newly emitted alerts synchronously add incident evidence in the same existing transaction. Historical retained alerts, signed rejected/conflicting receipts, open sync conflicts, dead-lettered executions and expired workflow leases are imported through explicit bounded `POST /api/v1/operations/incidents/refresh`. Loading the dashboard itself does not fabricate/backfill incidents. Each refresh scans bounded source rows in the selected window.

## Correlation keys

Keys contain the problem family, source domain and exact tenant-owned target UUID. They never contain just a tenant or a rounded time bucket. Connector alerts use explicit connection context; workflow alerts use explicit execution context; notification/calendar alerts validate their delivery reference. If no safe target link exists, the alert itself remains the target.

Repeated and terminal delivery alerts, unknown outcomes and calendar conflict evidence share a **delivery-specific** problem family. More specific observed categories can refine that same case. Terminal calendar failure and its calendar conflict alert therefore do not create two cases for one request. Workflow stall/lease-recovery evidence similarly shares an execution-specific lease family. Other categories remain separate unless their exact target/family agrees. A more severe new observation can raise severity; historical duplicate refresh does not undo an operator's adjustment.

Every source occurrence has a unique database evidence key. Re-reading the same alert occurrence/conflict does not create another incident/evidence row. Counts mean **distinct retained evidence observations**, not a guessed count of all historical failures. An alert snapshot imported after historical telemetry retention cannot reconstruct missing occurrences. Different source systems may contribute multiple real observations to one case.

Supported categories: WEBHOOK_INGESTION_FAILURE, WORKFLOW_STALLED, WORKFLOW_DEAD_LETTERED, CONNECTOR_UNHEALTHY, CREDENTIAL_FAILURE, SYNC_CONFLICT, SYNC_INCONSISTENCY, NOTIFICATION_DELIVERY_FAILURE, UNKNOWN_DELIVERY_OUTCOME, CALENDAR_CONFLICT and WORKER_RECOVERY_REQUIRED. A missing/outdated relationship remains explicit; no causal relationship is inferred from nearby timestamps.

## Lifecycle

OPEN/REOPENED → INVESTIGATING, MITIGATED or RESOLVED; INVESTIGATING → MITIGATED/RESOLVED; MITIGATED → INVESTIGATING/RESOLVED/REOPENED; RESOLVED → REOPENED. A genuinely new failure observation reopens a mitigated/resolved case. Re-reading existing evidence does not reopen it. Acknowledgement moves an open/reopened incident into investigation. Notes can be recorded without a status change.

Every edit requires the current integer revision and a bounded safe reason. Admins/engineers can adjust severity and assign an active user in the same tenant. Operations users can acknowledge, investigate and explicitly record lifecycle/resolution without configuration privileges. Incident resolution is a statement about the operator case; source objects and alerts retain their own states and controls. Resolution must not be read as evidence of an external effect.

IncidentEvidence and OperationsActivity are append-only under PostgreSQL triggers. Activity retains actor, tenant, incident/target UUID, correlation, time, reason and outcome. Severity/status transitions have explicit before/after outcomes; assignment activity preserves the assigned member UUID. New evidence and automatic category/severity changes are also audited. User reasons reject common credential/URL/markup/control-character patterns; they are not copied into request logs. This is a bounded safety guard, not a general-purpose PII classifier.

List/detail/activity/evidence APIs are paginated/tenant scoped. Revisions reject competing edits; tenant locks and unique source/problem keys serialize concurrent correlation. Historical incidents are not invented in migrations. No business failure is automatically repaired by creating or resolving a case.
