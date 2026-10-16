# Implementation phases

Each phase should deliver working code, explicit contracts and verification before expanding scope. Phases 0–10 are implemented. Phase 11 hardening and local verification are complete with results recorded in PHASE_11_REPORT.md; Phase 12 release preparation is implemented with local evidence and explicit unresolved release gates. Phase 13A local publication preparation is documented; actual Phase 13 publication remains separate. The requested Phase 3 webhook scope supersedes the earlier connection-onboarding plan; real provider onboarding remains deferred.

| Phase | Scope | Exit evidence |
| --- | --- | --- |
| 0 | Monorepo, diagnostics, tenant migration, honest dashboard | Local builds, endpoint tests, migration verification |
| 1 | Tenant-aware identity and authorization — complete | PostgreSQL isolation/concurrency tests, role matrix, real BFF login/switch/logout |
| 2 | Enterprise connector contracts, registry, five durable simulators and Connections UI — complete | PostgreSQL contract/isolation tests, idempotent seed, real BFF operations |
| 3 | Secure webhook ingestion, encrypted signing-secret lifecycle and durable event inbox — complete | HMAC/replay/rotation tests, concurrent deduplication, real BFF and signed HTTP |
| 4 | Canonical contracts, deterministic mappings, identity registry and field authority — complete | Immutable versions, PostgreSQL policy/concurrency tests, real BFF and accepted-event normalization |
| 5 | Durable sequential workflows, dispatch, PostgreSQL workers, retries, dead letters and recovery — complete | Fenced leases, immutable versions, race/recovery tests and real signed HTTP/worker demo |
| 6 | API reliability, atomic rate limits, persistent circuits, encrypted credential lifecycle, fault injection, metrics and alerts — complete | PostgreSQL concurrency/crypto tests, real worker retry/restart, token refresh/failure and observable persisted evidence |
| 7 | Ownership-aware CRM-to-ATS synchronization, identity-safe atomic upserts, reviewed recovery and bounded read-only reconciliation — complete | Real PostgreSQL locking/uniqueness tests, signed HTTP/worker CREATE/UPDATE/retry/review/reconciliation evidence |
| 8 | Transactional Slack/email outbox, durable per-action delivery/recovery and explicit synthetic calendar coordination — complete | PostgreSQL idempotency/concurrency/RBAC tests, real HMAC-to-ATS-to-delivery, SIGKILL recovery and calendar conflicts |
| 9 | Enterprise Operations Command Center, correlated incidents, reviewed recovery/approvals, operational consistency scans and runbooks — complete | Real PostgreSQL governance/concurrency tests, signed operation trace, approved replay, targeted recovery, SIGKILL visibility and tenant denial |
| 10 | Evidence-grounded AI assistance, strict decisions, human review, evaluation and AI observability — complete | Mock-only contract evaluations, real persisted citations, no autonomous execution, existing approval preservation |
| 11 | Security hardening, browser E2E, baselines, reliability and CI configuration | Findings register, PostgreSQL regressions, browser/axe evidence and bounded local measurements; see Phase 11 report |
| 12 | Release preparation, dependency/container review, reproducible environments, backup/restore and local CI/rehearsal | Actual local verification and release-readiness register; unresolved upstream findings remain explicit |
| 13 | Portfolio case study and local publication preparation (13A); actual publication pending | README, diagrams, real screenshots and review checklist; separate approval required for Git/remote/publication |

Real provider integration and OAuth are optional future extensions, requiring separate authorization and credentials. Paid APIs are not required. Deployment remains a manual future decision.

The explicitly requested Phase 5 engine scope supersedes the earlier allocation of workflow definitions/execution/retries to Phases 6/7/9. Those capabilities and Phase 7 synchronization are now implemented locally.

The requested Phase 6 reliability scope supersedes the earlier general hardening label. Phase 7 implements ownership-aware CRM-to-ATS synchronization and explicit conflict reconciliation, built on the worker/reliability contracts. Phase 8 reuses that worker for separate durable notification/calendar actions. Real providers/OAuth still require separate future authorization and transport design.

Phase 10 follows the explicitly requested AI scope. Phase 11 now implements security audits/full browser E2E and CI configuration; unresolved production gates remain explicit. Optional AI transport remains disabled unless server and tenant configuration explicitly enable it.


## Phase 11 hardening

Phase 11 adds direct API body and Host restrictions, bounded local webhook ingress protection, security response headers, Tenant-before-queue lock ordering, targeted dependency remediation, browser/axe verification and bounded performance/fault harnesses. Existing business services, approvals, idempotency and encrypted credentials are retained. No schema migration is added; head remains `e8e0abf289b1`.

See [Phase 11 report](PHASE_11_REPORT.md), [security findings](SECURITY_AUDIT.md), [browser setup](BROWSER_E2E.md), [performance evidence](PERFORMANCE_BASELINES.md) and [production release gates](PRODUCTION_SECURITY_CHECKLIST.md). Verification uses retained isolated synthetic schemas; no public data reset or deployment. One high development-tooling advisory remains open; production readiness is conditional on the documented gates.
