# Phase 8 implementation report

1. **Features.** Transactional notification intents, immutable Slack/email templates and rule behavior versions, separate durable action delivery, classified bounded retries, unknown receipt review, deduplicated operational alerts and explicit synthetic recruiting calendar coordination. Existing Phase 0–7 business behavior and tenant/security controls are preserved.

2. **Files and migrations.** Added `backend/app/models/notification.py`, `backend/app/schemas/notification.py`, `backend/app/services/notifications.py`, `backend/app/api/notifications.py`, `backend/tests/test_notifications.py`, `frontend/lib/notifications.ts`, `frontend/components/{notifications,calendar}.tsx`, `frontend/app/{notifications,calendar}/page.tsx`, `frontend/tests/{notifications,calendar}.test.tsx`, `scripts/verify_phase8_http.py` and the three Phase 8 domain documents. Updated the existing worker, synchronization/terminal workflow hooks, reliability fault allowlist, alert contracts, main router registration, system metadata, BFF/navigation, security tests, ignore rules and requested docs. Migrations `16f9c565a280` (ten tables plus extended existing alert constraint/immutable evidence triggers) and `cdc089ab8da6` (calendar event composite ownership) are additive after `a1d018ba7387`.

3. **Outbox architecture.** The synthetic ATS effect, sync receipt/outcome and matching notification intent/delivery records share the authoritative step commit. Held sync and terminal failure intents also share their outcome transaction. A committed outcome cannot lose its configured intents. Each delivery's subsequent effect/receipt/completion is a separate transaction; no notification code calls ATS synchronization for recovery.

4. **Templates and rules.** Six strict placeholders, escaping/mention neutralization, external-link/code rejection, sensitive-input redaction and bounded content. Activation and numbered immutable versions pin behavior; rules start disabled and validate tenant-owned destinations. CREATE/UPDATE, real conflict, terminal workflow failure and explicit MANUAL are allowlisted. Outcome keys suppress duplicate/retry notifications even across later rule edits.

5. **Delivery state machine.** PENDING → RUNNING → SUCCEEDED, RETRY_SCHEDULED, FAILED or UNKNOWN; eligible pending/retry/unknown actions can be cancelled. Existing WorkerLease, Phase 5 backoff/classification and Phase 6 reliability are reused by the same runner. Due timestamps, attempts, Retry-After, fencing and expired claims survive restart. Three default attempts; excessive lease loss holds for review. Retry preserves budget/provider deferral; terminal/unknown blind resend is rejected.

6. **Partial failure.** Slack and email are independent intents/actions. Successful ATS and successful other channel remain recorded when one delivery fails. The real SIGKILL demo pauses only its verification subprocess after local provider action and before checkpoint; the transaction rolls back, lease recovery commits one effect. Verified key/hash receipts recover confirmed outcomes; unproven results remain UNKNOWN.

7. **Calendar.** Explicit authenticated create/reschedule/cancel, stable event IDs, aware dates, distinct synthetic participants, optimistic provider revision checks, locked overlap detection and durable request/attempt/receipt evidence. Availability is a bounded current-state snapshot. Participants are request metadata; no actual invites. Calendar request event references enforce tenant **and connection** ownership.

8. **APIs.** Twenty-eight route declarations under `/api/v1/notifications` and `/api/v1/calendar`, with the recovery action declaration allowlisting exactly retry/cancel/review. Templates, versions/preview/activation, rules/versions/optimistic enable/update, manual intents, delivery details/attempts/receipts/filtering, real metrics, calendar requests/events/history/availability/actions. Lists are paginated and bounded. The BFF allows only exact paths/query keys and rejects tenant injection and forced resend/admin paths.

9. **Frontend.** Functional `/notifications` and `/calendar` use the existing Workspace/AuthGuard/BFF design. Actual record counts, template editor/preview/history/activation, pinned disabled-rule creation/edit/activation/history, filtered/paginated delivery timelines, authorized recovery/unknown inspection, explicit manual rule selection and calendar action/history/conflict feedback. Loading/empty/error/forbidden states and permission restrictions are covered. Manual/calendar form keys remain stable across a failed request; no false integration/real-delivery claims.

