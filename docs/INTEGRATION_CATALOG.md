# Local integration catalog

Every active member can read `/api/v1/connector-definitions`. Only administrators and integration engineers can manage connections or execute/inspect synthetic data. The Connections UI provides matching forms, payload examples, status and history. Use the same-origin BFF `/api/platform/*`, with existing login cookies, Origin and CSRF verification for all mutations. Tenant scope comes from server-verified active membership.

| Type | Public config | Supported operations |
| --- | --- | --- |
| synthetic_crm | `workspace_label` | contact.list/get/upsert; requisition.list/get/upsert; record.search |
| synthetic_ats | `workspace_label` | candidate.list/get/upsert; requisition.list/get/upsert; record.search |
| synthetic_slack | `allowed_channels`: recruiting, operations, alerts | message.send |
| synthetic_email | `allowed_recipients`: reserved example.test addresses | message.send |
| synthetic_calendar | `allowed_calendars`: interviews, recruiting | event.list/create/update/cancel |
| generic_rest | `mode: disabled` | None; no DNS/HTTP transport, cannot enable |

All configs accept only `schema_version: 1` plus the fields listed. No tokens, passwords, arbitrary channels, provider headers, import paths or outbound URLs. Email supports exact recipient allowlisting with at most twenty reserved synthetic addresses. Config updates reset local health to unknown. Disabled connections cannot execute; disabled health reports `DISABLED`.

## Try each simulator

Run `cd backend && .venv/bin/python -m app.seed_demo` after `alembic upgrade head`. Select Cedar Ridge Recruiting or Great Lakes Talent after sign-in. Each organization has CRM, ATS, Slack, email and calendar connections. Generic REST is catalogued but not seeded. The UI operation selector generates examples; replace ID placeholders with UUIDs from the record browser. Requests below are payloads inside the version-1 operation envelope.

CRM: execute `contact.list` with `{"offset":0,"limit":25}`; `contact.get` with `{"id":"<record UUID>"}`; `contact.upsert` with:

```json
{"external_id":"portfolio-contact-1","data":{"name":"Synthetic Buyer","email":"buyer@example.test","company":"Fictional Harbor Company"}}
```

ATS: execute `candidate.list`, then `candidate.upsert`:

```json
{"external_id":"portfolio-candidate-1","data":{"name":"Synthetic Candidate","email":"candidate@example.test","stage":"SCREENING","skills":["Python"]}}
```

Both: `requisition.upsert` uses an external ID and data with `title`, `department`, `location`, `status` (`OPEN` or `CLOSED`). `requisition.list/get` use the usual pagination/UUID shape. `record.search` uses `{"kind":"contact","external_id":"demo-contact-1"}` for CRM, or candidate/demo-candidate-1 for ATS; requisition is supported in both. Wrong-kind searches fail validation. External-ID search returns one record or 404.

Slack: execute `message.send` twice:

```json
{"channel":"recruiting","text":"Synthetic interview coordination","idempotency_key":"portfolio-slack-1"}
```

Email: execute `message.send` twice:

```json
{"recipient":"recruiter@example.test","subject":"Synthetic interview","body":"Local simulated delivery only","idempotency_key":"portfolio-email-1"}
```

Identical retries reuse the ID; a changed message under the same key conflicts. History remains in PostgreSQL with `SIMULATED` status. No Slack API or real email is sent. Subjects must be nonblank and single-line. Destinations must match configured allowlists.

Calendar: execute `event.create`:

```json
{"calendar":"interviews","title":"Synthetic interview","starts_at":"2030-01-16T15:00:00Z","ends_at":"2030-01-16T16:00:00Z","idempotency_key":"portfolio-event-1"}
```

Repeat to verify the same ID. Another key with overlapping times returns 409. `event.update` uses the same fields plus `id` instead of the key. `event.cancel` uses `{"id":"<event UUID>"}`. `event.list` returns booked and cancelled events, with standard pagination. These are local bookings with no calendar provider or attendees.

## Boundaries

Simulators are educational portfolio fixtures but actual running code and durable storage. CRM/ATS do not implement every vendor feature, deletes, contacts-to-candidates conversion, placements or synchronization. Notifications have no delivery receipts, templates, threads or attachments. Calendar overlap is scoped to one connection/calendar, not shared resources across providers. No webhook ingestion, event bus, background execution, retry policy, reconciliation, AI, OAuth or provider secrets exist. Health describes local configuration/database access, never a real provider account. Generic REST limitations are visible in capabilities and UI.
