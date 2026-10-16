# Phase 11 threat model

The fictional staffing operator handles synthetic candidate/contact records across local CRM, ATS, Slack, email and calendar simulators. No real provider endpoints or paid inference are used in this verification.

| Boundary | Untrusted input / threat | Existing control and Phase 11 verification |
|---|---|---|
| Browser → Next BFF | forged origin, CSRF, arbitrary upstream route, stolen session | Explicit route allowlist, mutation origin/CSRF, HttpOnly session; login/revocation and role browser tests |
| BFF/direct caller → FastAPI | spoofed tenant IDs, guessed/nested UUIDs, oversized JSON, hostile Host | Backend membership/RBAC and scoped queries; 24 additional UUID probes; explicit Host allowlist and 16 KiB mutation cap |
| Webhook → durable inbox | modified/replayed signatures, duplicates, memory/CPU abuse | Timestamped HMAC, durable uniqueness, original bounded streaming decoder; bounded pre-body per-peer ingress quota |
| Workers → PostgreSQL | competing claims, stale leases, lock inversion, partial checkpoint | Fencing/idempotency/action receipts, Tenant-before-queue locking, SIGKILL/restart verification |
| Connector → provider | credentials, unsafe destinations, uncertain write outcome | Synthetic connector registry; encrypted secrets and serialized refresh; uncertain outcomes held for review, no automatic resend |
| Operator → recovery | cross-tenant target, stale evidence, self approval | Existing planner, revisions, independent approval and explicit confirmation; no parallel recovery engine |
| Evidence → AI → operator | prompt injection, invented evidence, unsafe recommendation | Authorized retrieval, bounded typed evidence/output, citations and human review; mock-only execution, 60-case evaluation |
| Dependencies / CI | malicious packages, vulnerable tooling, leaked artifacts | Lockfiles, audits, isolated test PostgreSQL, restricted CI permissions; unresolved development advisory disclosed |

Trust is not granted by UUID possession or frontend visibility. Tenant isolation is enforced by service queries and composite constraints, not PostgreSQL RLS. Database administrators and host operators remain privileged. The local peer limiter is per process and does not provide distributed DDoS protection. Remote exactly-once delivery, production certification, full penetration coverage and formal noninterference are not claimed.
