# Decisions

- **Local processes + PostgreSQL-only Compose:** keeps an 8GB M1 machine usable and debugging straightforward. Database container is capped at 256MB and one CPU; tune only after measurement.
- **Synchronous psycopg + SQLAlchemy 2:** simple small connection pool. Readiness offloads blocking I/O to the thread pool; no database connection is made during import.
- **Environment configuration:** Pydantic Settings reads backend `.env` relative to backend working directory. Startup validates PostgreSQL dialect and explicit CORS origins. No credential defaults. Missing database is permitted for local API development but makes readiness fail; production rejects missing database configuration.
- **Versioned business API:** `/api/v1/system/info`; unversioned `/health` and `/ready` remain infrastructure endpoints.
- **Server-side frontend health probe:** same-origin browser request, bounded fetch timeout and server-only backend address. Never exposes connection secrets. Backend response must explicitly report healthy.
- **Explicit tenant indexes:** UUID primary key, unique slug, created-at index. No tenant CRUD or security claims yet. ORM-maintained update timestamps are documented; no trigger is introduced unnecessarily.
- **Honest UI:** disabled coming-soon navigation and empty operational state; no invented success rates, event counts or connected systems.
- **Request correlation:** accepts canonicalizable UUIDs only, otherwise generates an ID. Safe fixed error contracts; no raw exceptions or supplied query strings in logs. Future diagnostics can add safe classified error events.
- **Tooling:** isolated Python environment, npm lockfile, Python dependency snapshot, pytest, Ruff, ESLint, TypeScript and Vitest. No automated GitHub workflows until Phase 12.

