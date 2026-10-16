# Architecture diagrams

These describe the implemented local system. All provider effects shown are synthetic; optional external AI is disabled. PostgreSQL transactions do not extend across a future remote provider.

## System

[Mermaid source](diagrams/system.mmd)

```mermaid
flowchart LR
  User[Operator browser] --> Next[Next.js App Router]
  Next --> BFF[Allowlisted same-origin BFF]
  BFF --> API[FastAPI v1 / session / RBAC]
  Sender[Synthetic signed sender] --> Hook[HMAC webhook ingress]
  Hook --> API
  API --> PG[(PostgreSQL: tenant data and queues)]
  Worker[Existing Python worker] <--> PG
  Worker --> Sim[CRM / ATS / Slack / Email / Calendar simulators]
  Sim --> PG
  API --> AI[Evidence builder and schema validators]
  AI --> Mock[Deterministic mock provider by default]
  AI -. disabled optional transport .-> External[OpenAI: unverified externally]
```

## Webhook To Ats

[Mermaid source](diagrams/webhook-to-ats.mmd)

```mermaid
sequenceDiagram
  participant S as Synthetic CRM sender
  participant A as FastAPI webhook
  participant P as PostgreSQL
  participant W as Existing worker
  S->>A: Timestamped HMAC event
  A->>A: Size / signature / replay-window validation
  A->>P: Tenant endpoint + durable inbox uniqueness + receipt
  A-->>S: 202 accepted (200 duplicate)
  W->>P: Dispatch accepted event to pinned workflow version
  W->>P: PLAN_CRM_ATS_SYNC / immutable mapping
  W->>P: APPLY_CRM_ATS_SYNC / authority and revision checks
  alt Destination independently changed
    W->>P: Persist conflict / hold without overwrite
  else Safe local synthetic upsert
    W->>P: ATS effect + identity + sync receipt + outbox in one transaction
  end
  W->>P: RECORD_SYNC_OUTCOME / checkpoint
```

## Durable Worker

[Mermaid source](diagrams/durable-worker.mmd)

```mermaid
flowchart TD
  Inbox[Accepted durable inbox] --> Dispatch[Bounded dispatcher / unique dispatch receipt]
  Dispatch --> Plans[Execution and steps pin immutable version]
  Plans --> Lock[Tenant-first locks / SKIP LOCKED admission]
  Lock --> Claim[Claim token / worker lease / fencing]
  Claim --> Step[One bounded step]
  Step --> Commit[Local effect + receipt + checkpoint transaction]
  Commit --> Next{More work?}
  Next -->|yes| Plans
  Next -->|no| Done[Terminal outcome]
  Claim --> Expiry[Expired lease: new fenced claim]
  Expiry --> Receipt[Inspect existing receipt / attempt budget]
  Receipt --> Plans
  Receipt --> Hold[Unknown outcome held for review]
  Commit --> Companion[One notification or calendar action slot]
```

## Retry State Machine

[Mermaid source](diagrams/retry-state-machine.mmd)

```mermaid
stateDiagram-v2
  [*] --> PENDING
  PENDING --> RUNNING: fenced claim
  RUNNING --> PENDING: next checkpoint
  RUNNING --> WAITING: durable delay
  WAITING --> RUNNING: due
  RUNNING --> RETRY_SCHEDULED: classified retryable failure
  RETRY_SCHEDULED --> RUNNING: persisted due time and budget
  RUNNING --> SUCCEEDED
  RUNNING --> FAILED: terminal failure
  RUNNING --> DEAD_LETTERED: exhausted / unrecoverable
  PENDING --> CANCELLED: authorized cancellation
  WAITING --> CANCELLED
  RETRY_SCHEDULED --> CANCELLED
  RUNNING --> CANCELLED: cooperative cancellation
  RUNNING --> PENDING: expired lease with new fence
  note right of DEAD_LETTERED
    Authorized replay creates a NEW execution.
    Terminal records are not reset.
  end note
```

## Notification Outbox

[Mermaid source](diagrams/notification-outbox.mmd)

