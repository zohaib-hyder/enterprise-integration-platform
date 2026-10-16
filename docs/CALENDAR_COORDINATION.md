# Synthetic calendar coordination

Calendar coordination is explicitly authorized recruiting work. A job sync does **not** schedule an interview. Admins/integration engineers queue create, reschedule and cancel requests through the authenticated API. Operations users inspect requests/events and use permitted delivery recovery controls. No real invite or attendance response is sent.

A create request requires a tenant-owned synthetic calendar connection, an allowlisted calendar, a bounded title, aware start/end timestamps, 1–20 distinct `example.test` participants, and a UUID request key. End must follow start. Participants are retained as request metadata; the existing Phase 2 event adapter has no participant/invitation transport. Requests queue the existing event.create/update/cancel operations through Phase 6 reliability and the same worker action queue as Slack/email.

Event IDs remain stable through reschedule/cancel. Every action has its own immutable request digest/idempotency key and durable attempts/receipt. Reusing a request UUID returns the original request, even after the event has changed, provided the full operation input matches; different content returns 409. Reschedule/cancel also require the inspected event's aware `expected_updated_at`. The worker checks that revision again after locking; a concurrent edit or earlier cancellation is a safe conflict.

Before write, tenant then connection locking serializes overlap checks and the local provider action. Intervals are half-open: adjacent events are permitted, overlapping BOOKED events in the same tenant/connection/calendar are rejected. Reschedule excludes its own stable event ID. Availability is a bounded snapshot of current events, not a reservation; the worker rechecks. The snapshot includes an event's current booking when inspecting availability for a reschedule. Pending requests are not displayed as confirmed events. The UI exposes durable action status and conflict feedback independently of booked event records.

Composite foreign keys bind requests to their tenant-owned connection, delivery and referenced provider event; the provider event FK also includes connection ID. Calendar history is paginated. An UNKNOWN create can resolve from matching provider idempotency/hash evidence. An UNKNOWN reschedule/cancel requires its orchestration receipt; event field equality alone is insufficient. Unresolved ambiguity never triggers a blind resend.

## APIs

- GET `/api/v1/calendar/events`, `/events/{id}`: retained tenant provider events.
- POST `/events`: queue an explicit interview create, returning coordination request (201).
- POST `/events/{id}/reschedule`, `/events/{id}/cancel`: queue reviewed version-guarded action (201).
- GET `/availability?connection_id=...&calendar=interviews&starts_at=...&ends_at=...`: aware interval conflict snapshot, maximum 100 returned conflicts.
- GET `/requests`, `/requests/{id}`, `/requests/{id}/history`: durable request, linked action status and attempt history.

In `/calendar`, datetime-local inputs use the device timezone and serialize UTC ISO timestamps. Backend schemas require timezone awareness; there is no guessed UTC offset. Daylight-saving conversion follows the device runtime; the portfolio UI does not provide a separate IANA timezone picker. Connection selectors are bounded to 100. Calendar conflicts generate the existing operational alert, preserving acknowledgement/resolution permissions. There is no automatic conflict resolution, booking optimizer, recurring event support, meeting room model, real provider calendar or invitation delivery.

See [NOTIFICATION_DELIVERY.md](NOTIFICATION_DELIVERY.md) for exact local commands and [PARTIAL_FAILURE_RECOVERY.md](PARTIAL_FAILURE_RECOVERY.md) for crash boundaries.
