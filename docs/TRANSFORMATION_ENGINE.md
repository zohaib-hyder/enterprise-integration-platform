# Deterministic transformations and mapping lifecycle

The pure engine in `backend/app/transformations/engine.py` validates a source envelope (`{"data": ...}`), extracts only allowlisted paths, applies explicit operations and validates the target entity. It performs no I/O, code execution, eval, runtime imports or provider calls. Source schemas allow bounded optional known fields so missing/invalid business values can produce structured quarantine evidence rather than untyped storage.

Four templates exist: CRM contact → Contact, CRM requisition → JobRequisition, ATS candidate → Candidate, ATS requisition → JobRequisition. Mapping identity is tenant + connection + source kind. Target entity is fixed by the stored connector/source combination; users cannot select an arbitrary tenant, provider implementation or target type.

## Definition format

```json
{
  "schema_version": 1,
  "fields": [
    {"target":"name","source":"data.name","required":true,"null_handling":"reject","steps":[{"operation":"trim"}]},
    {"target":"email","source":"data.profile.email","required":true,"null_handling":"reject","steps":[{"operation":"email"}]},
    {"target":"company","source":"data.company","required":true,"null_handling":"default","default":"Fictional Harbor","steps":[{"operation":"trim"}]}
  ]
}
```

Mappings permit 1–20 distinct target fields and at most five steps per field. Source paths come from the chosen source schema; nested profile.email is explicitly allowlisted. Required target fields must have mappings. Unknown fields, duplicate targets, unsupported source paths/operations, oversized definitions and unsupported enum outputs fail before version creation/activation. Definitions are capped at 12KiB; source payloads accepted by the engine are bounded to 16KiB. Defaults and enum dictionaries are independently bounded.

| Operation | Behavior |
| --- | --- |
| Rename/extract | target/source paths in a field rule; no expression evaluation |
| trim | string whitespace trimming |
| email | string trim/lowercase; final synthetic email validation |
| enum | exact explicit source-value map, at most 20 entries; known target enums enforced |
| datetime | aware ISO timestamp → UTC; naive/invalid values fail |
| number | bounded finite decimal or exact integer; exponent bounded before formatting; rejects booleans, fractions for integer, invalid/non-finite numbers |
| string | primitive scalar conversion with explicit maximum length; no objects, truncation or custom repr |
| Defaults/nulls | reject, omit, default or preserve explicit null; final canonical constraints still apply |

No partial canonical mutation occurs if any field fails. Errors contain only fixed categories and allowlisted target field names, never raw values or Pydantic input dumps. Categories include INVALID_MAPPING, INVALID_SOURCE, MISSING_FIELD, INVALID_VALUE, CANONICAL_VALIDATION, AUTHORITY_CONFLICT, STALE_UPDATE, OUT_OF_ORDER and MANUAL_REVIEW.

## Immutable versions

Creating a mapping creates active version 1. Saving edits inserts a new immutable version, leaving activation unchanged. Activation selects a validated version from that same tenant/mapping. PostgreSQL enforces uniqueness of mapping/version, a deferred active-version FK and a trigger that rejects UPDATE or DELETE of version rows, including raw SQL. Each version has a canonical definition digest and creator identity. Old attempts retain their version FK and result snapshot; activation does not reinterpret history.

Preview runs the saved selected version, returns structured outcome and redacted proposed envelope, and writes no attempt/record/identity. Draft editor changes must be saved as a new version before preview. Explicit normalization creates an attempt and, if valid/authorized/fresh, merges canonical data and records external identity in one transaction. A tenant parent lock plus canonical row lock prevents races and lost updates. This coarse local serialization trades throughput for straightforward correctness; there is no network I/O under locks.

## Outcomes, quarantine and reprocessing

Success/NO_CHANGE returns HTTP 200. Invalid input creates QUARANTINED attempt + OPEN quarantine row and returns 422. Authority/order conflicts or unresolved authority create CONFLICT/MANUAL_REVIEW evidence + OPEN quarantine row and return 409. Returning structured error responses instead of raising after persistence allows evidence to commit. A database commit failure returns sanitized 500 and rolls back all mutations.

The attempt fingerprint includes exact input digest, source identity/time, mapping version and optional event ID. Identical re-execution returns the same historical attempt without another record. Source ordering still applies to new fingerprints/new mapping versions. A duplicate acknowledgement is not an instruction to revert current canonical state.

Reprocessing requires a different corrected version from the same mapping/connection. It reuses the bounded stored known-source snapshot and identity; successful retry marks the original quarantine RESOLVED and references the resolution attempt, without altering old evidence. Repeated reprocessing deduplicates. Unknown unsafe source fields are discarded rather than stored; wholly invalid source shapes may therefore need a separate corrected source normalization instead of snapshot reprocessing. There is no force-overwrite or arbitrary payload editing endpoint.

## APIs and demonstration

All routes are relative to /api/v1 and tenant-authorized; the browser uses the existing same-origin BFF. Read routes cover canonical-schemas, mapping-templates, mappings/details/versions, canonical-records/details, external-identities lookup, field-authority, transformation-attempts/details, invalid-records/details and transformation-conflicts. Lists have offset 0–100000 and limit 1–100 with bounded typed filters.

Admins/engineers use POST /mappings, /mappings/{id}/versions, /activate, /preview, /normalize; POST /external-identities; POST /invalid-records/{id}/reprocess; and POST /events/{id}/preview-transformation or /normalize. Every mutation and preview retains Origin/session-bound CSRF. Operations users read non-sensitive records/evidence only. There are no version-update/delete APIs or arbitrary code selectors.

From the root, after existing configuration and PostgreSQL are available:

```sh
cd backend
.venv/bin/alembic upgrade head
.venv/bin/python -m app.seed_demo
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
```

In another terminal: `cd frontend && npm run dev`. Sign in, select a demo organization and open /mappings. Inspect a template/version, edit allowlisted field rules, save a new version, preview it, then activate explicitly. Normalize a source or choose a preserved accepted event. Inspect canonical provenance, identities, attempts, quarantine and conflicts. The seed ensures eight mappings, twenty records, twenty-two identity associations, twenty-four attempts and four invalid/conflict rows across two demo organizations on an untouched demo dataset; additional existing data is retained.

With both servers running, from backend: `.venv/bin/python ../scripts/verify_phase4_http.py`. It verifies real BFF actions, signed-event normalization, history, collisions, stale/authority protections and repeated seeding without printing secrets. No workflow execution, automatic event dispatch or CRM-to-ATS sync is implemented.
