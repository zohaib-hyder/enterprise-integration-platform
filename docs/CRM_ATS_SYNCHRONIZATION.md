# CRM to ATS synchronization

Phase 7 extends Northstar Staffing's existing tenant platform. Commercial requisitions originate in the synthetic CRM. Recruiting users own the ATS recruiting status. Signed `job.created` and `job.updated` events can now create and update local ATS requisitions without overwriting recruiting status. This is actual PostgreSQL-backed execution, not provider connectivity.

## Configuration and versions

A tenant owns each source CRM/destination ATS route. Both connections must belong to that tenant and have the required synthetic connector types. One configuration owns a route; a different tenant or connection has an independent namespace. Configuration versions pin an immutable Phase 4 transformation version, job event types, identity matching rule, destination field mapping, create/update flags and `HOLD` conflict policy. Activation uses the configuration revision as an optimistic precondition, creates/activates a Phase 5 workflow and increments the revision. Deactivation also deactivates dispatch. Applying an already planned operation rechecks enabled state and active configuration version.

Versions cannot be updated or deleted in PostgreSQL. Creating a version does not activate it. Historical plans retain their version and digest. The existing tenant field authority registry is evaluated when planning and again when applying; a version cannot invent an authority override. Phase 4's `ORIGIN` fields are authorized only when provenance identifies this originating CRM connection. `REVIEW`, missing ownership and another source's provenance hold writes.

The ATS v1 contract supports title, department, location and status. Destination mappings currently permit identity mappings for title, department and location. ATS status is preserved; a new ATS requisition starts `OPEN`. Canonical commercial rate, description and future client account/scheduling fields are not sent to ATS v1. Unsupported mapping fields are rejected rather than discarded silently. A create missing any required ATS field is `INVALID`.

## Planner

`synchronization/planner.py::calculate` is pure, deterministic and performs no writes. The service gathers tenant-scoped canonical identity, pinned mapping, authority, current synthetic destination and last successful synchronization baseline. It validates its output against `PlanEvidence`, sorts decisions and conflict reasons and hashes canonical JSON with SHA-256. Equivalent aware source timestamps normalize to UTC and compare as instants. The plan records source/destination identities, normalized fields, proposed before/after changes, preserved fields, authority, timestamps, current provider record digests, configuration/mapping versions and a code-owned explanation.

Decisions are `CREATE`, `UPDATE`, `NO_OP`, `CONFLICT`, `SKIP` or `INVALID`. Stale source timestamps and rejected canonical governance hold writes. An independent change to a synchronized CRM-owned destination field differs from the saved baseline and becomes `DESTINATION_CHANGED`. A change to ATS status is preserved and does not block an authorized title update. A mapped record with no trusted baseline and divergent data requires review. Apply also checks source and destination record digests, canonical high-water time, current authority and identity links under the existing tenant/connection locks.

Previews persist reviewable plans and preview execution evidence but do not normalize/create identities or write ATS. Execution plans are produced by verified events or explicit reviewed recovery. Preview and execution business identities are distinct. A preview is not an operational success.

## Identity and atomic upsert

The Phase 4 `ExternalRecordMapping` registry is the identity source of truth. Its namespace is tenant + connection + kind + external ID. A successful create links the canonical ID to `sync:<canonical UUID>` on the destination connection. Existing destination identity mappings are used first. More than one mapping for a canonical destination or an external ID linked to another canonical record holds writes.

Optional pre-existing matching is exactly title + department + location, case-sensitive after source normalization. The query is scoped to this ATS connection and returns at most two matches. One candidate can be linked after a successful no-op; two candidates produce `AMBIGUOUS_MATCH`. An existing unmapped row at the generated stable ID, or a successful historical action missing its identity link, requires identity review; its fields are never blindly overwritten. There is no fuzzy matching or assumption that CRM IDs equal ATS IDs. An absent mapped destination is a stale identity requiring explicit review.