Implementation references: [Next.js installation](https://nextjs.org/docs/app/getting-started/installation), [FastAPI settings](https://fastapi.tiangolo.com/advanced/settings/), [SQLAlchemy psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#psycopg).

## Phase 1 decisions

- **Opaque PostgreSQL sessions instead of JWT:** immediate revocation and explicit tenant selection without token claims drifting from memberships. Random tokens are hash-indexed and never serialized. Fixed eight-hour expiry, no localStorage.
- **Argon2id:** 64MiB / three iterations / one lane balance an 8GB development machine and password resistance. Unknown identities verify against a dummy hash. [Argon2-cffi API](https://argon2-cffi.readthedocs.io/en/stable/api.html) supports verification and rehashing.
- **HMAC-signed session-bound CSRF + exact Origin:** covers login, logout, acceptance and all protected mutations. Only the CSRF verification value is readable; it has no authentication authority. Production requires stable signing configuration, HTTPS origins and Secure host-prefixed cookies. Browser origin is forwarded unchanged by the BFF.
- **Database limiter rather than Redis/process memory:** PostgreSQL counters work across workers; account and IP windows count every credential attempt. BFF browsers share an IP cap until a trusted proxy/IP design is deliberately introduced. Public exposure needs edge abuse controls and tuned limits.
- **Tenant parent locking:** serializes membership/invitation mutations and enforces last-admin invariants across concurrent requests. This trades peak throughput for a straightforward correct foundation. Authentication uses a session row lock; external provider calls must never run under these locks.
- **Commit before response:** function-scoped database dependencies prevent a successful auth cookie or acceptance response escaping a failed transaction. Failure-path counters and audit are explicitly committed without committing business changes.
- **Private local invitation delivery:** avoids an email dependency and prevents raw invitation tokens entering API responses. Requires trusted operator delivery to the synthetic email owner. Local files are not a scalable or atomic delivery system; a later outbox adapter is required before real usage.
- **Application isolation with real PostgreSQL tests:** no SQLite substitute and no claim of RLS. Tests use a generated schema and actual migrations, exercise constraints, cross-tenant IDs and races, then remove only test-owned objects.
- **Operator CLI bootstrap:** credentials come from hidden interactive input or environment; no public signup/admin endpoint. Existing passwords and membership privilege are preserved. HTTP verification creates random synthetic accounts and keeps credentials private inside the repository.
- **UI guards are presentation controls:** private data remains protected by backend dependencies and service checks. Pages offer explicit loading, denial, empty and retry states. Fixed-path navigation avoids open redirects.

Remaining security scope: no MFA, password recovery/change, browser CSP policy, distributed edge throttling, trusted forwarded-IP topology, idle session timeout, database RLS, automated retention, real email delivery, independent security audit or public deployment. These limits do not weaken the implemented CSRF/cookie/tenant checks.

## Phase 2 decisions

- **Explicit registry and strict version-1 contracts:** fixed connector types and operation models prevent runtime imports, arbitrary operation dispatch and unvalidated payloads. Pydantic forbids unknown configuration fields and validates results for HTTP and direct trusted callers.
- **Durable local adapters:** PostgreSQL stores synthetic CRM/ATS records, messages and meetings. Real providers, OAuth and transport are absent; message status is `SIMULATED`, never delivered.
- **Two-level ownership constraints:** every synthetic row has a tenant FK plus a composite connection/tenant FK. Application queries additionally scope tenant, connection and record kind. Namespace uniqueness never implies synchronization between systems.
- **Connection row lock for local serialization:** concurrent upserts, deliveries and booking checks serialize per connection. This is a correctness-first local design and trades throughput for simple race protection. Future external transport must not run under that lock.
- **Payload-bound idempotency:** SHA-256 of canonical validated input binds keys to meaning. Identical message retries reuse ID; changed payloads conflict. Calendar retries reuse ID and return current event state. No at-least-once provider delivery or exactly-once distributed claim is made.
- **Generic REST disabled:** implementing a general HTTPS client without DNS pinning and rebinding protection would create an unsafe proxy. Phase 2 ships no transport, rejects destination/credential config and forbids enabling REST in application and database. Tests invoke malicious destinations directly and verify zero DNS/HTTP calls.
- **Conservative RBAC:** all active members see safe connection metadata/capabilities; only admins/engineers inspect business payloads or configure, health-check or execute. Existing CSRF, exact Origin, BFF allowlist and backend authorization remain mandatory.
- **Repeatable operator seed:** hidden or environment-supplied credentials use existing bootstrap verification. Stable namespace IDs and idempotency keys avoid duplicate fixture rows; existing users/tenants are retained. Repeated seeding upserts managed business fixtures but does not silently re-enable an edited connection.
- **No new dependencies/infrastructure:** reuse current SQLAlchemy/Pydantic/test tooling and PostgreSQL-only Compose. No Redis, provider SDK, scheduler, webhook endpoint or LLM is added.

## Phase 3 decisions

- **Separate HMAC ingress from browser management:** public signed raw-body delivery has no session/CSRF requirement; every browser mutation retains exact Origin, session-bound CSRF and backend tenant/RBAC enforcement. Ingress derives ownership from opaque stored endpoint identity.
- **AES-256-GCM using a dedicated environment key:** a maintained cryptography primitive provides confidentiality/authenticity, fresh nonces and context binding. No handwritten encryption, connection-embedded tokens or external secret-manager service. Missing/wrong keys fail webhook operations closed without breaking earlier health/identity features.
- **Controlled one-time disclosure:** creation/rotation returns the generated secret only after commit; reads never return it. Frontend keeps it in a masked transient disclosure ref/DOM, clears it on close/unmount and never persists it in normal application state or browser storage. Lost disclosures require rotation.
- **Endpoint lock plus unique inbox identity:** serializes rotation, acceptance and concurrent duplicate classification; the database enforces `(tenant, endpoint, source, provider event ID)`. Canonical validated payload digests permit reserialization, while HMAC always covers exact raw bytes. A conflict never overwrites accepted data.
- **Receipt outcomes separate from event state:** accepted business events remain pending. Duplicate/conflicting deliveries and authenticated schema failures get bounded receipt rows; unverified attempts get safe log categories only, avoiding untrusted tenant attribution and attacker payload persistence.
- **No transport under database locks:** UI simulation calls the verifier in process after an explicit authorized action. The real CLI sends only to a fixed localhost allowlist, pins localhost to numeric loopback, ignores proxies and refuses redirects. No automatic simulator emission or provider transport is introduced.
- **Modest local scope:** four tables, one cryptography dependency, no additional frontend dependencies, Redis, workers or paid services. Master-key re-encryption, central metrics/retention and edge abuse controls remain future work. The roadmap now reflects the explicitly requested Phase 3 webhook scope.

## Phase 4 decisions

- **Six contracts, four source templates:** explicit entity validation/versioning supports future expansion without pretending unsupported provider adapters exist. CRM/ATS jobs separate commercial and recruiting status.
- **Declarative bounded operations:** rename/extraction, trim, email, enum, aware datetime, finite numeric conversion, string/default/null operations only. No eval, expressions, dynamic imports or user code; malformed values produce safe field/category errors.
- **Database-enforced immutable versions:** version creation is append-only, activation is explicit and constrained; a trigger blocks raw SQL updates/deletes. Every attempt retains its exact definition version and proposal snapshot.
- **Namespaced identity:** tenant/connection/source-kind/external ID uniqueness and composite FKs prevent cross-tenant links. UUIDv5 creates stable IDs; explicit additional links do not imply automatic business merges.
- **Conservative authority/time policy:** changed fields need an authorized source and provable ordering. ORIGIN ownership is per connection, REVIEW is unresolved, equal conflicting timestamps and missing ordering evidence cannot overwrite. Newer confirmations advance high-water marks without fake revisions.
- **Local transaction serialization:** tenant parent plus canonical row locking keeps races simple on the existing lightweight PostgreSQL setup. Canonical state, identity and attempt/evidence commit together; no downstream/provider network I/O exists.
- **Idempotent attempts and controlled quarantine:** fingerprints bind exact version/input/time/event. Reprocessing uses a corrected version and retained safe source identity; old evidence remains immutable. Unsafe unknown payload fields are not retained for later display.
- **No extra infrastructure/dependencies:** existing Pydantic, SQLAlchemy, pytest and frontend tools suffice. Explicit seed/normalization actions only; no worker, sync, workflow engine, OAuth or paid service.


## Phase 5 decisions

- PostgreSQL is the durable work store; no Redis, paid queue or unnecessary service on the M1/8 GB environment.
- Sequential JSON v1 with code-owned executors/output contracts; no user scripts, expressions, arbitrary URLs or dynamic imports. Fixed limits cover steps, payloads, SQL timeouts, lease duration and retries.
- Database immutability for plans/triggers plus composite tenant ownership constraints; execution pins a version and creator principal. Every effect revalidates current authorization.
- Separate dispatch receipts preserve Phase 3 ingestion semantics. NO_MATCH is explicit; manual rescan is restricted to previously unmatched events, preventing version activation from silently rerunning processed events.
- Single-slot workers use SKIP LOCKED, UUID lease fencing and durable due times. Claim and effect transactions are separate; polling and waiting do not hold transactions. No external I/O exists in worker adapters.
- Local mock effects/receipts/checkpoints commit together. Root-stable keys and receipt validation avoid duplicate business actions across checkpoints/replay. Future external uncertainty must be reconciled; exactly-once provider delivery is not promised.
- Positive capped exponential jitter and bounded attempts; explicit sanitized categories, dead letters and history instead of retrying every exception. Unexpected code errors fail closed.
- Operations users receive executions:recover only for cancel/confirmed eligible replay. Successful/running/cancelled replay and unknown/missing completed receipts are denied. Replay is a new traceable execution, never a reset.
- Seed plans are repeatable; demonstration executions are produced by signed ingestion and actual engine transactions. Read-only CRM observation demonstrates the end-to-end chain while full ATS synchronization stays in Phase 7.
- Future Phase 6 focuses on operational/observability hardening; the user's Phase 5 scope supersedes the older roadmap allocation of engine/retry work.


## Phase 6 decisions

- Shared execution performs one attempt; retry/wait ownership stays in Phase 5. One-off calls never silently repeat non-idempotent operations.
- Atomically coordinated fixed-window budgets use PostgreSQL clock/tenant locks for tenant, tenant-connector-type and connection scopes. No cross-company global bucket or process-local limiter. Boundary bursts/coarse locking are documented limitations.
- Persistent CLOSED/OPEN/HALF_OPEN state, cooldown and controlled probes survive restart; actual sanitized transitions are evidence, not fabricated history. Business validation errors do not trip transport health.
- Local effect savepoints preserve rollback while the outer transaction commits failed-attempt controls, observations, alerts and durable retry. Existing local effect/receipt/checkpoint atomicity remains authoritative; external network side effects are deferred.
- Dedicated context-bound AES-GCM credential generations accept synthetic values only; submission is input-only, metadata is fully masked. A local key reference/ring enables retained ciphertext reads, with missing/wrong encryption failing closed. No real OAuth or external secret manager.
- Deterministic local refresh rotates both tokens under connection ownership. A second concurrent refresh is NOT_NEEDED, not an invented extra refresh metric. Permanent authentication failures do not retry indefinitely.
- Fault scenarios require explicit authorized enabling, TTL and count; no arbitrary bodies, code, URLs or implicit provider outage claims.
- Observation metrics explain their committed/retained basis and no-sample latency; lazy retention never deletes old phase data. Alerts use allowlisted context, resource/source deduplication and audited lifecycle. Operations users acknowledge only; resolution remains admin/engineer.
- Real worker/BFF/HMAC demonstrations retain uniquely named synthetic evidence rather than resetting data or synthesizing completed executions. Remaining browser visual/load/security and real-provider checks are reported honestly.


## Phase 7 decisions

- Reuse the Phase 4 registry and provenance; external identity namespaces include tenant and connection. One config owns a CRM/ATS route, with immutable version activation and revision checks.
- Restrict destination mappings to the existing ATS requisition contract. CRM title/department/location may synchronize when governed; ATS status is preserved. Do not invent unsupported commercial/account/calendar provider fields.
- Detect independent destination changes using successful-operation baselines and reject races using current record digests under existing tenant/connection locks. Manual accept is a reviewed re-plan, never an authority override.
- Keep exactly one worker/retry architecture. Local ATS effects, links, receipts and checkpoints are atomic in PostgreSQL. Review-generated writes are queued as checkpoint-seeded Phase 5 workflows, retaining cancellation/fencing/retry behavior.
- Store previews separately from executable business identities. Completion of a planning workflow with sync HELD is not a successful synchronization.
- Exact pre-existing matching is explicit and scoped; ambiguous candidates and stale/duplicate identity associations hold. Unknown receipts are held rather than blindly retried.
- Reconciliation is explicit, bounded and read-only. Finding counts describe scanned pages. Optional finding plans do not perform repairs.
- Operations roles can inspect and keep/skip reviewed changes. Only admin/engineer roles can configure, reconcile, link or queue writes. Reasons remain in authorized resolution history; audit/log metadata excludes payloads and credentials.

## Phase 8

- Use a PostgreSQL transactional outbox inside the existing synthetic sync/terminal-failure transactions; no reconciliation gap is claimed for these local outcomes.
- Extend the existing foreground worker with a companion action claim per iteration. Reuse WorkerLease, Phase 5 bounded backoff/classification and Phase 6 reliability rather than add infrastructure or another scheduler.
- Pin immutable template and rule versions; deduplicate logical outcomes by tenant/rule/source key independently of later version changes. Rules start disabled, and destination/configuration ownership is backend validated.
- Freeze validated render inputs and escaped bounded provider payloads. No raw webhook rendering, external URLs, executable templates, real recipient domains or Slack mentions.
- Treat unconfirmed TIMEOUT as UNKNOWN. Verify provider key and request hash; never infer a calendar update/cancel receipt solely from current event fields. No blind replay of failed/unknown notifications or ATS writes.
- Keep calendar coordination explicit, optimistic and tenant/connection locked. Participants are synthetic request metadata; the existing calendar adapter sends no invitations.
- Reuse operational alerts and existing recovery/acknowledge/resolve RBAC. Free-text recovery reasons are validated but not copied into logs/audit metadata.
- Preserve the original volume and business records. Two additive migrations introduce ten domain tables and strengthen calendar ownership references; no dependency/service additions.

## Phase 9

- Add only seven cross-domain metadata tables; reuse alert emission, domain ownership, existing worker/retry/service contracts and receipts.
- Use explicit tenant-owned target/problem-family IDs and source occurrence uniqueness for incident correlation. Calendar/terminal/unknown delivery evidence refines one delivery-specific case rather than duplicate cases.
- Derive timelines from stored IDs/FKs, never timestamp proximity. Bounded projections expose provenance and unavailable references. Count only tenant-associated worker leases; idle infrastructure health remains unknown.
- Separate window counts from current backlog/health snapshots. Failure trends use actual recorded failed steps/attempts; do not erase historical failures after recovery.
- Keep preview read-only; materialize/audit an explicit stable-UUID request. Pin evidence, lock/revalidate the source and delegate confirmation to existing services. Preserve failed/stale/expired outcomes.
- Require independent expiring review for command-center replay/refresh. Approval cannot override missing/unknown receipt evidence, field authority, tenant ownership or existing service rejection. Existing low-level domain contracts remain intact.
- Store immutable evidence/activity/findings and safe actor/reason/outcome audit. Record assignment and severity/status changes explicitly; avoid copying sensitive source payloads into operations.
- Operational reconciliation is bounded metadata only. Skip historically indeterminate notification rules instead of guessing activation history; never repair or dispatch during a scan.
- Keep ten static, source-controlled runbooks; no AI diagnosis or runtime executable instructions. AI assistance remains Phase 10.

## Phase 10: extractive AI, explicit review and unchanged recovery authority

- Default to a deterministic credential-free mock. Optional OpenAI requires explicit server
  flag/key/configured model plus tenant manager selection; no paid call occurs by default.
- Prefer fixed authorized projections over sending payloads/free-form notes. Require exact
  quote grounding, real citation IDs and planner-derived action candidates. This deliberately
  limits model prose instead of claiming arbitrary semantic entailment is verified.
- Use eight tenant-scoped metadata tables and existing PostgreSQL locks/audit. Snapshot and
  decision updates are DB-prohibited; AI-only retention is bounded and lazy. No business data
  or existing recovery/audit history is pruned by AI.
- Human review is advisory and independent of recovery approval. Accepting creates no recovery
  request and executes no action. A separate operator confirmation and all existing approval,
  receipt, principal and field-authority checks remain mandatory.
- Share one pure eligibility policy with offline evaluations. Mock contract pass rates are
  never reported as real-model accuracy. Real-model interoperability is unexecuted.
- Use async timeout/cancellation within a bounded process gate and synchronous authenticated
  DB transaction for lightweight local correctness. HTTP disconnect cancellation and
  multi-process global concurrency are not guaranteed. Phase 11 remains a separate audit.


## Phase 11 hardening

Phase 11 adds direct API body and Host restrictions, bounded local webhook ingress protection, security response headers, Tenant-before-queue lock ordering, targeted dependency remediation, browser/axe verification and bounded performance/fault harnesses. Existing business services, approvals, idempotency and encrypted credentials are retained. No schema migration is added; head remains `e8e0abf289b1`.

See [Phase 11 report](PHASE_11_REPORT.md), [security findings](SECURITY_AUDIT.md), [browser setup](BROWSER_E2E.md), [performance evidence](PERFORMANCE_BASELINES.md) and [production release gates](PRODUCTION_SECURITY_CHECKLIST.md). Verification uses retained isolated synthetic schemas; no public data reset or deployment. One high development-tooling advisory remains open; production readiness is conditional on the documented gates.

### Phase 11 engineering decisions

Use stable Tenant-before-queue locking rather than adding a second scheduler or weakening fencing. Bound admission to 32 tenants and skip busy locks. Preserve the webhook decoder's typed rejection receipts and streaming limit; the direct JSON body middleware does not pre-buffer webhook bodies. Use per-process hashed-peer ingress limits only as a local defense; edge rate limiting is a production gate. Upgrade Vitest and cryptography selectively; retain compatible Next ESLint tooling with a disclosed advisory instead of forcing a framework downgrade. CI uses disposable PostgreSQL trust authentication only inside ephemeral isolated jobs; local Compose credentials and persistent volume are unchanged.


### Phase 12 release decisions

- Require independent encryption keys, persistent CSRF secret, explicit production hosts, HTTPS origins and disabled debug at startup. Use dedicated APP_DEBUG rather than a generic host DEBUG variable. Opaque database-backed sessions retain their existing contract.
- Align Next and its ESLint package at supported patch 16.4.1. Investigate but do not force an unstable/incompatible glob replacement or hide the unresolved braces chain.
- Use digest-pinned supported slim bases, multi-stage dependency installation, non-root/read-only runtime, and omit runtime package managers. Newer supported distro/runtime images remediate scanner findings; residual findings remain release blockers, not an audit exception.
- Use standalone output only for container builds; preserve normal npm start for host builds. Use an edge network because internal-only published ports were not reachable in the actual Docker Desktop rehearsal. Keep PostgreSQL unpublished and the original Compose/volume untouched.
- Back up the complete public application schema and explicitly exclude retained test schemas. Restore with guarded generated database names only; no caller-selected overwrite destination. Preserve failed rehearsal databases and volumes.
- Keep worker health tied to its own lease. A 20-second probe budget accommodates cold imports under 0.5-CPU limits; this does not change heartbeat/lease/fencing semantics. No engine rewrite or unmeasured performance optimization.
- Remote Actions, publication and deployment remain separate authorized work. Image pins, local CI equivalence and successful recovery are preparation evidence, not production certification.
