# External identity registry

An external identity is `(tenant_id, connection_id, source_kind, external_id)`. PostgreSQL enforces that namespace as unique. CRM/ATS IDs are not globally unique, and the same identifier on different connections/kinds/tenants is independent. Missing lookups return an empty paginated list; wrong-tenant object IDs return generic 404.

First successful normalization creates a UUIDv5 canonical record and identity association. Later accepted input for that identity resolves the same canonical ID. Mapping edits and reprocessing never allocate a second identity. Canonical/source type compatibility is checked from stored connector metadata and fixed supported combinations; a candidate identity cannot link to a contact/job record.

An authorized manager may explicitly link an additional source identity to an existing same-tenant, same-entity canonical record with POST /api/v1/external-identities. The association is append-only through current APIs and records MANUAL_LINK provenance. Existing identities cannot be silently reassigned, even to the same target; duplicate/collision creation returns 409. There is no automatic CRM/ATS match based on names/email/external-ID resemblance and no merge/detach API.

Composite foreign keys bind the connection/tenant and canonical/entity/tenant, preventing cross-tenant links even through direct SQL. Tenant-scoped queries and role checks additionally protect normal API use. The registry retains source update high-water time and source payload digest. Per-field provenance and immutable attempts retain exact mapping-version and event attribution, so a historical link is not an inferred workflow execution.

GET /external-identities supports bounded connection_id, canonical_id, source_kind and external_id filters with offset/limit. The Mappings UI shows associations and supports explicit linking and lookup. Lists never reveal credentials or raw source payloads.

Identity association does not grant authority to overwrite canonical fields. A linked ATS job can supply recruiting_status, while CRM retains commercial fields; origin-owned shared fields remain owned by their first connection. Any rejected merge keeps the association/history and leaves canonical business state unchanged. See [FIELD_AUTHORITY.md](FIELD_AUTHORITY.md).

Limitations: no identity reassignment/merge reconciliation, tombstones, historical provider-ID rename, global IDs, database RLS or automatic deduplication of different source identities. UUIDv5 is deterministic identification, not an authentication secret. Provider ID lifecycle changes need explicit future governance.
