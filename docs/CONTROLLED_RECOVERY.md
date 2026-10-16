# Evidence-based controlled recovery

The recovery planner is read-only. It loads a tenant-owned target, relevant durable steps/receipts/metadata and any explicit incident/finding link. A SHA-256 digest of that snapshot is the evidence revision. It returns observed classification, exact evidence references, certainty described as observed (never AI causation), a versioned runbook and eligible/blocked actions.

Plans explain required permission, expected effect, preconditions, idempotency protection and risk. Supported options are RETRY_NOTIFICATION, REPLAY_WORKFLOW, RESUME_ELIGIBLE_EXECUTION, RECHECK_CONNECTOR, REFRESH_SIMULATED_CREDENTIAL, RECONCILE_SYNC, REVIEW_SYNC_CONFLICT, REVIEW_UNKNOWN_OUTCOME, ACKNOWLEDGE_ALERT and ESCALATE_FOR_MANUAL_REVIEW. Options are target-specific, not a promise every action is available on every record.

## Preview → request → independent review → confirm

1. POST `/api/v1/operations/recovery/preview` with target kind/UUID. This does not change business or operations records.
2. POST `/recovery/requests` with chosen action, exact evidence revision, stable client UUID and safe reason. The explicit request materializes/audits the reviewed preview. UUID reuse with another actor/target/action/reason/revision is rejected; identical retries return the same request. Incident attachment must have an explicit target relationship, not merely tenant membership.
3. Workflow replay and simulated credential refresh require an approval from a **different active admin/engineer**. The approval is bound to request/tenant/target revision and expires with the 15-minute request. The requester cannot self-approve; a decision is final and audited. Rejection blocks confirmation.
4. The original requester explicitly confirms `confirm: true` and the same revision. Tenant/source locks pin the current target; permission, state, receipt proof, revision, expiry and approval are revalidated. STALE/EXPIRED/BLOCKED/FAILED outcomes persist with audit and return 409. Successful confirmation is database-idempotent; repeated confirmation returns the recorded result without executing another action.

An approval is **not technical evidence**. Unknown workflow action receipts, unverifiable completed actions, unknown linked deliveries or missing ATS action receipts block replay even if a reviewer approved the request. Expired approvals and revoked permissions fail closed. Safe receipt review can leave a delivery UNKNOWN; the recovery outcome is then `REVIEWED_UNRESOLVED`, not delivered. No force-success or force-resend endpoint exists.

## Owning service boundaries

- Workflow replay delegates to Phase 5's existing checkpoint/root receipt-preserving replay. A queued replay is not reported as completed success.
- Notification retry delegates to Phase 8's scheduled retry contract. It preserves budget, Retry-After and due time; it does not reset exhausted actions or call ATS synchronization.
- Unknown review checks existing provider key/hash/receipt evidence; no resend.
- Connection health uses the existing synthetic health check. A healthy probe is not proof that a previous action succeeded.
- Simulated refresh uses Phase 6 encrypted token lifecycle under the existing connection lock, exposing outcome only.
- Sync reconciliation uses the existing bounded read-only service. Conflict review links the existing digest/authority-protected workflow; the command center does not override field ownership or ambiguous identity.
- Alert acknowledgement uses the existing alert service/permission.
- Escalation records an operator action without business writes. Unavailable historical references allow escalation only.
- Resume is intentionally blocked as a separate state mutation: the existing worker owns pending/due and expired-lease recovery. Restart that runner rather than writing execution states directly.

There is no independent recovery scheduler/worker. Confirmation delegates inside its database transaction, with nested rollback for owning-service rejection; original workers execute any queued workflow/action. Metadata failures roll back the request/action transaction. Database unavailability can itself prevent durable recording; the implementation does not claim impossible audit durability during an unavailable commit.

The command-center approval policy governs **command-center recovery requests**. Existing Phase 5–8 domain endpoints retain their prior authenticated permissions and technical safety contracts to preserve the verified baseline. This is not a new global policy engine; callers of those existing endpoints are still constrained by their own receipt/authority/idempotency safeguards. No existing technical guard was relaxed or overridden.

The UI shows exact evidence, reason, request state, approval queue/history and an explicit confirmation checkbox. Failed/stale confirmation reloads the durable request. Expiry display uses the server's last observation; backend expiry checks use current server time. All mutation routes retain Origin/CSRF and exact BFF path/query allowlisting.
