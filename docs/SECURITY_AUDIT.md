# Phase 11 security and reliability audit

Audit date: 2026-10-15. Review covered session/auth dependencies, tenant-scoped service queries, BFF allowlisting, HMAC inbox, credential storage/refresh, durable workflow/outbox, synchronization authority/conflicts, calendar, recovery approvals, AI evidence/output, logs, configuration, migrations, Docker and dependencies. Existing controls were tested rather than replaced. Findings below distinguish confirmed failures, defense-in-depth gaps and package advisories; test synchronization mistakes are not product vulnerabilities.

## H11-01 — High reliability

- Component: Workflow dispatch/claim, notification claim
- Reproduction: Run bounded parallel workflow claims with concurrent execution; initial baseline produced 5/10 errors, diagnosis returned PostgreSQL 40P01.
- Root cause: Queue-first locks conflicted with Tenant-first execution/FK locks.
- Impact: Deadlocked work rolls back and delays processing.
- Remediation: Acquire due Tenant rows in UUID order with SKIP LOCKED before queue rows.
- Verification: Two lock-holding regression cases; 88 focused tests; final concurrency baseline zero errors.
- Residual risk: 32-tenant admission/fairness and per-tenant serialization need production load evaluation.

## H11-02 — Medium

- Component: Direct FastAPI mutations
- Reproduction: Submit >16 KiB Content-Length or chunked JSON directly, bypassing the existing BFF limit.
- Root cause: No global direct-API pre-decode bound.
- Impact: Unnecessary memory/JSON decoding work.
- Remediation: ASGI cap before accumulation/decode; reject compressed direct mutation bodies. Existing webhook typed bounded decoder is preserved.
- Verification: test_hardening content-length/chunked/compression tests.
- Residual risk: Edge limits and request timeouts remain deployment requirements.

## H11-03 — Medium

- Component: Host validation
- Reproduction: Send an arbitrary Host to the direct API.
- Root cause: No explicit trusted Host middleware.
- Impact: Untrusted host values accepted by the application boundary.
- Remediation: Explicit validated ALLOWED_HOSTS, restrictive local defaults, TrustedHostMiddleware.
- Verification: Unsafe Host config matrix and hostile Host 400 tests.
- Residual risk: Configure exact public DNS and proxy policy before deployment.

## H11-04 — Medium

- Component: Webhook ingress
- Reproduction: Repeated ingest requests previously reached signature/service work without an ingress quota.
- Root cause: No bounded peer ingress guard.
- Impact: Avoidable abuse cost before domain protections.
- Remediation: Bounded 4096-bucket hashed-peer, atomic per-process minute quota, before body/service work.
- Verification: ASGI threshold test verifies third request 429 without downstream call.
- Residual risk: Not distributed; peer accuracy behind proxies needs edge configuration. Fixed window bursts remain possible.

## H11-05 — Medium defense in depth

- Component: Browser/API response headers
- Reproduction: Inspect responses for framing, MIME and referrer protections.
- Root cause: Explicit security response policy was absent.
- Impact: Reduced browser defense in depth. No exploitable XSS was demonstrated.
- Remediation: DENY framing, nosniff, referrer/permissions restrictions, CSP framing/base/object boundaries.
- Verification: API header regression, frontend production build/browser checks.
- Residual risk: No strict nonce/script CSP or complete XSS prevention claim.

## H11-06 — Medium usability

- Component: Overview/sidebar/account controls
- Reproduction: At representative viewports, tab through the page and run axe. Three contrast nodes had 4.44 or 2.45 contrast ratios; a landmark was missing and the skip link followed account controls.
- Root cause: Low-contrast text, missing banner landmark, skip placement and fixed sidebar overflow.
- Impact: Keyboard and low-vision users face avoidable navigation barriers.
- Remediation: Move skip link before account controls, add main focus target/banner, darken overview text, enable desktop sidebar scrolling.
- Verification: Three browser viewport/keyboard/axe scenarios.
- Residual risk: Automated overview audit is not whole-product WCAG certification.

## DEP11-01 — Critical dependency (development)

