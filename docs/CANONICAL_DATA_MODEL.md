# Canonical data model, version 1

Phase 4 publishes actual strict Pydantic contracts for six entities. The currently executable source adapters map CRM contacts/jobs and ATS candidates/jobs to three canonical entity types. Client, Application and Activity contracts and authority policies are implemented and validated, but no provider mapping template creates those entities yet. This is not a full staffing enterprise schema.

| Entity | Required fields | Optional fields / constraints |
| --- | --- | --- |
| Client | name | industry; headcount integer 0–10,000,000 |
| Contact | name, email, company | client UUID; aware last_contacted_at |
| JobRequisition | title, department, location | CRM commercial status, ATS recruiting status, decimal commercial rate, bounded description |
| Candidate | name, email, stage | skills, decimal experience 0–80 years, aware availability |
| Application | candidate UUID, job UUID, stage | aware submission time |
| Activity | INTERVIEW/EMAIL/NOTE kind, summary | aware start/end; end must follow start |

Email values use reserved synthetic `example.test` domains. Names/titles and arrays are bounded; numeric values reject booleans/non-finite conversions. Candidate stage is NEW/SCREENING/SUBMITTED/PLACED/REJECTED. Application stage is DRAFT/SUBMITTED/INTERVIEW/OFFER/PLACED/REJECTED. Job commercial/recruiting statuses are OPEN/CLOSED/PAUSED. Unknown fields and unsupported schema versions are rejected. These contracts do not authorize references to future entity types; business-relation FK enforcement for not-yet-supported entity creation is future work.

## Envelope and serialization

```json
{
  "schema_version": 1,
  "entity_type": "contact",
  "canonical_id": "11111111-1111-4111-8111-111111111111",
  "tenant_id": "22222222-2222-4222-8222-222222222222",
  "source_connection_id": "33333333-3333-4333-8333-333333333333",
  "source_external_id": "synthetic-contact-1",
  "source_updated_at": "2030-01-15T12:00:00Z",
  "normalized_at": "2030-01-15T12:01:00Z",
  "correlation_id": "44444444-4444-4444-8444-444444444444",
  "normalized_data": {"name":"Synthetic Contact","email":"contact@example.test","company":"Fictional Harbor"},
  "metadata": {"synthetic":true,"tags":[]}
}
```

Tenant/connection provenance comes from verified server context and the stored mapping, never payload tenant fields. Canonical IDs are UUIDv5 of tenant namespace and connection/kind/external ID unless an explicit existing identity association resolves another canonical record. Equal validated source input and mapping produce deterministic business data and IDs. `normalized_at` and correlation are execution metadata, not deterministic business fields; preview and execution legitimately have different times/correlations.

The envelope validates that normalized_data matches entity_type. Explicit nullable values remain present; absent optional mappings are omitted. Metadata permits only synthetic=true and at most ten bounded tags; runtime metadata is code-owned. Decimal values serialize safely as strings, timestamps are aware and normalized to UTC by the datetime operation. Record APIs return a separate validated **display schema** because redacted emails/names are intentionally not valid business values.

## Storage and provenance

Migration `575346b58b80` adds seven tables: canonical records, mappings, mapping versions, external identity associations, attempts, quarantined records and field authority rules. JSONB stores normalized data, declarative definitions, field provenance and bounded attempt snapshots. All tenant rows have explicit tenant FKs. Composite connection/tenant and canonical/entity/tenant FKs prevent mismatched identity ownership. The inbox gains an additive connection/tenant ownership constraint used by event attempts.

Canonical records have schema_version, revision, creation/update/normalization times and per-field provenance: connection, mapping version, source timestamp and attempt UUID. Only changed successful business data increments revision. Newer identical confirmations advance the owning field/source high-water mark without incrementing revision. The record envelope retains its primary source connection/external ID; linked-source field ownership is visible in field_provenance. The primary source_updated_at is not a global clock for all linked sources.

Attempt result snapshots preserve the proposed canonical representation for that exact mapping/input. They are not always the full merged record: an ATS job proposal can add recruiting status while existing CRM commercial data remains untouched. Read the canonical record for current merged state. Accepted inbox events are never edited and retain PENDING workflow-processing status.

## Compatibility and limits

Version 1 definitions/results are explicit and strict. Breaking changes require a new schema version, validation/mapping compatibility review and additive migrations; do not silently reinterpret historical JSON. Historical attempts always reference an immutable exact mapping version. Canonical schema definitions are available at GET /api/v1/canonical-schemas.

Read APIs recursively redact email/name and secret/token/password/authorization keys. The UI receives only display-safe business values; APIs never expose raw invalid source snapshots. Stored synthetic business JSONB is not encrypted; encryption applies to existing webhook credentials. There is no RLS, global provider identity, automatic merge, automatic synchronization, client/application/activity provider adapter, or retention/export policy yet.
