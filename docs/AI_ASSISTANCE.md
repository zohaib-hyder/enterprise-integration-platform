# Evidence-grounded AI assistance — Phase 10

Northstar's fictional recruiting operations team can inspect a failed workflow, connector,
CRM/ATS conflict, delivery, incident or consistency finding without copying payloads or
credentials into a chatbot. AI supplies an advisory evidence projection. The owning
services remain the source of truth and the sole writers of business state.

## Local use

From the repository root, in separate terminals:

```sh
cd backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

```sh
cd frontend
npm run build
npm run start
```

Sign in, select the tenant, then open `/ai-insights` or `/ai-review`. Enter a persisted
record UUID, choose its target/analysis type and request analysis. The health target is
the active tenant UUID; it summarizes at most eight retained unresolved incidents,
not an exhaustive infrastructure health assessment. An empty scope explicitly abstains.
Inspect citations, recommendation eligibility and missing evidence before review.
Accepting opens a separate `/operations` recovery preview. That flow still requires
an operator-created request, current evidence, confirmation and any independent approval.

```sh
cd backend
.venv/bin/python -m app.ai.evaluation
PYTHONPATH=. .venv/bin/python ../scripts/verify_phase10_http.py
```

The HTTP demonstration requires retained Phase 9 synthetic records and the ignored local
operator fixture `.local/http-verification.json`. It fails if prerequisites are missing;
it never invents a successful source event. It creates a fresh synthetic Slack connection,
template and rule, runs one existing worker claim against a local simulated 503, and
retains the failed attempt. It seeds and audits one malicious-text incident explicitly.
It cancels its unused scheduled synthetic retry using the existing operator API. No
recovery is confirmed and no real messages, calendar invitations or provider calls occur.
Do not run a separate worker concurrently with this bounded fixture verifier.

## Provider architecture

`app/ai/providers.py` defines the typed provider protocol, registry, deterministic mock
and optional OpenAI Responses transport. Mock is the effective default even when server
OpenAI credentials exist: a manager must explicitly select the optional provider in the
tenant's versioned configuration. Default startup does not run analysis at all.

Each analysis has an actor-bound idempotency UUID, correlation UUID, immutable configuration
version number, evidence digest, requested provider/model, effective provider/model,
sanitized errors, measured latency and actual usage where supplied. Provider failures or
invalid outputs can fall back once to the same evidence-constrained mock, clearly labelled
simulated. There are no retries against a paid provider, tools or autonomous agent framework.
A process-wide concurrency gate defaults to two calls (configurable one to four), tenant
request limit defaults to 30/hour, context/output each cap at 24 KiB, response transport at
128 KiB and request timeout at eight seconds (one to ten configurable). Token output defaults
to 1,800 with a hard configured range of 256–4,000. There is one provider call per analysis,
plus an optional local fallback. Cancellation propagates through asynchronous transport and
releases the gate; HTTP disconnection does not guarantee cancellation of FastAPI's synchronous
request thread. Any remaining call is timeout-bounded and can only persist AI metadata.

## Optional OpenAI setup

Set these **private backend environment variables only**:

```dotenv
AI_OPENAI_ENABLED=true
AI_OPENAI_MODEL=YOUR_STRUCTURED_OUTPUT_CAPABLE_MODEL
# AI_OPENAI_API_KEY is supplied privately; never put its value in tracked files.
AI_TIMEOUT_SECONDS=8
AI_MAX_OUTPUT_TOKENS=1800
```

Then a tenant admin/integration engineer explicitly saves `provider=openai` and the exact
server-configured model via the AI configuration UI/API. No API-key configuration is
accepted through that API. Disabling the server flag gates the transport again, including
previously saved tenant configurations. Mock fallbacks remain visibly simulated.
The adapter uses a fixed HTTPS Responses URL, `store=false`, an empty tool list and strict
JSON schema following [official OpenAI documentation](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
No real OpenAI request was made during implementation, verification or demonstrations;
transport behavior is tested with `httpx.MockTransport`. Model access/schema compatibility,
latency and usefulness require a separately authorized real-model evaluation.

Optional USD pricing per million input/output tokens can be set with
`AI_INPUT_PRICE_PER_MILLION` / `AI_OUTPUT_PRICE_PER_MILLION`. Cost is null unless both
prices and both actual usage counters exist. Mock token counts and costs are always null.

## API inventory

All routes are active-tenant scoped under `/api/v1/ai`:

| Method | Path | Boundary |
|---|---|---|
| GET / PUT | `/configuration` | Operations read / integrations manage |
| POST / GET | `/analyses` | Operations read; mutation requires Origin and CSRF |
| GET | `/analyses/{id}` and `/{id}/evidence` | Current evidence authorization |
| GET | `/recommendations`, `/review-queue` | Safe tenant queue projection |
| POST | `/recommendations/{id}/validate` | Recheck current evidence and role |
| POST | `/recommendations/{id}/review` | Manager; revision-bound decision |
| GET | `/recommendations/{id}/reviews` | Immutable, paged decision history |
| POST / GET | `/evaluation-runs` | Manager runs / operations read |
| GET | `/evaluation-runs/{id}/results` | Tenant-scoped case evidence |
| GET | `/metrics` | Real retained AI telemetry |

Pagination uses offset/limit (maximum 100). BFF method/path/query allowlists are exact;
there is intentionally no AI execute, connector, SQL, URL or recovery-confirm endpoint.
Configuration and review actions reuse tenant locking and the existing audit recorder.
The existing recovery approval models are reused downstream rather than creating AI
approvals that could replace them. AI review decisions are a separate advisory-domain
history because acceptance does not approve a particular recovery command.

## Transactions and retention

The authenticated request transaction holds the tenant lock for request idempotency,
quota checks, provider result persistence and recommendation creation. It can hold that
lock for the bounded provider timeout; this is a lightweight correctness tradeoff, not a
throughput claim. Only AI metadata and existing safe audit events are written. Evaluation
runs write case results atomically. Review state/version and decision history commit
together under the same tenant lock. Recovery remains a distinct later transaction.

Eight additive tables use aware timestamps, tenant/time indexes, tenant-qualified foreign
keys, request/config/review uniqueness and constrained states. Database triggers reject
updates to configurations, result snapshots, evidence snapshots, decisions and evaluation
case results. Recommendation state/eligibility is mutable through explicit locked APIs.
AI result/history retention is bounded (default 30 days/1,000 analyses and 100 evaluation
runs) and pruned lazily on explicit analysis/evaluation requests, at most 100 rows per
selection. This allows a temporary surplus until subsequent requests, not a background
retention SLA. AI-only retention can delete expired decision snapshots; existing platform
audit and business/recovery records are retained. Configuration versions are kept for
provenance. No scheduler, Redis, new worker or external vector database is introduced.
