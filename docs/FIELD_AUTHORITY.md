# Source-of-truth and conflict protection

Authority rules are explicit, code-owned and instantiated per tenant on first mapping creation. GET /api/v1/field-authority exposes actual rules. There is no policy-editing or force-overwrite API in Phase 4.

| Entity/field | Owner |
| --- | --- |
| Client account fields; Contact fields | synthetic_crm |
| Candidate fields, including pipeline stage; Application fields/stage | synthetic_ats |
| Activity fields, including interview schedule | synthetic_calendar |
| Job status and commercial_rate | synthetic_crm |
| Job recruiting_status | synthetic_ats |
| Job title, department, location | ORIGIN: first connection that populates the field |
| Job description | REVIEW: explicit unresolved policy |

These implemented policies refine earlier provisional system-level assumptions. CRM commercial OPEN/CLOSED status and ATS recruiting OPEN/CLOSED status are distinct canonical fields. The same raw source `status` maps to the appropriate canonical field. Client/Application/Activity policies are contract foundations; those provider mapping templates are not executable yet.

A changed value requires matching connector-type authority. ORIGIN requires the exact first owning connection, not merely the same vendor type. REVIEW or missing policy produces MANUAL_REVIEW. Identical values can be observed by another source without transferring field ownership. A rejected proposal changes no canonical business fields and preserves a structured attempt/quarantine record. All fields in a proposal succeed or fail together; there is no silent partial merge of conflicting fields.

Source timestamps are aware UTC instants, not arrival order. Registry high-water time rejects older source updates. Equal timestamps with changed validated source payload produce OUT_OF_ORDER; they do not win by last-write-wins. Per-field provenance also rejects older/equal changed values across linked source identities. Missing source time or missing existing field time makes a changed update ambiguous and requires review. A new record may be created without source_updated_at, but later changed updates cannot silently invent ordering.

Newer identical confirmations advance the owning source/field high-water mark without incrementing the business revision. Replaying an exact previously processed fingerprint returns its historical attempt and does not restore old data. A new mapping version cannot bypass stale ordering. Provider clocks are assumed comparable for this local synthetic design; there is no sequence-number protocol or cross-provider clock-skew reconciliation.

Normalization locks the tenant parent, rechecks manager membership and locks resolved canonical records. Concurrent legitimate updates serialize; if a newer timestamp arrives first, an older one conflicts. If the older arrives first, the newer subsequently replaces only authorized values. PostgreSQL constraints enforce identity/ownership and the whole record/association/attempt transaction commits before response.

Conflicts are returned as HTTP 409 with fixed issues (AUTHORITY_CONFLICT, STALE_UPDATE, OUT_OF_ORDER, MANUAL_REVIEW) and exact attempt/version IDs. GET /transformation-conflicts and /invalid-records show tenant-scoped evidence; raw source snapshots remain private and response payloads are redacted. There is no administrative override or execution/retry worker. A manager may correct a declarative mapping and explicitly reprocess its quarantine; unresolved policy or timestamps remain blocked rather than overwritten.
