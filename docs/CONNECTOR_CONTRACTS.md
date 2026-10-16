# Connector contracts

Phase 2 adapters implement `Connector` under `backend/app/connectors/contracts.py`. `validate_config` validates safe fields, `health_check` reports local availability, `supported_operations` returns fixed payload model classes, and `execute` receives a database session, verified tenant/connection, config and `ConnectorOperation`, returning a validated `ConnectorResult`. Services enforce membership and permissions before calling adapters. The interface is for trusted application code, not an authorization bypass.

`ConnectorType` allowlists `synthetic_crm`, `synthetic_ats`, `synthetic_slack`, `synthetic_email`, `synthetic_calendar` and disabled `generic_rest`. The registry resolves only explicitly registered implementations and rejects unknown/duplicate registration. Capability metadata exposes typed config and operation JSON schemas, version, mutating flags and explicit limitations. Global persisted definitions are migration-owned; no runtime plugin loading exists.

## Versioning and validation

Configuration, operation envelopes and results use `schema_version: 1`. Unknown fields and other versions fail closed. An envelope is `{ "schema_version": 1, "name": "contact.list", "payload": { "offset": 0, "limit": 25 } }`. Names are enumerated, then validated against the selected adapter's supported operation map. Payload models enforce field bounds, UUIDs, fixed stages/statuses, safe identifier characters, reserved synthetic email domains and aware event times. Inputs are never echoed in errors.

Successful output is `{ "schema_version": 1, "operation": "contact.list", "synthetic": true, "data": ... }`. Data is a validated record, record page, simulated delivery reference, event or event page. IDs and timestamps are durable PostgreSQL values. Record inspection omits delivery keys and internal payload fingerprints. Offset pagination orders by creation timestamp and UUID, with `limit` 1–100. It is not a snapshot cursor under concurrent writes.

## Errors

HTTP errors have `detail`, `error: {category, retryable}` and a correlation UUID. Messages are fixed safe strings. Unexpected adapter exceptions become `INTERNAL_ERROR` without raw provider/database text. Validation errors at the HTTP envelope use the existing safe FastAPI validation contract.

| Category | HTTP | Retryable |
| --- | --- | --- |
| AUTHENTICATION_FAILED | 401 | No |
| AUTHORIZATION_FAILED | 403 | No |
| RATE_LIMITED | 429 | Yes |
| TIMEOUT | 504 | Yes |
| TEMPORARY_UNAVAILABLE | 503 | Yes |
| VALIDATION_FAILED | 422 | No |
| NOT_FOUND | 404 | No |
| CONFLICT | 409 | No |
| UNSUPPORTED_OPERATION | 400 | No |
| INTERNAL_ERROR | 500 | No |

Retryable is advisory metadata; no retry worker runs. Cross-tenant/unknown connection IDs receive generic 404. Inactive memberships, unavailable identity and insufficient permissions use existing identity security errors.

## Idempotency and transaction semantics

CRM/ATS full upserts match `(tenant, connection, kind, external_id)`. Updates replace validated business payload and retain ID/creation time. CRM and ATS remain independent. Message and calendar-create payloads require 8–128-character keys matching `[a-zA-Z0-9][a-zA-Z0-9_.:-]*`. The same key/input returns the original ID with `duplicate: true`; changed input returns 409. Keys remain scoped to tenant/connection. Notification/email results report `SIMULATED`; no transport or real delivery exists.

Calendar create fingerprints original validated input. Retrying it after an event update/cancel returns the current state of that same event. Update replaces calendar/title/time fields; cancelled events cannot be updated. Cancellation is idempotent. Booking intervals must be timezone-aware with end after start. Overlap compares half-open intervals within a connection/calendar, excluding cancelled events; adjacent events and separate calendars are allowed. No attendees, recurrence, working hours or timezone/DST business policy exists yet.

Execution, durable mutation and sanitized audit commit together before HTTP success. Connection locking makes simulator operations safe across concurrent database sessions. Failed operations roll back. Future real provider adapters must introduce durable intent/outbox and reconciliation without holding a database transaction across network I/O.

## Future providers

A future adapter can implement the same versioned configuration, capabilities, health, operations and classified result/error shape. Explicit registration is required. Before transport is enabled it needs credential references (never public plaintext tokens), operation permission review, provider-ID namespaces, field authority, rate-limit handling, timeouts, bounded responses, observability redaction and provider-specific contract tests. Real generic REST also needs fixed allowlisted HTTPS origins, validated/pinned resolved public addresses, rebinding defense and redirect refusal/revalidation. Phase 2 does not implement those transport guarantees and keeps REST disabled.
