# Browser end-to-end verification

`frontend/e2e/platform.spec.ts` implements the 25 requested operational scenarios plus desktop (1440×1000), tablet (768×1024) and mobile (390×844) checks. Tests use real Next BFF requests, FastAPI, migrated PostgreSQL and existing synthetic connectors. Calendar processing starts and stops the existing worker, rather than manufacturing successful UI state. Auth/session tests use independent empty cookie contexts.

## Local setup

Run from repository root with the existing PostgreSQL service and backend virtual environment available:

```sh
cd backend
PYTHONPATH=.:tests .venv/bin/python ../scripts/phase11_fixtures.py
PYTHONPATH=.:tests .venv/bin/python ../scripts/phase11_retry_fixture.py
cd ..
backend/.venv/bin/python scripts/phase11_server.py api
# another terminal
cd frontend
NPM_CONFIG_CACHE=.npm-cache npm ci
PLAYWRIGHT_BROWSERS_PATH=.playwright npx playwright install chromium
npm run build
npm start
# another terminal, frontend/
PLAYWRIGHT_BROWSERS_PATH=.playwright npm run test:e2e
```

The fixture script creates and retains a unique `phase11_*` schema. Never reset the shared public schema or volumes. Private generated credentials and database URL live in ignored mode-600 `backend/.local/phase11-fixtures.json`; session state is ignored mode-600 `frontend/.local/phase11-state.json`. One failed workflow state is explicitly seeded for approval UI coverage; other business outcomes use existing fixture services and synthetic faults. A fresh schema is required for a fully independent rerun because conflict/recovery/calendar tests intentionally mutate their isolated records. `phase11_browser_accounts.py` appends fresh principals for iterative local testing without clearing login limits; `phase11_retry_fixture.py` creates a new actual HTTP-503 delivery rather than resetting an old state.

Use the full Chromium channel configured in `playwright.config.ts`: the downloaded headless shell failed locally with an ICU resource error. The full browser works. One browser worker and bounded polls keep memory modest; no arbitrary sleep synchronization. Do not run performance/reliability workers simultaneously with browser fixtures: they may correctly advance the delivery selected for targeted retry.

JSON and HTML reports are under `frontend/.local`; failures retain `frontend/test-results` screenshots/traces. These may contain synthetic sessions and must stay private. CI failure artifacts expire after three days. Axe checks the initial overview at all three sizes; it is not a complete WCAG audit of every route. Browser scenario results and final counts are recorded in PHASE_11_REPORT.md.

## Recorded result

Final complete local run: **28 passed (1.1 minutes)**, no skipped or flaky cases. All three overview axe reports contained zero violations. Earlier failed attempts exposed test fixture uniqueness, selector and stale-view synchronization issues; fixes retained all backend constraints and revision checks. The final calendar case waits for refreshed persisted evidence and asserts cancellation was queued before polling its terminal state.
