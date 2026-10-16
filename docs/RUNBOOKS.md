# Version-controlled runbooks

The initial catalog is static code in `backend/app/operations/runbooks.py`, version 1. There is no executable template, AI step, external link, runtime editing endpoint or automatic business action.

| ID | Symptom |
| --- | --- |
| webhook-rejected | Signed tenant-attributable webhook was rejected/conflicted |
| workflow-stuck | Workflow has not progressed beyond its due/lease threshold |
| connector-unavailable | Local connector health/reliability requires investigation |
| credential-expired | Masked credential/refresh metadata indicates a problem |
| rate-limit-exceeded | Known admission or Retry-After evidence |
| sync-conflict | Source/identity/authority/destination conflict |
| notification-failed | An individual synthetic notification action failed |
| unknown-outcome | No verified provider action result |
| worker-restart | Lease expired or existing worker recovered it |
| calendar-conflict | Explicit recruiting action conflicts with current calendar state |

Every runbook contains symptom, evidence to inspect, preconditions, safe recovery options, escalation conditions and verification steps. Advice is operational guidance; the recovery planner and existing owning service remain authoritative about action eligibility. A runbook never grants permission, overrides field authority or proves delivery.

The authenticated catalog/detail APIs are `GET /api/v1/operations/runbooks` and `/runbooks/{id}`. Incident/evidence plans return a matching runbook ID; the Operations UI provides a structured viewer. Unknown IDs return 404. The catalog is shared static instructional content, while incident/evidence/recovery data remains tenant scoped. Runbook changes are source changes and do not rewrite incident history.
