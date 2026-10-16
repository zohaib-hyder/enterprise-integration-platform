# AI evaluation methodology

The offline harness is `backend/app/ai/evaluation.py`. It always instantiates the mock
provider directly, even if a tenant/server optional provider is configured. It contains
60 deterministic synthetic cases: 15 operational/safety categories × four variants
(grounded response, fabricated citation, unsupported execution action, injection attempt).

Categories cover incident explanation, failed workflow, connector outage, expired
credential, rate limiting, sync conflict, notification failure, unknown provider outcome,
worker restart, insufficient evidence, cross-tenant citation, prompt injection, unsupported
recommendation, stale evidence and independent human approval. Case-level results include
actual expected/observed validation outcome, citation IDs, individual check booleans,
classification, abstention, human review, safety eligibility and approval requirements.

The evaluator calls the production strict-output validator and pure eligibility policy.
Grounded cases must cite only supplied evidence and copy its summaries. Invalid variants
must fail with the expected categorized error. Empty evidence must abstain. The unknown
outcome fixture offers receipt review and never retry. Unsafe state, role and revision
fixtures must remain blocked/stale/insufficient even after simulated human acceptance.
Pending safe cases require human review; accepted safe cases allow preview only. Approval
metadata is asserted separately from acceptance. Redaction is checked against four explicit
attacks. There is no model-generated oracle or hardcoded pass result.

These are **mock contract evaluations**, not a representative diagnostic benchmark or real
model accuracy measurement. Cross-tenant fixture citation rejection is distinct from DB
authorization; actual PostgreSQL and BFF tests separately check tenant-owned records,
current-role permissions, history access, request limits and Origin/CSRF. Domain integration
tests exercise actual local notification 503/403/timeout, workflow review/independent
approval preservation and source-authority sync conflict. The twelve HTTP demonstrations
inspect real retained records, citations, blocked outcomes and no automatic execution.

Run without a server:

```sh
cd backend
.venv/bin/python -m app.ai.evaluation
```

Run through the manager-only UI or `POST /api/v1/ai/evaluation-runs` to persist the run
and all case results in the tenant. At most five explicit runs/hour are accepted. Inspect
case details in AI Insights. Null token/cost values on mock runs are deliberate.
The actual Phase 10 recorded results and final test counts are in [PHASE_10_REPORT.md](PHASE_10_REPORT.md).

An optional future real-model evaluation must be separately authorized, name the exact
configured model, use the same fixtures/guards, record actual usage/cost and failures, and
report its own results. Mock success must never be presented as real-model accuracy.
