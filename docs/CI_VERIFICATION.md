# CI readiness and local equivalence

`.github/workflows/verification.yml` has three jobs: backend (isolated PostgreSQL, migration application/drift, all tests, Ruff, 60 mock AI cases and Python audit), frontend (clean npm install, unit/lint/type/build, production and full dependency audits), browser (isolated PostgreSQL, fresh migrated schema/credentials, synthetic retry fixture, full Chromium, real BFF/API and Playwright).

Permissions are contents:read, concurrency cancels superseded runs, explicit job/readiness timeouts bound execution, Python/npm caches use lockfiles and failure traces expire after three days. No production credentials or automatic deployment. CI database trust authentication exists only in ephemeral CI services and is not a release/database recommendation. Browser fixture session state remains private; failure artifacts are synthetic but can contain cookies, so access/retention still matter.

The full development npm audit intentionally remains visible with continue-on-error because the upstream braces chain is unresolved; this is **not** release approval or suppression. The production-only audit blocks its job. RELEASE_READINESS.md retains the development finding as a blocker. Do not turn an audit failure green by deleting Next lint rules, forcing an incompatible downgrade or masking a dependency name.

Local equivalents, from backend/:

```sh
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/alembic heads
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/pytest -q --tb=short
.venv/bin/python -m app.ai.evaluation
```

From root: run Ruff check/format over backend and the Phase 11/12/13 helpers listed in the workflow. From frontend/: `npm ci`, `npm test`, `npm run lint`, `npm run typecheck`, `npm run build`, `npm audit`, `npm audit --omit=dev`, `PLAYWRIGHT_BROWSERS_PATH=.playwright npm run test:e2e`. Private browser setup is documented in BROWSER_E2E.md. Python auditing uses pip-audit against requirements-dev.lock from backend/; its editable project path is cwd-dependent.

Local results are recorded in PHASE_12_REPORT.md and sanitized evidence. GitHub Actions YAML was parsed and checked locally. Phase 13B now records actual hosted Actions runs in the [publication report](PHASE_13B_REPORT.md). Use its exact commit/run/job results; do not treat earlier local counts as hosted execution. Initial browser CI exposed missing FRONTEND_ORIGINS in the production-mode BFF environment. The workflow now supplies exactly `http://127.0.0.1:3000`; fail-closed application defaults and CSRF/Origin checks remain intact. A subsequent hosted backend run exposed two defaults/readiness tests inheriting DATABASE_URL and a seed regression inheriting a private local webhook key. Their fixtures now explicitly isolate settings and generate an ephemeral test key; assertions remain unchanged. A clean source copy reproduced all three failures and then passed all 13 affected diagnostics/seed checks without `.env`. The initial global-setup failure produced no `frontend/test-results` files, so the failure-artifact step ran but uploaded nothing; artifact delivery is not claimed as verified. Cache operations can be inspected in the hosted logs, without a separate cache-correctness guarantee. Application container execution adds Linux/ARM64 coverage but does not replace remote CI execution. Third-party action SHA pinning is an additional future supply-chain gate.

## Verified hosted execution

[Run 38903952434](https://github.com/zohaib-hyder/enterprise-integration-platform/actions/runs/38903952434) succeeded on `d8347254da867f2cde89fc915dd010758884d2e0`: 653 backend tests (709.80 seconds, one warning), 157 frontend tests, 28 browser tests and 60 mock cases. Migration/drift, Ruff, lint/types/build and Python/production dependency audits passed. The five high development advisories remain visible/nonblocking and still block production readiness. See the publication report for failed-run history and fixes. The final report-only documentation commit uses `[skip ci]`; no application/test/workflow source differs from that verified code revision.
