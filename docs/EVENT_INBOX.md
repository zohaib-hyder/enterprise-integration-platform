# Durable event inbox

Phase 3 adds migration `66667c2c593d` after `6835545912e5`, creating `webhook_endpoints`, `webhook_secret_versions`, `incoming_events` and `event_receipts`. No prior table/data is replaced. All tenant-owned rows have explicit tenant FKs. Composite ownership FKs bind endpoint → connection/tenant, secrets/events/receipts → endpoint/connection/tenant, and receipts → event/tenant/endpoint. Time values are timezone-aware; chronological and source/type indexes support bounded reads.

## Version-1 schema

```json
{
  "schema_version": 1,
  "source": "synthetic_ats",
  "event_id": "provider-candidate-001",
  "event_type": "candidate.created",
  "occurred_at": "2026-10-13T12:00:00Z",
  "external_id": "candidate-001",
  "data": {
    "name": "Synthetic Candidate",
    "email": "candidate@example.test",
    "stage": "NEW",
    "skills": ["Python"]
  }
}
```

Every shown envelope field is required; schema version is exactly 1. Unknown fields, tenant IDs, arbitrary types and secret fields are forbidden. IDs are bounded, safe identifiers. Candidate data uses the existing typed ATS schema and reserved synthetic email domains; only ATS accepts candidate.created/candidate.updated. Both sources accept job.created/job.updated, with data `{title, department, location, status}` using the requisition schema. Events do not modify simulator records or synchronize systems. The occurrence timestamp is provider business time and may be historical/future synthetic fixture time; freshness authenticates the separate signed delivery timestamp.

The receiver verifies HMAC **before** parsing JSON. It rejects malformed UTF-8/JSON, duplicate object keys and non-finite numbers. Payloads are normalized using validated models and bounded field lengths, then SHA-256 hashed over canonical sorted compact JSON. The signature hashes exact received bytes; the deduplication digest hashes normalized semantics, so signed whitespace/key-order variants can be identical duplicates. Equivalent datetime strings retain their validated timezone representation; this is not an exhaustive semantic-equivalence system.

## State and acknowledgements

| Delivery | HTTP | Inbox mutation | Receipt |
| --- | --- | --- | --- |
| New verified identity/payload | 202 accepted | One `ACCEPTED` / `PENDING` event | ACCEPTED |
| Same identity and normalized payload | 200 duplicate | Existing ID returned, event unchanged | DUPLICATE |
| Same identity with changed payload | 409 CONFLICTING_EVENT | Existing event never overwritten | CONFLICT |
| Valid signature, invalid JSON/schema/source | 422 fixed category | No event | REJECTED, no payload/provider text |
| Unknown/disabled endpoint, bad signature/time | 401 fixed category | None | None |
| Unsupported content type / oversized body | 415 / 413 | None | None |
| Missing/wrong key or database failure | 503 / 500 | No successful acknowledgement | Transaction rolls back on failure |

Deduplication uses a PostgreSQL unique constraint on `(tenant_id, endpoint_id, source, provider_event_id)`. Endpoint locking serializes normal concurrent ingestion and rotation; the constraint also protects against bypass of application duplicate checks. Different provider IDs are distinct events even with identical business data. The same ID on another authorized endpoint is a distinct namespace. Retries must reuse the correct provider ID. No separate nonce/dedup table is needed for this contract.

Accepted events store endpoint/connection/tenant attribution, provider identity/type, schema version, normalized payload/digest, occurrence/receipt/creation timestamps, correlation UUID and separate ingestion/processing statuses. Events remain **PENDING**. Duplicate/conflict/rejection are receipt outcomes, not fake workflow states or extra business events. Quarantine, workflow processing, outbox, retry workers, event editing and replay-to-execution do not exist in this phase.

Event insertion and receipt insertion commit together before returning a success; function-scoped dependencies enforce this. Signed schema-rejection/conflict receipts commit without an accepted event mutation. Unauthenticated rejection raises before receipt creation. Commit failure yields a sanitized 500 and rolls back event/receipt or endpoint/credential creation. There is no network I/O under database locks. Local UI simulation uses the same verifier in its authorized transaction. Raw SQL can bypass application validation but cannot bypass ownership/uniqueness/check constraints.

## Read and management API

Routes below are relative to `/api/v1`; browser calls use the same-origin `/api/platform` BFF. Public ingestion is called directly on FastAPI and is **not** a BFF route.

| Route | Permission |
| --- | --- |
| GET /webhook-endpoints; GET /webhook-endpoints/{id} | operations:read |
| POST /webhook-endpoints; PATCH /webhook-endpoints/{id} | integrations:manage + CSRF/Origin |
| POST /webhook-endpoints/{id}/rotate-secret | integrations:manage + CSRF/Origin |
| POST /webhook-endpoints/{id}/simulate | integrations:manage + CSRF/Origin |
| GET /webhook-endpoints/{id}/status | operations:read |
| GET /webhook-endpoints/{id}/receipts | operations:read |
| GET /events; GET /events/{id}; GET /events/{id}/receipts | operations:read |

Lists use offset 0–100000 and limit 1–100, ordered by timestamp/UUID. `/events` additionally accepts allowlisted `source`, `event_type`, `status` (`ACCEPTED` ingestion or `PENDING` processing), `since` and `until` aware timestamps filtering receipt time. Invalid/reversed time ranges fail. Query keys cannot override tenant identity. Event and endpoint IDs from other tenants return 404; tenant scopes derive from active verified membership. All three existing roles can read history; only admins/engineers manage or simulate.

Details expose recursively redacted payloads (email/name and secret/token/password/authorization keys), never raw webhook bodies, signature headers, signing credentials, encrypted bytes or nonces. Stored normalized candidate data is synthetic but is not encrypted; payload-at-rest encryption beyond signing secrets is future scope. Read endpoints expose no event editing or execution control.

## Observability and honest UI

Events shows actual persisted records, ingestion/processing badges, duplicate receipt counts, source/type/time filters, redacted details and delivery history. Endpoint status counts aggregate actual ACCEPTED/DUPLICATE/CONFLICT/REJECTED receipts and fixed authenticated error categories. Receipt duration measures verifier/persistence work before commit, not provider delivery latency or workflow execution. Logs record request duration, UUID correlation and fixed webhook outcome; bodies, headers, public endpoint identifiers and secret values are excluded. Endpoint lifecycle, rotation and explicit simulations append sanitized tenant audit events.

Unauthenticated signature rejections are visible as categorized request log events, not tenant-attributed counters. No global cross-tenant metrics are exposed. Counts are synthetic local inbox facts, not provider success rates. Histories are paginated but do not yet have automatic retention or immutable export. Offset lists are not snapshot cursors. Public endpoint URLs and provider identifiers are safe metadata, not signing secrets.

## Verification

From `backend`, with both local servers running:

```sh
.venv/bin/python ../scripts/verify_phase3_http.py
```

This creates a synthetic endpoint through BFF, performs real signed HTTP and actual sender CLI delivery, proves one-event deduplication, rejects signature/body/stale tampering, exercises grace/zero-grace rotation, reads the exact persisted records through dashboard APIs, checks other-tenant denial and scans logs/audit for secret leakage. It keeps two accepted pending synthetic events and disables the verification endpoint afterwards. Secrets remain in memory/private child environments, never disk or printed output.

The backend suite uses real migrated isolated PostgreSQL schemas for persistence, ownership, uniqueness, concurrent duplicates and transaction failures. Frontend tests cover the real component's data/render/action contracts. Browser visual/E2E inspection and external provider interoperability are separate checks and must not be inferred from these tests.