The durable business key hashes configuration version, source identity/version/data, observed destination version and operation decision. Its action key is `sync:<business digest>`, stable across retries of a saved plan. A tenant mutex coordinates planning/applying; destination connection locking is shared with one-off simulator writes. External-ID uniqueness, business-key uniqueness and operation receipt uniqueness suppress duplicate creates/actions.

ATS `requisition.upsert` runs through Phase 6's reliability layer and the existing Phase 2 adapter. The local provider effect, destination identity, sync receipt and Phase 5 checkpoint commit in one PostgreSQL transaction. A connector failure rolls back its effect savepoint while quotas, breaker evidence and the worker's durable retry can commit. There is no separate synchronization scheduler or retry engine. This local transaction is the supported idempotency contract; it must not be presented as exactly-once behavior for remote providers. An imported/inconsistent unknown receipt holds with `UNKNOWN_WRITE_OUTCOME` rather than repeating an unsafe write.

## Existing worker integration

The allowlisted steps are `PLAN_CRM_ATS_SYNC`, `APPLY_CRM_ATS_SYNC`, and `RECORD_SYNC_OUTCOME`. Planning verifies the accepted event's tenant/source/trigger and calls Phase 4 normalization with the pinned mapping. Applying reads the prior typed checkpoint and delegates to the reliability-protected ATS action. Recording returns the persisted outcome. The original dispatch deduplication, lease token/expiry checks, cancellation, replay checkpoints and bounded retries remain in force.

A planning workflow can finish successfully with a sync execution `HELD`: its task of recording a safe decision is complete, but no synchronization write succeeded. UI status separates these states. Operator write recovery creates an immutable workflow version containing the same allowlisted steps, seeds the reviewed plan checkpoint and queues apply/record in the existing worker. It does not activate that recovery version for automatic event dispatch. A verified source event is still required as the execution ownership anchor. Apply rechecks state after review; subsequent edits produce a new hold.

## API and UI

All APIs are under `/api/v1/sync`. GET routes cover configurations and versions, plans, executions/details, operations/details, conflicts/details/resolutions, reconciliation runs/findings and status. POST routes create configurations/versions, activate/deactivate, preview, resolve a conflict, start reconciliation and generate a finding plan. Pagination is bounded (1–100 rows, offset at most 100000); execution filters support configuration/state, operation filters execution and conflict filters status. The existing BFF exposes only these explicit paths and allowlisted query parameters.

The Synchronization dashboard provides connector selectors, immutable version editing, event/field controls, current connection/worker health, activation, preview field comparisons, history, operation receipts, conflict review and read-only reconciliation. No lifetime performance or fabricated integration metrics are shown. Operations users inspect and can keep/skip reviewed destination updates; connector writes, linking, configuration and reconciliation initiation require admin/engineer management permission.

## Repeatable demonstration

Keep API and built frontend running on loopback; stop separately started workers so the demonstration can control its own process. PostgreSQL remains running with the existing volume.

```sh
# Repository root, API terminal
cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
# Repository root, frontend terminal
cd frontend
npm run build
npm run start
# Repository root, demonstration terminal
cd backend
# Only when the private synthetic operator file has not been prepared:
.venv/bin/python ../scripts/verify_phase1_http.py
PYTHONPATH=. .venv/bin/python ../scripts/verify_phase7_http.py
```

The script signs actual HTTP webhooks, exercises the Next BFF, starts/stops only its own worker and verifies persisted CREATE, UPDATE, duplicate suppression, stale hold, destination conflict/review, transient retry/restart, reconciliation, cross-tenant denial and secret redaction. Each run retains uniquely named `phase7-<run>-` records. It never resets a volume or deletes application data. Broader forced-interruption/replay regressions remain in the Phase 5 verification script and PostgreSQL tests. Notification workflows and real provider integrations remain Phase 8/future scope.

Frontend testing uses one Vitest worker; the production build uses two workers to fit the development machine. Connector/mapping/version selectors are bounded to their latest/first 100 records; use the paginated APIs for larger inventories. Sync history distinguishes persisted sync decisions from the linked workflow state, including retries and dead letters.