10. **Live demonstrations.** All ten requested scenarios passed three times. Final retained prefix: `phase8-c583dfc007-`. The final CREATE also verified automatic rendering redaction without losing the ATS/outbox outcome. Actual signed CRM CREATE→ATS→Slack, UPDATE→email, Slack transient retry without ATS repeat, Slack success/email-only retry, duplicate ingress suppression, actual SIGKILL/expired lease recovery, unknown hold/no resend, stable calendar create/reschedule/cancel, overlap conflict and cross-tenant denial. Phase 7's actual signed HTTP regression passed under `phase7-a781e5d5a8-`.

11. **Backend verification.** The full regression passed 455 tests (415 baseline + 40 Phase 8). After final sanitization and additional assertions, all 40 Phase 8 tests passed again (160.32 seconds), with one existing deprecation warning; detailed results are recorded in [VERIFICATION.md](VERIFICATION.md). The new file contains 40 tests, including real PostgreSQL concurrency, uniqueness, atomic rollback, role/Origin/CSRF/tenant ownership, version/receipt immutability, classification/Retry-After, partial failure, exhausted attempts, revoked principal, unknown/provider receipt digest evidence, terminal workflow rules, calendar revision and interval conflicts. The full Phase 0–7 suite is included in the regression run. Ruff check/format pass.

12. **Frontend verification.** 122 tests across 11 files pass: 97 baseline plus 15 notifications, eight calendar and two BFF security additions. ESLint, TypeScript and production webpack build pass. Existing single Vitest worker/two Next build CPUs remain for the 8GB machine. Initial new proxy fixture errors (missing JSON content type and reuse of a consumed Response) were corrected; product protections remained intact.

13. **Database.** Head `cdc089ab8da6`; Alembic reports no new upgrade operations. Original tenant UUID/timezone/index/uniqueness rollback verification passes. Original `postgres:17-alpine` container is healthy, original `enterprise-integration-platform_postgres_data` volume retained on 127.0.0.1:5432. Only fixture-created test schemas are torn down; application business data and demo records are retained. No new dependencies or services were added. Verification API/frontend/workers were stopped; PostgreSQL remains running.

14. **Security.** Active tenant predicates plus composite FK constraints, admin/engineer management, operations recovery without configuration writes, revalidated worker principal/connection, exact BFF route/query allowlisting, Origin/CSRF and bounded schemas. No raw webhook template interpolation or credentials/provider response bodies in delivery receipts. Audit contains safe IDs/actions rather than free-text reasons. Existing encryption/session/HMAC security regressions remain included. No real communication, OAuth, external provider/paid API, commit, push, publish or deployment.

15. **Limits/unexecuted checks.** Local transactional atomicity is not a remote exactly-once guarantee. Unknown actions cannot be forcibly marked delivered or blindly resent; terminal repair requires investigation/new explicit business authorization. Calendar attendees are synthetic metadata, not transport invitations; only interviews/recruiting calendars, simple intervals and no recurrence/room/person availability model. UI inventories/history inspectors are bounded at 100; action tables paginate 25. Coarse tenant locking favors correctness over throughput. Durable delivery history has no new automatic retention job. Browser visual/E2E, independent audit/load/throughput and real-provider interoperability were not executed. The existing Starlette TestClient/httpx deprecation warning remains. System Python is 3.9; use backend `.venv` Python 3.12.

16. **Recommended Phase 9.** Add advanced triage and operational reconciliation: evidence-based unknown outcome resolution, repair eligibility/replay policy, operator case histories and cross-domain outcome reconciliation. Preserve per-action receipt fencing; never repeat successful ATS work to repair notifications. AI enrichment remains Phase 10; real providers and deployment need separate scope.

## Exact local commands

From the repository root, in separate terminals:

```sh
cd backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

```sh
cd frontend
npm run dev
```

```sh
cd backend
.venv/bin/python -m app.workers.runner --poll-seconds 0.5
```

For the controlled demo use `npm run build` then `npm run start`, stop independent workers and run:

```sh
cd backend
PYTHONPATH=. .venv/bin/python ../scripts/verify_phase8_http.py
```

Checks:

```sh
cd backend
.venv/bin/pytest -q
.venv/bin/ruff check app tests alembic ../scripts/verify_phase8_http.py
.venv/bin/ruff format --check app tests alembic ../scripts/verify_phase8_http.py
.venv/bin/alembic check
```

```sh
cd frontend
npm test
npm run lint
npm run typecheck
npm run build
```
