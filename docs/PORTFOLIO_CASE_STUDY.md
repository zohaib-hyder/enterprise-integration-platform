# Case study: reliable staffing operations across disconnected systems

## Customer scenario and requirements

Northstar is a fictional North American staffing company. Sales owns commercial requisitions in a CRM, recruiters work in an ATS, coordinators manage interviews in calendars, and teams depend on Slack/email updates. Manual copying can produce stale jobs, conflicting edits and duplicate notifications; partial failures leave operators unsure which action completed. This project models that problem without using a real client's records or provider accounts.

The engineering requirement was a runnable vertical slice: authenticate an operator, verify tenant context, accept a signed source event, transform it against an immutable mapping, synchronize an owned ATS record, independently deliver notifications and explain failures with inspectable evidence. Recovery must be deliberate and auditable. The portfolio prioritizes failure semantics and honest boundaries over the number of external integrations.

## Implementation

Next.js supplies the operator workspace and a narrow same-origin BFF. FastAPI owns authentication, authorization, typed contracts and application services. SQLAlchemy 2 and Alembic manage PostgreSQL, which stores business records, durable queues, leases, receipts, outbox, incidents and audit history. A single existing Python worker handles workflow steps and companion actions; no Redis or broker is needed for this laptop-scale deployment. See [eight architecture diagrams](ARCHITECTURE_DIAGRAMS.md).

Opaque HttpOnly session tokens are hashed server-side; mutations enforce Origin/CSRF checks through the narrow BFF. Credential and webhook signing material is encrypted at rest with separate configured keys. Structured request logs use correlation IDs and safe error metadata rather than secret-bearing bodies.

HMAC verification and a durable uniqueness key protect the webhook inbox. Dispatch captures a workflow's immutable version. Workers use tenant-first locking, bounded SKIP LOCKED admission, leases and fencing so stale workers cannot checkpoint another claim. Retries preserve due times and budgets rather than hiding transport retries inside a connector. Terminal replay creates a new execution with provenance.

Canonical records, external identities and field-authority rules distinguish commercial source ownership from recruiter-managed destination fields. The synchronization planner pins mapping versions and checks revisions/digests again before applying. Independent ATS edits produce a conflict hold; an operator reviews evidence instead of accepting a blind overwrite.

For the local simulators, ATS effect, identity, synchronization receipt/state and notification intents commit in one database transaction. Slack/email deliveries then proceed independently. A successful Slack delivery is retained when email needs a retry; targeted recovery avoids repeating the completed business operation. Unknown outcomes stay held without proof of a receipt. Calendar coordination uses the same durable action machinery and explicit overlap checks.

The operations console correlates persisted failures into incidents and timelines. Recovery planning rechecks target, permissions, evidence revision and eligibility; high-risk actions require a different approver and separate explicit confirmation. AI assistance reads bounded authorized evidence, validates structured citations and recommendations, and produces a preview after human acceptance. It has no autonomous execution path. The demonstrated provider is deterministic mock only.

## Tradeoffs and observed failure handling

PostgreSQL provides understandable local transaction boundaries and minimizes memory/services on an 8GB M1 laptop. It also couples queue throughput to the primary database; bounded admission, indexes and one execution slot are deliberate constraints, not a scale claim. Shared-database tenant isolation depends on membership checks, scoped queries and composite ownership foreign keys; RLS is not implemented.

Earlier concurrency verification exposed inconsistent lock ordering; hardening standardized tenant-before-queue acquisition. Real synthetic fault injection exercises HTTP 503/Retry-After, retries, dead letters and uncertain receipt handling. Phase 12 rehearsals cover idle-worker SIGKILL, graceful restarts, session persistence and a local database outage/readiness recovery. These do not prove every possible remote after-effect crash boundary.

Optional external transport cannot share a PostgreSQL transaction. Future adapters need provider-specific idempotency and reconciliation, OAuth lifecycle validation, rate/quota handling and tests with real sandbox accounts. Exactly-once remote delivery is not promised.

## Measured evidence

Verification combines typed contract/unit checks, real PostgreSQL migration/constraint/tenant/concurrency tests in isolated schemas, real BFF HTTP flows, Vitest component states, Playwright browser checks and bounded mock grounding evaluations. Failure injection and receipt checks test partial completion rather than asserting only happy-path responses. Original public data is compared to a preserved backup manifest; suites do not reset it.

[Phase 12 verification](evidence/phase12-verification.json) records 653 backend tests (2061.8 seconds), 157 frontend tests, 28 browser tests, 60 mock evaluation cases and successful lint/type/build checks. The local container demo verified 13 HTTP checks; restart/outage rehearsal verified six checks. An isolated backup restore matched counts and fingerprints across 82 public application tables. Phase 13A creates new screenshot/demo evidence separately and does not repeat the full backend suite when application business source is unchanged.

Observed Phase 11 local p50/p95 latency was 20.791/32.861 ms for health (30 samples, concurrency 2), 78.965/126.135 ms for operations overview (20 samples, concurrency 2), and 200.692/244.544 ms for mock analysis (5 samples, concurrency 1). Samples are too small for meaningful p99; worker dispatch observed no pending events and execution completed only one claimed step. The workload and retained SQL evidence are in [performance baselines](PERFORMANCE_BASELINES.md). No customer ROI, reduced incident percentage, production uptime or throughput is claimed. Screenshots show persisted synthetic fixture state, not a live customer's operational metrics. [Screenshot gallery](assets/screenshots/README.md) and [reproducible demo](DEMO_WALKTHROUGH.md) make the evidence inspectable.

## What this demonstrates and what remains

The work demonstrates full-stack delivery, integration contract design, transactional data modeling, concurrency reasoning, observability, recovery governance and constrained AI assistance. Real external providers/OAuth, production TLS, least-privilege deployment roles, operational load, paid-model evaluation and remote CI remain unverified or deferred. Upstream dependency/image findings block production release; see [public disclosures](PUBLIC_RELEASE_DISCLOSURES.md). License and publication are owner decisions; no repository has been published by this preparation phase.
