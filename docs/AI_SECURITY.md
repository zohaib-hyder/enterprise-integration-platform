# AI security boundaries and limits

All platform records, webhook content, runbooks and provider output are untrusted data.
Tenant membership and per-source permissions are checked before retrieval; the model
has no database handle, tenant selector, query construction, URLs or tools. The optional
provider receives fixed instructions separately from serialized bounded evidence. Only
safe fixed projections are sent, never raw payloads, free-form user prompts or secrets.

Defenses include fixed SQL queries, explicit tenant predicates, current audit permission
rechecks, strict Pydantic contracts, bounded context/output/transport, literal extractive
claims, authorized citation membership, exact action candidate matching, fixed internal
links, existing RBAC/Origin/CSRF, locked version-bound review and the unchanged recovery
planner/confirmation/independent-approval path. React renders text rather than raw HTML.
Credentials remain backend SecretStr environment configuration and never enter API or
frontend bundles. Provider errors are categorized; raw responses, prompts and exception
strings are not logged or persisted on invalid output. No provider retries or autonomous
callbacks exist. Optional transport redirects are disabled and destination is fixed.

Tests exercise instruction override, credential extraction, fake system messages,
cross-tenant references, forged citations, arbitrary execution and malicious source text.
Webhook text is excluded from AI retrieval entirely; an attacker cannot promote it to
system instructions by embedding it in a signed source record. Existing HMAC and ingestion
regressions still run. The live malicious-source case explicitly seeds a synthetic note
and verifies it is absent from the provider-safe evidence/result.

Prompt injection is not completely solved. These controls deliberately limit the assistant's
expressiveness and available capabilities; they do not prove every future provider/model,
new projection, new free-text field or semantic inference is safe. Models may refuse, time
out or produce invalid schema even with structured output, triggering sanitized failure
or labelled mock fallback. Process-local concurrency is per API process; tenant quota and
idempotency use PostgreSQL. Synchronous HTTP request threads are timeout-bounded but client
disconnect does not guarantee upstream cancellation. External real-model privacy, access,
pricing, throughput, production security audits and full browser E2E are unverified and
remain outside this phase. Phase 11 should independently audit these boundaries.
