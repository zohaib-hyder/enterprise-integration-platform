# Reproducible portfolio demo — 12 steps

All effects are synthetic and AI is mock-only. No external account, OAuth, Slack/email delivery or calendar invitation is used. Use a fresh isolated release database, never the development database. Existing release users should keep their private configuration and retained data: reruns add a unique demo prefix rather than resetting anything. Commands below start at the repository root unless stated otherwise.

## Preparation

Install host dependencies using [environment setup](ENVIRONMENT_SETUP.md). Prepare and start the isolated release as described in [deployment guide](DEPLOYMENT_GUIDE.md):

```sh
python3.12 scripts/setup_release.py
# Sequential local builds; worker reuses API image.
docker build -t enterprise-integration-phase12-api:local backend
docker build -t enterprise-integration-phase12-frontend:local frontend
```

`setup_release.py` refuses to overwrite its private file. Skip it when that configuration already exists. This rehearsal uses the separate `enterprise-integration-release` project and release volumes. It does not attach the development database volume. The local HTTP/test-mode setup is not production deployment validation. Define a shell helper without expanding any private values into terminal output:

```sh
release() {
  docker compose --env-file .local/release/release.env \
    -p enterprise-integration-release \
    -f compose.release.yaml -f compose.rehearsal.yaml "$@"
}
release up -d --wait --wait-timeout 240 postgres
release run --rm --no-deps api python -m alembic upgrade head
release run --rm --no-deps api python -m app.seed_demo
release up -d --wait --wait-timeout 240
```

For the existing Phase 13A screenshot database, use `.local/phase13a/release.env` in the helper instead; do not regenerate it. It points to a new logical database retained in the separate release volume. The older rehearsal database and original development data remain intact. Private credentials can be read by the local operator from their mode-600 file; do not paste or screenshot it.

## Walkthrough

| Step | Action | Expected real local evidence |
| --- | --- | --- |
| 1 | Open `http://127.0.0.1:3300/login`; sign in with your private synthetic bootstrap account and choose Cedar Ridge Recruiting (Synthetic). | Active tenant context; real API health indicator. Cross-tenant data is not displayed. |
| 2 | Connections → Demo CRM → Test connection; inspect local records. | HEALTHY refers to the PostgreSQL simulator, not a provider account. Five connector types are implemented. |
| 3 | Run the HTTP demo command below. It configures unique CRM/ATS/Slack/email simulators, an immutable mapping and active sync route. | Separate tenant-owned configuration and pinned mapping/workflow versions; no credentials printed. |
| 4 | Demo sends one signed `job.created` webhook; inspect Events. | 202 durable acceptance and signature-verified receipt. Duplicate replay returns 200 and does not create another sync execution. |
| 5 | Inspect Mappings and the generated workflow. | Canonical requisition transformation uses the pinned immutable version and field-authority rules. |
| 6 | Inspect Executions, then Synchronization → Executions/Operations. | Real worker checkpoint history and a SUCCEEDED ATS create with an operation receipt. It is not a real ATS write. |
| 7 | Demo injects one HTTP_503 email failure with Retry-After. Inspect notification attempts. | Email RETRY_SCHEDULED is observed before recovery; ATS success and completed Slack effect persist independently. The demo later finishes, so inspect retained history to see the transient state. |
| 8 | Demo issues an authorized retry of that email delivery only and waits for both channels. | Two SUCCEEDED synthetic deliveries; no replay of the ATS business operation, no reset of retry budget or bypass of due time. |
| 9 | Optionally run the evidence-preparation command below. Open Synchronization → Conflicts and inspect DESTINATION_CHANGED. | Genuine held update after independent ATS/source edits, with before/after evidence. Leave unresolved for review; do not overwrite the recruiting edit. |
| 10 | Operations → Incidents / Timeline; use the correlation ID in screenshot-fixture evidence. Also inspect Calendar. | Correlated persisted incident/workflow/action history; synthetic booked interviews, no invitations. Recovery requires a separate reviewed request and explicit confirmation. |
| 11 | AI Insights → Inspect analysis; inspect authorized evidence/citations. | Clearly labeled deterministic mock response grounded in stored authorized evidence. No paid-model call or accuracy claim. |
| 12 | Human review → inspect ESCALATE_FOR_MANUAL_REVIEW. The HTTP demo accepts one recommendation for preview and asserts recovery-request count unchanged; the optional preparation leaves another pending. | Preview-only result, no autonomous action. Existing operational approval and explicit confirmation gates remain required. |

## Automated HTTP demonstration

This takes bounded waits for actual worker completion and fails if expected states do not occur. From the repository root:

```sh
PYTHONPATH=backend backend/.venv/bin/python scripts/release_demo.py \
  --env-file .local/release/release.env \
  --evidence .local/release/demo-result.json
```

Expected: `PASS: 13 release demonstration checks; no real communication or AI execution.` Evidence includes the unique non-secret prefix and check names. `[3–8, 11–12]` in the table are executed by this script; UI inspection is complementary. Login/logout and real operations dashboard requests are also checked. Each run appends a new unique fixture; it does not clean up prior records.

Optional genuine conflict/incident/pending-review fixtures (same env and demo-result):

```sh
PYTHONPATH=backend backend/.venv/bin/python scripts/prepare_portfolio_evidence.py \
  --env-file .local/release/release.env \
  --demo-evidence .local/release/demo-result.json
```

This creates an independent synthetic ATS edit and a newer signed CRM update through application APIs, refreshes real incidents and requests a mock explanation. It writes only non-secret IDs/provenance to `docs/evidence/phase13a-screenshot-fixtures.json`. It does not resolve the conflict, accept the pending recommendation or execute a recovery. Use it once per demo prefix; rerun the HTTP demo first for a fresh prefix.

## Screenshot reproduction

[Browser setup](BROWSER_E2E.md) explains installing Chromium into the ignored frontend cache. With the same isolated database and optional fixture evidence available:

```sh
cd frontend
PLAYWRIGHT_BROWSERS_PATH=.playwright node scripts/capture-portfolio.mjs ../.local/release/release.env
```

The helper uses Playwright against the real local UI (no route mocking), in-memory session state, a fixed viewport/timezone and lossless PNG compression using Next's locked Sharp dependency. It fails on API 5xx/browser errors and checks visible text against configured private values and personal-path patterns. It captures 13 pages and a provenance manifest. Review every image before publication; automated checks are not a complete visual secrecy guarantee. The checked-in screenshots were captured with the separate Phase 13A env file.

## Finish safely

```sh
release stop
```

Keep the release volume and private env files for reproducibility. Do not run `down -v`, reset schemas or change the original PostgreSQL service. If any step fails, retain its private diagnostic output and investigate the exact failed assertion; do not edit database rows to manufacture successful evidence. [Disclosures](PUBLIC_RELEASE_DISCLOSURES.md) and [release readiness](RELEASE_READINESS.md) remain authoritative limits.
