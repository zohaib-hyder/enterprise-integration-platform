# Synthetic credential lifecycle

Phase 6 provides a tenant/connection-scoped inventory for API_KEY, OAUTH_ACCESS_TOKEN, OAUTH_REFRESH_TOKEN and SERVICE_ACCOUNT_REFERENCE. Values must be bounded `sim_` markers, not real provider credentials. No OAuth redirect, authorization grant, Google/Microsoft/HubSpot/Salesforce call, SMTP or external token endpoint is implemented.

## Storage boundary

A dedicated `CREDENTIAL_ENCRYPTION_KEY` holds 32 random bytes in canonical URL-safe base64. `CREDENTIAL_KEY_REFERENCE` defaults to local-v1. Up to three `CREDENTIAL_PREVIOUS_KEYS` references allow decryption of older rows during operator-managed key change. They are SecretStr settings and never appear in diagnostics. Webhook HMAC storage keeps its separate existing key and AAD; neither subsystem reuses the other’s encryption domain.

```sh
# Repository root; appends only if no credential key exists, never prints/replaces it
python3 scripts/setup_credential_key.py
```

AES-256-GCM uses a fresh 12-byte nonce. AAD binds tenant, connection, credential UUID, type, generation and encryption key reference. The encrypted JSON includes only a local issuer marker and secret value. Ownership composite foreign keys, a unique current credential per type and generation uniqueness reinforce application authorization. Missing/wrong keys or modified ciphertext/AAD fail closed with safe ENCRYPTION_UNAVAILABLE. There is no plaintext fallback.

Normal API responses contain masked metadata: UUIDs, type, generation, status, expiration/rotation/revocation timestamps, predecessor and key reference. They never return the value, nonce, ciphertext, derived token/hash or key bytes. Submission/rotation is one-time input, not one-time disclosure. The frontend uses a password DOM ref, clears it before sending and on detach, and never stores a value in React state, browser storage or URLs. Lost values are replaced rather than retrieved. Private local `.env` is ignored by Git; a real deployment would need separate secret-management operations.

## Lifecycle and enforcement

Default OPTIONAL mode preserves the existing credential-free local simulators. Inventory submission does not pretend to authenticate a real provider. Choose API_KEY, OAUTH or SERVICE_ACCOUNT_REFERENCE enforcement explicitly in the connection policy. OAuth-style rows require aware expiration timestamps. Metadata detects expiration without writing on GET; authorization/refresh records the actual expiration event and alert. Required missing/expired/revoked credentials fail permanently unless a supported OAuth refresh succeeds.

Admin/engineer submit, rotate and revoke under tenant then connection locks. Rotation retires the current row, inserts a new encrypted generation and records its predecessor; old ciphertext and audit evidence are preserved. Concurrent replacement of a superseded generation returns conflict. Revocation marks the row and connection unavailable. Configuration/replacement invalidates a previous health sample. Health checks cannot label required unavailable encryption/credentials or an open circuit healthy. Encryption key changes are not automated re-encryption; retain old references until historical ciphertext no longer requires them.

## Deterministic local refresh

Expired access in OAUTH mode checks the latest refresh row for active status, expiration, valid ciphertext/AAD and local issuer. Connection serialization prevents simultaneous refreshes. One request rotates both tokens and records SUCCEEDED; a concurrent request sees the new valid access and returns NOT_NEEDED, without another refresh record. Derived values use HMAC over connection identity, prior generation and synthetic refresh value; they are encrypted immediately and never disclosed. New access lasts five minutes and refresh lasts seven days.

Absent/revoked/expired refresh, revoked access or explicit REFRESH_FAILED injection records a sanitized FAILED refresh, connection unavailability, audit and operational alert. Authentication failure is permanent for the current workflow attempt, not an infinite retry. Rotation/replacement followed by explicit eligible workflow replay is the recovery path. Token refresh is a bounded local administrative operation, not a hidden retry of a business effect.

## API

Read-only metadata requires operations:read; all mutations require integrations:manage plus Origin/CSRF.

- GET/POST `/api/v1/connections/{id}/credentials`
- POST `/credentials/{credential_id}/rotate` and `/revoke`
- POST `/credentials/refresh`
- GET `/credentials/refresh-history`

Cross-tenant/connection IDs are unavailable. There is no reveal/export/decrypt endpoint. Audits use fixed action names and IDs without submitted values. Refresh history contains outcome/reason and credential UUIDs only. See [CONNECTOR_RELIABILITY.md](CONNECTOR_RELIABILITY.md) for worker transaction boundaries and [OPERATIONS_OBSERVABILITY.md](OPERATIONS_OBSERVABILITY.md) for alerts.
