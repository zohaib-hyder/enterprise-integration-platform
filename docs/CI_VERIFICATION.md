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

From root: run Ruff check/format over backend and Phase 11/12 scripts. From frontend/: `npm ci`, `npm test`, `npm run lint`, `npm run typecheck`, `npm run build`, `npm audit`, `npm audit --omit=dev`, `PLAYWRIGHT_BROWSERS_PATH=.playwright npm run test:e2e`. Private browser setup is documented in BROWSER_E2E.md. Python auditing uses pip-audit against requirements-dev.lock from backend/; its editable project path is cwd-dependent.

Local results are recorded in PHASE_12_REPORT.md and sanitized evidence. GitHub Actions YAML was parsed and checked locally. Remote hosted Actions, Linux runner behavior, cache restore/upload and artifact delivery remain **NOT_VERIFIED** until an authorized Phase 13 push/run. Application container execution adds Linux/ARM64 coverage but does not replace remote CI execution. Third-party action SHA pinning is an additional future supply-chain gate.
