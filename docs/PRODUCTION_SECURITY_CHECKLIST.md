# Production security release gates

Phase 11 is a locally verified portfolio hardening pass, not production approval.

- [ ] Resolve the high-severity `braces` advisory in the compatible Next ESLint dependency chain; rerun the complete audit and lint suite. Do not force an incompatible framework downgrade.
- [ ] Scan the deployed PostgreSQL/base images and pin reviewed immutable digests; Phase 12 executed scans: both application images retain high/medium findings and PostgreSQL critical/high findings; see phase12-container-scans.json.
- [ ] Provide TLS, secure cookie domain, exact public Host/CORS/Origin configuration, trusted proxy policy, edge request/body/rate limits and centralized abuse monitoring. The in-process webhook limiter is insufficient across replicas.
- [ ] Use secret management, least-privilege application/migration database roles, rotation procedures and backup/restore drills. Evaluate RLS as defense in depth; current isolation relies on verified service predicates/constraints.
- [ ] Establish production log access/retention, redaction monitoring and security incident response. Never upload local session states, fixture secrets or unredacted traces.
- [ ] Review strict script CSP/nonces with Next; current CSP protects framing/base/object boundaries and does not claim complete XSS prevention.
- [ ] Validate real provider sandbox OAuth, destination allowlists, timeout/retry contracts and unknown-effect reconciliation before enabling any external connector. No real provider is enabled here.
- [ ] Reassess Tenant lock granularity, 32-tenant admission fairness, connection budgets, retention and query indexes under representative multi-tenant load; obtain CPU/RSS/IO observations and large enough samples for tail latency.
- [ ] Run GitHub Actions after an explicitly authorized push; workflows are configured but remote CI was not executed in this run. Pin third-party actions to reviewed SHAs for deployment workflows.
- [ ] Expand browser/a11y coverage beyond the initial overview, include assistive technology and real-device checks, and conduct an independent penetration assessment.
- [ ] Require a separately reviewed production change process. AI remains evidence-grounded advisory output; approval, authorization, evidence revision and confirmation stay in the existing recovery engine.

No compliance certification, production SLA, remote exactly-once guarantee, paid AI verification or production deployment is asserted.
