# Evidence retrieval and grounding

The database, deterministic planner and authorized principal define evidence. Provider
prompts are never an authorization boundary. Retrieval starts with `operations:read`,
checks the active tenant in every query and follows explicit stored identifiers only.
Audit evidence additionally requires `audit:read`; details/revalidation repeat that check,
so an administrator's historical analysis cannot disclose audit evidence after a role change.

Supported projections include incident category/severity/status, activity action/outcome,
workflow history and step state/error category, sync/conflict and linked plan decisions,
notification attempts, connection health/status, circuit state, credential status and derived
expiration, operational findings and relevant audit action identifiers. Versioned runbooks
supply their stable identity/version only; their untrusted prose is not sent to providers.
Raw webhook text, record titles, names, email addresses, free-form notes, payloads, workflow
outputs, encrypted credentials and plaintext credential values are excluded by construction.

Sources carry stable `type:UUID` identifiers, an aware source timestamp, fixed internal
source link and redacted bounded summary. Runbooks use `runbook:slug:vN` and a null timestamp.
Related queries cap at eight per source; combined sources cap at 32 plus one runbook. Context
caps at 24 KiB and excess is rejected. Truncated or deleted historical sources do not prove
an event never happened. Generic source references are application scoped; only AI-owned
request/result/review references use composite database tenant foreign keys.

The output schema rejects extras, unknown actions, tool instructions, oversized arrays and
strings. `OBSERVED_FACT` must copy an exact authorized projection and cite it. A supported
inference is limited to one fixed investigation sentence with real citations. `UNKNOWN`
uses a fixed statement that root cause is not established. Summary, limitations and missing
evidence use fixed language; every recommendation must match a deterministic candidate
exactly, including target, citations, effect, risk and human-review requirement. The model
cannot invent a new URL or field-override action. Empty operational evidence requires
explicit abstention and no recommendations. Factual content outside these fields is not
accepted as free-form narrative.

This intentionally extractive contract is narrower than a general purpose assistant.
Citation validity alone does not prove arbitrary natural-language entailment, so arbitrary
claims are rejected rather than pretending to score their truth. Authorized retained source
records themselves may be incomplete, stale or erroneous. Advisory classification is
restricted to the observed planner classification, persisted severity (when present) and
existing runbook; it cannot overwrite deterministic classification. Disagreement metadata
is recorded, but this constrained implementation cannot emit a different unsupported class.
No claim of real-model diagnostic intelligence or calibrated confidence is made.

Evidence snapshots and the planner's current state digest form the analysis revision.
Revalidation retrieves again using current authorization and compares revisions. Source
changes, disappearance, changed receipts, field authority and planner eligibility therefore
block stale recommendations. Snapshot citations remain inspectable only under the current
principal's evidence permissions.
