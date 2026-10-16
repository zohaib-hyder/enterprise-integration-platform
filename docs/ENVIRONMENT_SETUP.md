# Reproducible environment

Use Python 3.12 (local pin `.python-version`: 3.12.14; release image uses supported 3.12.15), Node 24.19.0 (`.nvmrc`; release image uses a separately digest-pinned supported Node 24 security update), npm 11, Docker Engine 24+ and Compose v2+ supporting health dependencies, `--wait`, resource limits and init. Locally tested: Docker 29.7.2 / Compose 5.5.0, macOS ARM64 M1 / 8GB. PostgreSQL is 17.11; release configuration pins the reviewed image digest. Python 3.9 supplied by macOS is insufficient. Runtime versions and image digests require deliberate periodic updates; pins do not mean CVE-free.

## Fresh checkout

Do not copy anyone's ignored environment files. From the repository root:

```sh
python3.12 scripts/setup_local.py
python3.12 -m venv backend/.venv
cd backend
.venv/bin/python -m pip install -r requirements-dev.lock
cd ../frontend
npm ci
cd ..
docker compose up -d --wait
cd backend
.venv/bin/alembic upgrade head
.venv/bin/python -m app.bootstrap
.venv/bin/python -m app.seed_demo
```

The setup script exclusively creates mode-600 `.env`, `backend/.env`, `frontend/.env.local`, with independent database password, webhook key, credential key and persistent CSRF secret; it refuses to overwrite configuration. Bootstrap and seed prompt privately or accept operator-provided BOOTSTRAP_* environment values; never put passwords into shell history. Seed adds synthetic demo organizations and records without replacing operator data. Existing installations keep their files and volumes. If keys are missing, follow the existing key setup scripts; do not silently replace keys protecting ciphertext.

Three terminals, from repository root:

```sh
cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
# terminal two
cd backend
.venv/bin/python -m app.workers.runner --poll-seconds 0.5
# terminal three
cd frontend
npm run dev
```

Open `http://127.0.0.1:3000/login`. `/health` is process liveness; `/ready` verifies database connectivity and returns 503 if unavailable. It is not a migration-head check: migrations are an explicit prerequisite. Use `alembic current` / `alembic check` separately. Stop processes with Ctrl-C; do not remove volumes.

## Production validation

`APP_DEBUG` defaults false and is prohibited in production. Production requires DATABASE_URL, a persistent CSRF_SECRET (minimum 32 characters), both canonical 32-byte independent encryption keys, explicit trusted ALLOWED_HOSTS without testserver/wildcards, HTTPS AUTH_ORIGINS and HTTPS CORS origins if configured. Mock AI remains default; optional paid transport stays disabled. Authentication sessions are opaque random tokens stored as hashes in PostgreSQL, not signed JWTs needing another invented session secret. The persistent CSRF key preserves CSRF token validity through process restarts; production cookies are Secure/HttpOnly with existing SameSite policy.

`release.env.example` documents exact required values. Never print a resolved Compose configuration containing secrets; use `docker compose ... config --quiet`. Generate keys with Python `secrets.token_bytes(32)` and `base64.urlsafe_b64encode`, deliver them through private files or a secret manager and retain required previous key references during managed rotation.

## Verification

Backend: `pytest -q`, Ruff, `alembic heads/current/check`, `python -m app.ai.evaluation`, pip-audit against the actual lock. Frontend: `npm ci`, `npm test`, `npm run lint`, `npm run typecheck`, `npm run build`, full `npm audit`. Browser: BROWSER_E2E.md (fresh isolated schema, never original data). A new backend virtual environment was installed from the lock and passed dependency compatibility/import checks; fresh configuration generation was exercised without existing ignored files. This is evidence for installation and configuration, not a claim that a fresh physical Mac was provisioned.
