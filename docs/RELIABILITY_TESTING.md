# Phase 11 reliability verification

`scripts/phase11_reliability.py` reuses the Phase 6 and Phase 8 HTTP verifiers against the isolated schema and existing BFF, API and worker. Both returned zero. Safe summary: [reliability evidence](evidence/phase11-reliability.json). Private local logs are retained under `backend/.local`.

- Phase 6 run `phase6-03c910dd62-`: bounded quota contention, scheduled retry/restart, circuit OPEN/half-open recovery, expired credential refresh, revoked credential terminal failure, duplicate action protection, alerts and tenant isolation.
- Phase 8 run `phase8-6581039102-`: signed CRM → ATS flow and transactional notifications; partial failure and targeted retry without repeating ATS writes; duplicate intent/effect protection; worker SIGKILL after effect and before checkpoint, rollback/lease recovery with one committed effect; unknown write held without resend; calendar overlap rejection and create/reschedule/cancel.
- A dedicated pooled connection was terminated using its own PostgreSQL backend PID. `pool_pre_ping` reconnected and SELECT 1 succeeded. The shared database/container was not stopped.
- The isolated API was restarted while retaining its migrated schema. No reset or volume deletion was used. The same retained browser session returned 200 before and after the final API restart, and persisted connections remained available.

The concurrency baseline exposed PostgreSQL 40P01 from inconsistent Tenant/queue lock ordering. Workflow dispatch/claim and notification claim now admit up to 32 due tenants in stable UUID order using `FOR UPDATE SKIP LOCKED`, before locking queue rows. A two-domain regression holds the Tenant lock while another connection attempts claim: the claim returns no work without blocking/mutating; it succeeds after release. Final focused queue/workflow/notification tests: 88 passed. The post-fix bounded baseline recorded zero errors; the original five-error artifact is retained.

Reproduce after fresh isolated fixtures and local servers:

```sh
backend/.venv/bin/python scripts/phase11_reliability.py
# from backend/
.venv/bin/pytest -q tests/test_queue_locking.py tests/test_workflows.py tests/test_notifications.py
```

This tests local transactional simulators, not remote exactly-once delivery. External effect ambiguity remains explicitly held for operator review. Busy tenants can be skipped; the 32-tenant admission batch and coarse tenant serialization require fairness/capacity review before horizontal production scaling. There was no uncontrolled stress, database corruption, host crash or real provider outage test.
