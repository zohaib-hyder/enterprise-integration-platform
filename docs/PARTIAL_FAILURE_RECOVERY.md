# Partial failure and recovery

An ATS synchronization and each notification are distinct business outcomes. The sync transaction creates all matching intents atomically. Subsequent Slack and email actions are independent durable queue entries. Failure of either channel changes only its delivery; ATS success, its immutable receipt and the successful other channel remain intact. There is no notification path back into `synchronization.apply`.

| Failure window | Actual local behavior | Recovery target |
| --- | --- | --- |
| Before sync commit | ATS, identity, sync receipt and outbox all roll back | Existing workflow step retry |
| After sync commit, before action claim | Intent and frozen payload remain PENDING | Existing worker claims delivery |
| After claim commit, before provider action | RUNNING claim persists; no provider effect | Expired lease, new fenced token |
| After synthetic provider write, before receipt/checkpoint commit | One PostgreSQL transaction rolls back both on connection loss | Lease recovery, provider lookup, one new committed effect |
| After delivery commit, before process observes commit | Provider, receipt and SUCCEEDED are committed | Queue does not claim completed delivery |
| Known 429/503/reliability failure | Admission/fault/observation and retry timing commit; provider savepoint rolled back | Only failed delivery retries |
| Repeated known retry failure | Attempt budget exhausts; FAILED plus deduplicated alert | Operator investigates configuration/provider health |
| Unconfirmed timeout or receipt digest mismatch | UNKNOWN and alert; no auto retry | Authenticated receipt review or stop local delivery |
| Calendar interval/revision conflict | FAILED with calendar conflict alert; event untouched | Inspect current state, authorize a new request |
| Authorized cancellation during scheduled retry | CANCELLED under tenant/action locking | No further claim; successful other actions preserved |

The verification script's crash subprocess temporarily wraps the production completion function to pause after the adapter action, then SIGKILLs that process. It verifies the uncommitted provider write is absent, waits for the ordinary 15-second lease to expire, restarts the same worker and observes one committed delivery and the recovered attempt. This pause exists only in the demo subprocess, not in the product worker. Unit/integration tests also kill the transaction with a BaseException after the real provider action and exercise lease token fencing, races and idempotency.

Synthetic providers support database idempotency and request hashes. NotificationReceipt is the orchestration action receipt; existing Phase 5 WorkflowActionReceipt remains responsible for workflow actions, and Phase 7 SyncOperation remains the ATS sync receipt. None is duplicated or repurposed to imply success in another domain. Existing alerts are reused, rather than a second alert system.

Real external transports will have a separate provider commit that cannot participate in PostgreSQL atomicity. Their adapter contract must distinguish known rejection, confirmed success, and ambiguity; provider-supported idempotency/receipt lookup or explicit human review is required. This implementation claims local transaction safety, not future exactly-once remote delivery. Terminal notifications are not automatically redelivered by replaying the original ATS workflow. A new manual notification is a separately authorized business action, not repair of an uncertain earlier delivery.

Run the repeatable HTTP demo with the commands in [NOTIFICATION_DELIVERY.md](NOTIFICATION_DELIVERY.md). It exercises signed CREATE/UPDATE, Slack transient recovery, email-only partial retry, duplicate ingress, actual SIGKILL, unknown review, calendar operations/conflicts and tenant denial. Test data is retained; no volume reset or application cleanup is performed.
