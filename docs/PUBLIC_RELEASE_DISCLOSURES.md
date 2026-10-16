# Public release disclosures

Updated 2026-10-16. The source is publicly published under MIT at [zohaib-hyder/enterprise-integration-platform](https://github.com/zohaib-hyder/enterprise-integration-platform). Actual remote Actions results are recorded in the [Phase 13B report](PHASE_13B_REPORT.md). **No production deployment or production security certification has occurred.** The project is a fictional staffing-company implementation; screenshots and demonstrations use synthetic organizations, records and reserved `example.test` recipients.

## Implemented and simulated boundaries

FastAPI, Next.js, PostgreSQL migrations, tenant authorization, signed webhook verification, durable workers, canonical transformation, CRM-to-ATS synchronization, conflict holds, retry/outbox history, incidents and human review are working local code. CRM, ATS, Slack, email and calendar are PostgreSQL-backed simulators. Delivery success proves a local simulated effect and receipt, not contact with a real person. Calendar operations send no invitation. No real OAuth, provider-account onboarding or external provider delivery has been verified.

AI defaults to a deterministic mock. The optional OpenAI transport is disabled in all published evidence. The 60/60 evaluation result measures mock contract/grounding cases, not real-model accuracy, safety certification or production reliability. Human acceptance yields a recovery preview; execution still requires the existing deterministic request/approval/confirmation flow. No autonomous AI remediation is implemented.

## Delivery and transaction limits

Durable uniqueness, fenced claims, attempt budgets and receipts suppress duplicate local effects. Synthetic ATS effect, identity association, synchronization receipt/state and notification outbox commit together in PostgreSQL. Notification channels run independently afterward. A future remote write cannot share that transaction: unknown outcomes are held for receipt review rather than blindly resent. **No end-to-end exactly-once delivery guarantee is claimed.** Provider-specific idempotency, reconciliation, quotas, OAuth and ambiguous outcome handling require transport-specific validation.

Tenant isolation uses verified membership, route permissions, scoped service predicates and composite ownership constraints. It is shared-database logical isolation, not PostgreSQL row-level security or separate tenant databases. Local rehearsals use test mode and HTTP; they do not verify production HTTPS/Secure-cookie behavior or least-privilege database-role separation.

## Verification scope and unresolved findings

The [Phase 12 record](evidence/phase12-verification.json), recorded 2026-10-15 UTC, reports 653 backend tests, 157 frontend tests, 28 browser tests and 60 mock evaluation cases passing, plus build/lint/type checks. Those are local results, not a remote CI badge. Phase 13A does not rerun the approximately 34-minute backend suite because application business source is unchanged. Its separately dated preparation evidence records what was actually rerun. One TestClient deprecation warning remains. Overview axe checks cover three viewport sizes; they do not certify the whole application accessible.

Phase 13B additionally verified [hosted run 38903952434](https://github.com/zohaib-hyder/enterprise-integration-platform/actions/runs/38903952434) on code revision `d8347254da867f2cde89fc915dd010758884d2e0`: all three jobs succeeded, with 653 backend / 157 frontend / 28 browser tests and 60 mock cases. Earlier real CI failures and fixture/configuration corrections are retained in the publication report. The documentation-only evidence commit skips duplicate CI; it does not change the verified application, tests or workflow. Hosted success is not a production certification.

Dependency/image results are dated snapshots, not current guarantees. Phase 12 Python and production npm audits reported zero known findings. The complete npm tree reported **five high development-tooling findings** through the ESLint/fast-glob/micromatch/braces chain. No unsafe override, forced downgrade or suppression was used. Docker Scout 1.24.0 reported:

| Pinned image | Critical | High | Medium | Low |
| --- | ---: | ---: | ---: | ---: |
| API / worker | 0 | 1 | 1 | 26 |
| Frontend | 0 | 1 | 1 | 26 |
| PostgreSQL | 4 | 31 | 27 | 10 |

The application images include an unresolved zlib high advisory and tar medium advisory; PostgreSQL includes Go/gosu and OS findings. See the [exact image IDs, advisories and fixes](evidence/phase12-container-scans.json) and [release gates](RELEASE_READINESS.md). **Production release remains blocked.** Public source review is a separate decision and must retain these disclosures.

Bounded laptop performance measurements are diagnostic baselines, not load capacity, SLA or production throughput. Empty dispatch and a single claimed step limit worker conclusions. Idle-worker restart and local outage tests do not prove every after-effect crash cut. Backup/restore verification covers the 82 public application tables and a retained isolated restore; it is not a managed backup policy or disaster recovery certification.

## Publication status

Phase 13B authorizes public source publication under `zohaib-hyder` with the [MIT license](../LICENSE), copyright (c) 2026 Zohaib Hyder. The holder/year/account were explicitly confirmed by the owner. Actual publication and remote CI status are recorded in the [Phase 13B report](PHASE_13B_REPORT.md). No real customer assets or data are intentionally included; secret scanning and screenshot review reduce disclosure risk but cannot certify absence of every possible secret. Private env files, credentials, delivery tokens, session state, dumps, raw traces/scans and local caches remain ignored. Re-review the actual staged files and scan history before any approved push. Follow [the publication checklist](PUBLICATION_CHECKLIST.md); publication approval does not authorize deployment or paid/external calls.