- Component: Vitest/tinypool dependency chain
- Reproduction: Initial npm audit reported critical advisories under Vitest 3.2.7.
- Root cause: Outdated test tooling dependencies.
- Impact: Development/CI exposure; no production exploit demonstrated.
- Remediation: Targeted Vitest 5.0.5, compatible config, Node >=22.12.0; retain framework lint.
- Verification: 157 unit tests pass; final audit has zero critical entries.
- Residual risk: Continue dependency monitoring.

## DEP11-02 — High dependency

- Component: cryptography 47.0.0
- Reproduction: pip-audit against the actual requirements lock returned seven records, four distinct advisory IDs.
- Root cause: Affected library release.
- Impact: Affected X509/PKCS7 APIs are not used by application AES-GCM, but installed dependency risk remained.
- Remediation: Targeted ARM64 cryptography 50.0.2; no ciphertext/key format change.
- Verification: 124 credential/webhook/reliability tests pass; final Python audit reports no known vulnerabilities.
- Residual risk: Library audit is not a proof of absence of vulnerabilities.

## DEP11-03 — High dependency — OPEN

- Component: Next ESLint development chain
- Reproduction: Final npm audit reports five high affected packages: eslint-config-next, @next/eslint-plugin-next, fast-glob, micromatch, braces; underlying GHSA-vfj7-8cjw-p6xm.
- Root cause: Compatible tooling still includes affected braces; npm proposes a framework lint downgrade to 14.2.35.
- Impact: Development glob-processing exposure; no runtime exploit demonstrated.
- Remediation: Do not force an incompatible downgrade; retain audit evidence and explicit CI development warning. Production-only audit is a separate gate.
- Verification: frontend/.local-phase11-npm-audit-final.json; rerun npm audit and framework lint after upstream fix.
- Residual risk: Unresolved high finding blocks an unconditional production security approval.

## Controls retained and adversarial scope

The full PostgreSQL-backed regression suite retains tests for password hashing/session fixation, cookie attributes, CSRF/origin, expiration/revocation/deactivation, live membership/RBAC, guessed IDs and cross-tenant nested references. Additional Phase 11 tests probe 24 cross-tenant read paths and expired/revoked/deactivated access across major domains. Existing write/retry/replay/resolve/approval tests exercise backend authorization; not every possible method/resource permutation is formally proven.

Webhook regressions cover bad/missing HMAC, mutation, old/future timestamp, replay/duplicate/conflicting ID, oversized/malformed input, content type, disabled endpoint, secret rotation and concurrent duplicate ingestion. Rejected inputs do not enter downstream business execution. Connector configuration is typed and synthetic; no arbitrary outbound URL transport or real OAuth was added. Unknown provider write outcomes stay held. Fencing, action receipts, source versions, authority, stale conflict/recovery revisions and independent approval remain enforced. AI uses authorized typed evidence and human review; accepting a recommendation creates only an existing recovery preview.

## Dependency decisions

[Vitest migration guidance](https://main.vitest.dev/guide/migration/) informed the Node/tooling compatibility change. [cryptography 50.0.2 release notes](https://cryptography.io/en/50.0.2/changelog/) informed the targeted library update. No blanket forced npm upgrade or incompatible framework downgrade was used. PostgreSQL image CVEs were not established by application dependency audits. See PRODUCTION_SECURITY_CHECKLIST.md for remaining release gates.


## Phase 12 follow-up (2026-10-15–16)

Production now fails closed on debug, missing/invalid/shared encryption keys, missing persistent CSRF secret, invalid DB, unsafe/missing trusted hosts and non-HTTPS origins. Fresh setup generates independent keys/secret. Next/ESLint 16.4.1 still carries the unfixed braces chain; the full audit remains visible. Actual Docker Scout scans and supported base/runtime-tooling remediation reduced both application images to zero critical, one high, one medium and 26 low findings each. Unfixed high zlib and medium tar remain. PostgreSQL has four critical/31 high/27 medium/10 low findings in the reported image. Do not infer image safety from pip/npm audits. Exact identities/advisories are in evidence/phase12-container-scans.json and RELEASE_READINESS.md. Production release remains blocked by those findings and deployment gates.
