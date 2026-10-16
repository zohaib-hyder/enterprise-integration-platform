# Webhook security and local demonstration

Phase 3 accepts synthetic CRM/ATS webhooks into a durable inbox. The public receiver is separate from browser management: `POST /api/v1/webhooks/ingest/{public_id}` uses HMAC authentication and does not inspect session cookies or require CSRF. All endpoint creation, enable/disable, rotation and UI simulation routes retain session authorization, exact Origin and session-bound CSRF. Only tenant admins/integration engineers manage endpoints; all active roles with `operations:read` inspect safe endpoint metadata, redacted events and authenticated receipts.

## Signature contract

An endpoint has a random 256-bit opaque public identifier, one stored tenant/connection and fixed `hmac-sha256-v1`. It accepts JSON up to **32,768 raw bytes**, including streamed requests without a declared length. Content type must be `application/json` or `application/json; charset=utf-8`. Body/transport errors are rejected before JSON interpretation.

Headers:

- `X-Webhook-Timestamp`: decimal Unix seconds, no whitespace, sign or fraction; at most eleven digits.
- `X-Webhook-Signature`: `v1=` followed by 64 lowercase hexadecimal characters.

The digest is `HMAC-SHA256(signing_secret_bytes, b"v1." + timestamp_ascii + b"." + exact_raw_body)`. The generated signing secret is a URL-safe random string; its **literal UTF-8 bytes**, not base64-decoded bytes, are the HMAC key. Timestamp and raw body are both authenticated. No arbitrary algorithm/header version is accepted. Comparison uses `hmac.compare_digest`; all currently eligible versions are evaluated.

Timestamp tolerance is endpoint-configured between 30 and 300 seconds, default 300. Signed requests too far in the past **or future** are rejected. A modified timestamp needs a new signature. Successful retries can use the same signature during the freshness window or sign the same event with a new current timestamp later. There is no optional delivery nonce in this version. Durable provider-event identity prevents fresh replay creating another accepted event; a captured valid request within the window can produce a duplicate receipt. This is compatible with at-least-once delivery and does not claim exactly-once external delivery.

Tenant/source ownership comes from the stored endpoint and its authorized enabled CRM/ATS connection. Tenant headers are ignored and tenant fields in events are forbidden. Unknown or disabled endpoints and disabled connections receive generic authentication failure. Public IDs are not signing credentials. Signature and stale failures create no trusted tenant event or receipt; sanitized request logs contain fixed outcome categories and route templates, never the actual public identifier or headers. Signature-authenticated schema failures get a bounded receipt without raw payload or provider-supplied metadata.

## Encrypted secret storage

`WEBHOOK_ENCRYPTION_KEY` is a dedicated canonical URL-safe base64 encoding of **32 random bytes**. It is separate from database credentials and `CSRF_SECRET`. Cryptography's AES-256-GCM encrypts each random signing secret with a fresh 96-bit nonce. Authenticated associated data binds ciphertext to protocol version, tenant UUID, endpoint UUID and secret version. A copied/tampered ciphertext or wrong master key fails closed.

Encrypted versions are in `webhook_secret_versions`, outside public connection configuration. Ordinary read/detail/list/status APIs return no secrets, ciphertext or nonces. Creation/rotation returns a signing secret once, after the transaction commits, in a `Cache-Control: no-store` response. A failed commit never releases a success/disclosure. If the response is lost, rotate again; there is no recovery/read-secret API. UI disclosure is masked, held in an isolated transient ref/DOM input, cleared on close/unmount, and never stored in normal event/endpoint React state, localStorage, URLs or analytics. Browser inspection can still see an authorized one-time disclosure; this is not protection against a compromised browser or developer tools.

For the existing local checkout, from the root:

```sh
python3 scripts/setup_webhook_key.py
cd backend
.venv/bin/alembic upgrade head
```

The setup command appends a key only if absent to private mode-600 `backend/.env`; it prints no key and never replaces existing configuration. Fresh `scripts/setup_local.py` also generates a dedicated key. Restart FastAPI after changing environment configuration. A missing key leaves earlier phase functionality available but blocks creation, rotation, simulation and ingress with 503. Invalid supplied keys fail settings validation. Status reports a missing/wrong/decryption-failing key as unavailable.

**Back up the master key securely with the database.** Losing it makes existing signing secrets unusable. Replacing it does not re-encrypt records. Master-key IDs, dual-master-key reads, atomic re-encryption tooling, external secret management and automated credential retention are not implemented; do not rotate this key by casually rerunning setup. A future migration must decrypt with the old key and re-encrypt with the new key without exposing plaintext.

## Signing-secret rotation

Rotation locks the endpoint, verifies its current ciphertext and issues a new current version. The immediately previous version can verify for 0–900 seconds (default 300). Older versions are immediately retired even if their earlier grace period had not ended. Zero grace retires the old secret immediately. Expired encrypted rows are retained as historical version records, but cannot verify signatures. Rotation does not enable a disabled endpoint; disabling blocks every version. Concurrent ingress/rotation serialize on the endpoint row. These are local database transactions with no outbound provider calls.

## Explicit sender commands

Start FastAPI and Next.js using the README commands. Sign in, open Events and create an endpoint for an enabled CRM/ATS connection. Save its one-time signing secret privately and copy only the public ingress identifier below. Run from `backend`; the sender prompts for the secret with hidden input:

```sh
.venv/bin/python -m app.connectors.webhook_sender \
  --target http://127.0.0.1:8000/api/v1/webhooks/ingest/REPLACE_WITH_PUBLIC_ID \
  --source synthetic_crm --event-type job.created --event-id portfolio-job-001

.venv/bin/python -m app.connectors.webhook_sender \
  --target http://127.0.0.1:8000/api/v1/webhooks/ingest/REPLACE_WITH_PUBLIC_ID \
  --source synthetic_crm --event-type job.created --event-id portfolio-job-001 \
  --scenario duplicate
```

Use `--scenario invalid_signature`, `stale` or `altered_payload` with the same shape to verify rejection. For ATS use `--source synthetic_ats --event-type candidate.created` (or candidate.updated/job.created/job.updated). CRM supports job.created/job.updated. The target placeholder must be replaced by the actual 43-character identifier. No signing secret goes in command arguments. Noninteractive trusted operators may use `WEBHOOK_SIGNING_SECRET` in a private child-process environment; avoid shell history and shared process environments.

Only `http://127.0.0.1:8000` and `http://localhost:8000` with the exact ingress path are allowed. Localhost is normalized to numeric loopback, eliminating user-controlled DNS. Credentials, alternate ports, queries, fragments, redirects and external/private/metadata destinations are rejected. Transport ignores proxy environment variables, uses a five-second timeout and bounds responses to 8KiB. Importing or seeding simulators never sends requests; the CLI requires explicit invocation. The authorized UI button signs and calls the same verifier **in process**, explicitly labeled as such; it does not pretend to be real HTTP or send externally.

## Limits

This is a local portfolio implementation, not an independently audited public ingress service. No edge rate limiting, slow-body/read deadline at the application layer, per-provider quotas, proxy trust configuration, central signature counter backend, key custody service or automated receipt retention exists. Deployment requires HTTPS, body/connection/time limits and abuse controls at a trusted ingress proxy. Existing application logs provide categorized rejection evidence; endpoint counts include only actual authenticated persisted receipts. No external provider or OAuth credentials are used.
