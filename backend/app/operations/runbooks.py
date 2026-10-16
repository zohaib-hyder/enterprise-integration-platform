"""Version-controlled operator instructions; no executable code or external links."""

CATALOG = {
    "webhook-rejected": (
        "Webhook rejected",
        "Inspect signed receipt outcome and endpoint configuration.",
        "A rejected receipt does not establish an accepted business event.",
        "Correct the local source/schema; submit a newly signed synthetic event.",
        "Escalate missing tenant-attributable evidence; never disable HMAC.",
        "Verify an ACCEPTED receipt and durable event; do not replay rejected payloads.",
    ),
    "workflow-stuck": (
        "Workflow stuck",
        "Inspect execution state, due time, lease and step history.",
        "Wait until the documented grace/lease expires.",
        "Run the existing worker; preview safe replay only for eligible terminal executions.",
        "Escalate unknown effects or missing action receipts.",
        "Verify a fresh heartbeat, lease recovery history and forward progress.",
    ),
    "connector-unavailable": (
        "Connector unavailable",
        "Inspect connection health, reliability observations and circuit state.",
        "Connection must be owned by the active tenant.",
        "Recheck the synthetic connector; preserve circuit cooldown and scheduled retries.",
        "Escalate repeated failure after configuration repair.",
        "Inspect actual subsequent operation/observation; a health probe alone is not delivery success.",
    ),
    "credential-expired": (
        "Credential expired",
        "Inspect masked credential metadata and refresh history.",
        "Synthetic token pair and management permission required.",
        "Request reviewed simulated refresh; never reveal or paste credentials.",
        "Escalate revoked/missing refresh token or encryption failure.",
        "Verify refresh outcome and subsequent connector observation.",
    ),
    "rate-limit-exceeded": (
        "Rate limit exceeded",
        "Inspect quota observations, Retry-After and next-attempt time.",
        "Preserve provider deferral and bounded attempt budget.",
        "Allow the existing scheduled retry; inspect budgets before editing policy.",
        "Escalate repeated exhaustion; do not accelerate retry storms.",
        "Verify due-time admission and a recorded successful action.",
    ),
    "sync-conflict": (
        "Sync conflict",
        "Inspect plan, authority decisions, canonical identity and current destination.",
        "Fresh source/destination digests and appropriate role required.",
        "Open existing conflict review; reconcile without automatic repair.",
        "Escalate ambiguous identity/unknown write evidence; approval cannot override authority.",
        "Verify reviewed resolution, new plan and exact ATS operation receipt.",
    ),
    "notification-failed": (
        "Notification delivery failed",
        "Inspect attempts, channel, receipt, next-attempt time and classified error.",
        "Target only the incomplete delivery; ATS success is independent.",
        "Request an eligible scheduled retry or inspect permanent failure.",
        "Escalate exhausted/unknown actions; never replay ATS to resend a notification.",
        "Verify provider receipt and final delivery state; other actions remain unchanged.",
    ),
    "unknown-outcome": (
        "Unknown delivery outcome",
        "Inspect provider key/hash and immutable action receipts.",
        "No resend without proof; approval is not evidence.",
        "Use receipt review; otherwise preserve UNKNOWN and escalate.",
        "Escalate any absent/mismatched receipt or ambiguous identity.",
        "Verify matching receipt evidence before claiming confirmation.",
    ),
    "worker-restart": (
        "Worker crash or restart",
        "Inspect tenant-associated lease expiry and LEASE_RECOVERED history.",
        "Confirm lease expiration before replacement work; use the existing runner.",
        "Restart the normal worker; stale tokens are fenced.",
        "Escalate repeated lease losses or unavailable database.",
        "Verify fresh attempt/history, one committed effect and no duplicate receipt.",
    ),
    "calendar-conflict": (
        "Calendar conflict",
        "Inspect stable event ID, interval, request revision and delivery history.",
        "Explicit recruiting authorization and synthetic participants required.",
        "Check availability and authorize a new reviewed reschedule/cancel request.",
        "Escalate ambiguous action receipts; do not automatically schedule around conflicts.",
        "Verify intended stable event revision and action receipt; no real invite is sent.",
    ),
}


def runbook(identifier: str) -> dict | None:
    values = CATALOG.get(identifier)
    if not values:
        return None
    title, evidence, preconditions, options, escalation, verification = values
    return {
        "id": identifier,
        "version": 1,
        "title": title,
        "symptom": title,
        "evidence_to_inspect": [evidence],
        "preconditions": [preconditions],
        "safe_recovery_options": [options],
        "escalation_conditions": [escalation],
        "verification_steps": [verification],
    }
