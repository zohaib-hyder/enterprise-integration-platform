# Release configuration guide — no deployment performed

`compose.release.yaml` is a separate production-style four-service topology: API, existing worker, standalone Next frontend, PostgreSQL. API and worker share the same image and existing services. The original `compose.yaml` remains unchanged. Release volumes are project-scoped `release_database` / `release_private`; never attach the original `enterprise-integration-platform_postgres_data` volume. Application and database networks are internal. A separate edge network allows the frontend localhost binding; the rehearsal override attaches API ingress to that edge as well. The rehearsal override additionally binds API ingress to localhost:18000. No database port is published.

Images use pinned runtime/base digests, exact dependency locks, multi-stage builds, non-root users, no dev dependencies in runtime, exec commands, init, read-only application roots, dropped capabilities, no-new-privileges, bounded tmpfs, health checks and resource limits. Build contexts use allowlists excluding env files, tests, backups, browser state and caches. Python's runtime has no pip; the worker command reuses the API image. Next uses standalone output only for CONTAINER_BUILD=true; ordinary host builds retain compatible npm start behavior. Containers do not embed secret values.

## Isolated local preparation

```sh
python3.12 scripts/setup_release.py
# Short alias shown here only for readability; every call has the separate project/file.
docker build -t enterprise-integration-phase12-api:local backend
docker build -t enterprise-integration-phase12-frontend:local frontend
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml up -d --wait --wait-timeout 240 postgres
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml run --rm --no-deps api python -m alembic upgrade head
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml run --rm --no-deps api python -m app.bootstrap
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml run --rm --no-deps api python -m app.seed_demo
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml -f compose.rehearsal.yaml up -d --wait --wait-timeout 240
```

Build the two images sequentially on an 8GB host; do not overlap builds/scans with the full suites when avoidable. The worker reuses the API image.

`setup_release.py` creates private, independent secrets and synthetic bootstrap credentials; it refuses overwrite. Its explicit APP_ENV=test is for local HTTP rehearsal only. That verifies container wiring and functionality, **not** production TLS/Secure-cookie behavior. For actual future deployment, prepare a separate private file from `release.env.example`, set APP_ENV=production and configure external HTTPS origins/hosts. Do not keep bootstrap credentials permanently in production service environments. Do not deploy while RELEASE_READINESS.md has blocking security findings.

## Future production prerequisites

An independently managed TLS reverse proxy must terminate HTTPS, forward only intended paths, enforce body/read timeouts and distributed rate limits, strip untrusted forwarding headers and apply exact trusted Host/Origin configuration. API does not blindly trust proxy headers. Secure cookie authentication needs the same external HTTPS origin through the BFF. Protect webhook ingress explicitly; it is not exposed by the base release file. No TLS proxy or cloud infrastructure is provisioned by this phase.

Use a least-privilege application database role and separate migration/backup operator, private key management/rotation, encrypted backups and tested rollback, local log retention and image CVE review. Compose's single rehearsal database role is convenient local wiring, not proof of least-privilege production DB separation. PostgreSQL liveness uses pg_isready; API readiness uses SELECT 1. Explicit migrations precede startup. Health failure alone does not make Compose restart a running process: restart policies act on process exit.

Worker default polling is 0.5s with a single execution slot and 30s leases. Optional WORKER_HEALTH_FILE records its own generated lease UUID; its probe checks that exact worker's recent heartbeat and non-stopped state, so another healthy worker cannot mask it. Grace period 45s permits an in-flight bounded step to finish. SIGKILL skips graceful state updates; leases/fencing handle recovery. Unknown remote write outcomes remain held, not blindly resent. See ROLLBACK_RUNBOOK.md and RUNBOOKS.md.

Do not publish images, run remote CI or deploy without a separate explicit authorization. No paid API/provider credentials are needed.