```mermaid
flowchart LR
  subgraph TX[Single PostgreSQL step transaction for local simulator]
    Sync[ATS effect / sync state] --> Receipt[Operation receipt]
    Receipt --> Intent[Unique notification intent / pinned template]
    Intent --> Rows[Independent SLACK and EMAIL delivery rows]
  end
  Rows --> Worker[Existing worker companion slot]
  Worker --> Attempt[One bounded connector attempt]
  Attempt --> OK[SUCCEEDED + synthetic receipt]
  Attempt --> Retry[RETRY_SCHEDULED / persisted due / budget]
  Retry --> Worker
  Attempt --> Unknown[UNKNOWN: hold / no automatic resend]
  Attempt --> Failed[FAILED: terminal review]
  Unknown --> Review[Authorized receipt review or cancellation]
  Failed --> Ops[Operations recovery planner]
  OK --> Boundary[Delivery outcome does not roll back ATS success]
```

## Incident Recovery

[Mermaid source](diagrams/incident-recovery.mmd)

```mermaid
flowchart TD
  Evidence[Persisted failures / alerts / consistency findings] --> Refresh[Explicit incident refresh]
  Refresh --> Incident[Correlated incident and timeline]
  Incident --> Preview[Read-only recovery preview / evidence revision]
  Preview --> Request[Authorized request with reason and request key]
  Request --> Risk{Independent approval required?}
  Risk -->|yes| Approver[Different authorized reviewer]
  Risk -->|no| Confirm[Explicit confirmation of exact target and action]
  Approver --> Confirm
  Confirm --> Recheck[Recheck tenant / role / revision / eligibility]
  Recheck --> Existing[Existing retry / replay / receipt-review service]
  Recheck --> Reject[Reject stale / forbidden / ineligible request]
  Existing --> Audit[Durable recovery history / audit]
```

## Ai Human Review

[Mermaid source](diagrams/ai-human-review.mmd)

```mermaid
flowchart LR
  User[Authorized operator request] --> Evidence[Bounded typed evidence snapshot]
  Evidence --> Mock[Deterministic mock by default]
  Mock --> Validate[Strict schema / citation / grounding validation]
  Validate --> Store[Persist analysis / evidence / recommendations]
  Store --> Reviewer[Human inspects authorized citations]
  Reviewer --> Decision[Accept / reject / request changes with version check]
  Decision --> Preview[Accept yields existing recovery PREVIEW only]
  Preview --> Governance[Separate request / approval / explicit confirmation]
  Governance --> Existing[Existing deterministic recovery service]
  Store --> Prohibit[AI has no execution tool or autonomous write path]
```

## Multi Tenant Boundary

[Mermaid source](diagrams/multi-tenant-boundary.mmd)

```mermaid
flowchart TD
  Browser[Opaque HttpOnly session cookie] --> Session[Hashed server-side session / active tenant]
  Session --> Membership[Active membership and route permission]
  Membership --> Scope[Tenant-scoped service queries]
  Scope --> FK[Composite tenant ownership foreign keys / uniqueness]
  FK --> PG[(Shared PostgreSQL / logical isolation)]
  Hook[Public webhook identifier + verified HMAC] --> Endpoint[Stored endpoint fixes tenant and connection]
  Endpoint --> Scope
  Worker[Worker claim] --> Stored[Stored tenant / configured principal rechecked]
  Stored --> Scope
  PG --> A[Tenant A records]
  PG --> B[Tenant B records]
  Scope --> Deny[Cross-tenant IDs denied / no evidence leakage]
  PG --- Limit[No PostgreSQL row-level-security claim]
```

Implementation references: [workflow engine](WORKFLOW_ENGINE.md), [synchronization](CRM_ATS_SYNCHRONIZATION.md), [notification delivery](NOTIFICATION_DELIVERY.md), [recovery governance](CONTROLLED_RECOVERY.md), [AI safety](AI_RECOMMENDATION_SAFETY.md), [threat model](THREAT_MODEL.md).
