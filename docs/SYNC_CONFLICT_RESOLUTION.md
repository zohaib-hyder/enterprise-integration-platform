# Synchronization conflict review

Unsafe plans and apply-time races persist tenant-scoped conflicts. Categories are `STALE_SOURCE`, `DESTINATION_CHANGED`, `FIELD_AUTHORITY_VIOLATION`, `IDENTITY_COLLISION`, `AMBIGUOUS_MATCH`, `INVALID_MAPPING`, `CONCURRENT_UPDATE` and `UNKNOWN_WRITE_OUTCOME`. Evidence includes a typed sanitized requisition snapshot, field differences, authority, identity references, version IDs, timestamps, record digests and explanation. Credentials and arbitrary connector configuration are excluded.

## Review contract

GET `/api/v1/sync/conflicts/{id}` returns the historical plan plus current synthetic CRM/ATS data and current digests. An optional validated `destination_external_id` retrieves a proposed existing ATS link candidate and its digest within this configuration's destination connection.

POST `/{id}/resolve` requires same-origin CSRF, an authenticated tenant membership with `sync:resolve`, action, a reason (8–500 characters), expected conflict revision, reviewed source/destination digests and a UUID request key. Linking also requires the selected external ID and reviewed link digest. The tenant lock revalidates permissions and serializes review. Changed records/revision return 409; unavailable foreign tenant IDs return 404. Retrying the same key/action/reason returns the existing immutable resolution; reusing it for a different decision returns 409. Use only synthetic review reasons and never paste credentials into free text.

| Action | Operations user | Admin / engineer | Result |
| --- | --- | --- | --- |
| KEEP_DESTINATION | Yes | Yes | Resolve this review item; no provider write |
| SKIP_UPDATE | Yes | Yes | Resolve this review item; no provider write |
| ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS | No | Yes | Refresh current CRM, re-plan against reviewed destination, queue safe fields |
| RETRY_AFTER_REFRESH | No | Yes | Refresh and re-plan; destination conflicts still hold |
| LINK_EXISTING_IDENTITY | No | Yes | Verify candidate digest/existence/ownership, link through Phase 4, re-plan and queue |

Manual accept removes the historical destination-baseline conflict for the reviewed state, not authority or optimistic guards. Missing/ambiguous authority, duplicate identities, unsupported mapping and stale source governance still block it. Authority remains the Phase 4 tenant rule/provenance mechanism; there is no override or new policy-bypass API. Correct governance/mapping through its authorized lifecycle first. A stale mapped identity cannot be silently replaced by a link action; the existing registry must be governed explicitly.

## Durable recovery

Write resolutions do not execute ATS inline in the browser request. They create a safe current plan and seed it in an immutable Phase 5 recovery workflow version. The existing worker runs apply/record with the operator principal and the accepted original/latest source event as ownership anchor. Retries use the saved plan/action key. The resolution records that the operator's decision was accepted; consult the linked plan's sync execution for eventual success/hold/retry. A later concurrent change holds the recovery and creates fresh evidence.

Keep/skip resolves the individual conflict, retains the original held execution and does not claim a synchronized result or change the provider baseline. Other conflicts remain visible. Audit events contain only action and resource/actor IDs; resolution text is stored only in the authorized resolution history. No credential retrieval, real OAuth or customer communication is introduced.

Unknown/inconsistent historical receipts remain quarantined when refreshed; accepting source cannot erase uncertain evidence. Keep/skip can record a review decision without a new provider effect. Confirming a remote uncertain write or amending an identity registry is an explicit future governance task, not an automatic retry in this local implementation.
