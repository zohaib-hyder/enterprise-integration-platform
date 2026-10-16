# Bounded local performance baseline

The ARM64 macOS host was the user-reported M1 MacBook Air / 8GB RAM, Python 3.12.14, Node 24 and PostgreSQL 17 in the existing Docker service (1 CPU / 256 MB limits). Existing regression work could run concurrently. No CPU/RSS sampler was attached. These are short local observations, not production capacity or SLA claims.

Run `backend/.venv/bin/python scripts/phase11_performance.py` after isolated fixture creation and server startup. Do not run alongside browser delivery tests. Maximum concurrency was two, samples 5–30; p99 is deliberately null because each sample is below 100. Data and SQL plan: [final JSON](evidence/phase11-performance.json).

| Scenario | n / concurrency | p50 ms | p95 ms | Duration s | Errors | Completed units | Units/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| health | 30 / 2 | 20.791 | 32.861 | 0.3202 | 0 | 30 | 93.69 |
| authenticated_connections_bff | 20 / 2 | 38.304 | 86.628 | 0.502 | 0 | 20 | 39.838 |
| operations_overview_bff | 20 / 2 | 78.965 | 126.135 | 0.8633 | 0 | 20 | 23.166 |
| incident_listing_bff | 20 / 2 | 30.826 | 46.156 | 0.3574 | 0 | 20 | 55.962 |
| mock_ai_analysis_bff | 5 / 1 | 200.692 | 244.544 | 1.0427 | 0 | 5 | 4.795 |
| sync_plan_and_existing_route_apply | 5 / 2 | 140.045 | 611.87 | 0.8746 | 0 | 5 | 5.717 |
| notification_queue_processing | 8 / 1 | 155.379 | 273.938 | 1.3355 | 0 | 8 | 5.99 |
| event_dispatch | 5 / 1 | 2.464 | 7.124 | 0.0198 | 0 | 0 | 0.0 |
| workflow_claim_execute | 10 / 2 | 7.767 | 218.985 | 0.2192 | 0 | 1 | 4.562 |
| signed_webhook_ingestion | 20 / 2 | 35.63 | 151.817 | 0.6005 | 0 | 20 | 33.304 |

Event dispatch found no pending events: its zero completed units measure empty polling only. Workflow execution completed one claimed step across ten claim attempts; this is not an entire workflow or a saturated-backlog capacity result. Sync attempts reuse an existing route and can produce legitimate idempotent/held outcomes. Notification throughput counts processed delivery work. Signed webhook samples are actual HMAC HTTP submissions. All ten measured categories had zero errors; database behavior adds the eleventh category: the operations overview issued 22 SQL statements and an incident-list EXPLAIN plan is retained in the JSON.

The [initial baseline](evidence/phase11-performance-initial.json) exposed five errors in ten workflow attempts, confirmed as PostgreSQL 40P01 deadlocks in separate diagnosis. Tenant-before-queue lock ordering fixed the inversion; the final concurrency run had zero errors. UUID-ordered tenant admission and SKIP LOCKED avoid waiting on a busy tenant, with a bounded 32-tenant batch. This improves correctness but serializes work within a tenant; fairness and production throughput remain unmeasured. No statistically meaningful p99, memory saturation or production scalability conclusion is available.
