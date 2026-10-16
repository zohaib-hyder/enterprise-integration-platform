# Recommendation safety and human review

AI cannot import or call recovery execution, create a recovery request, bypass field
authority, submit a connector operation or mutate a workflow/delivery/incident. The AI
service imports read-only evidence/planning and writes its own metadata plus safe audit.
Recommendations use the ten existing Phase 9 action names, never arbitrary tools.

The deterministic validator checks exact target/citation/candidate membership. Live
eligibility then calls the existing recovery planner for present-day state, ownership,
permissions, unknown-write receipt guards, retry/idempotency rules and approval requirements.

| Status | Meaning |
|---|---|
| VALID | Human accepted; current evidence and planner allow a preview handoff |
| REQUIRES_REVIEW | Technically eligible and role allowed, human acceptance still pending |
| BLOCKED | Planner/role/human decision prohibits this handoff |
| STALE | Evidence digest changed; request a fresh analysis |
| INSUFFICIENT_EVIDENCE | No authorized evidence for a decision |

Blocked options can appear with a visible BLOCKED label so an operator understands why
an unsafe retry is unavailable. AI never upgrades them. UNKNOWN deliveries cannot be
resent; exhausted deliveries cannot reset their retry budgets. Workflow replay keeps
existing receipt/checkpoint/principal proof and independent approval. Sync conflicts
open the existing source-authority/digest-guarded review instead of changing ATS fields.
Credential suggestions contain status metadata only, with independent approval retained.

Reviewers are tenant admins/integration engineers (`integrations:manage`). Operations
users can request, inspect and revalidate. A review requires the current recommendation
version and a safe meaningful comment. Only PENDING_REVIEW transitions to ACCEPTED,
REJECTED or NEEDS_REVISION. A new successful analysis of the same actor/target supersedes
previous NEEDS_REVISION recommendations and adds an immutable SUPERSEDED decision.
Terminal decisions cannot be edited or overwritten. Decisions and evidence snapshots
reject SQL updates; bounded retention is documented separately.

Acceptance returns only a recovery preview descriptor and an internal Operations link.
Opening that link runs a read-only preview. No RecoveryRequest or RecoveryApproval is
created. The operator must explicitly choose an action, provide the existing recovery
reason/request UUID and confirm the exact current revision. A separate authorized
reviewer must still approve actions requiring independent approval. AI acceptance can
be by the requester because it is an advisory review, never that independent command
approval. A stale/rejected/revision-requested/superseded recommendation has no handoff.
Existing Phase 0–9 low-level domain API contracts remain unchanged.

The pure eligibility policy is shared by live revalidation and offline fixture checks.
It is not a second workflow/recovery engine. All execution remains in existing owning
services with their existing transaction, lease, idempotency and authority boundaries.
