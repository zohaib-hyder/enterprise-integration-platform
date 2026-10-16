# ruff: noqa: F811
"""Phase 8 pure contracts plus real PostgreSQL outbox, queue and calendar recovery."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_identity import login, mutation
from test_synchronization import apply_plan, context, make_plan
from test_synchronization import setup as sync_setup
from test_workflows import fixture_ids, workflow_engine  # noqa: F401

from app.core.security import utcnow
from app.models.integration import (
    SyntheticCalendarEvent,
    SyntheticEmail,
    SyntheticNotification,
)
from app.models.notification import (
    CalendarCoordinationRequest,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationIntent,
    NotificationReceipt,
    NotificationRule,
    NotificationTemplateVersion,
)
from app.models.reliability import OperationalAlert
from app.models.synchronization import SyncExecution, SyncOperation, SyncPlan
from app.models.tenant import Tenant
from app.schemas.integration import CreateConnection
from app.schemas.notification import (
    CalendarCancel,
    CalendarChange,
    CalendarCreate,
    ManualNotification,
    RenderInputs,
    RuleCreate,
    RuleUpdate,
    TemplateCreate,
    TemplateVersionInput,
)
from app.schemas.reliability import FailureConfig
from app.services import integrations, reliability
from app.services import notifications as service
from app.workflows import engine as worker


@pytest.mark.parametrize(
    "body",
    [
        "{password}",
        "{job_title.__class__}",
        "{{job_title}}",
        "https://example.test",
        "<script>bad</script>",
        "{job_title",
        "\x00unsafe",
    ],
)
def test_template_rejects_unsafe(body):
    with pytest.raises(ValidationError):
        TemplateVersionInput(body=body)
    if body == "https://example.test":
        assert RenderInputs(job_title=service.safe_value("Data: Engineer", 160))
        assert RenderInputs(job_title=service.safe_value("password:", 160))
        for url in ["ftp://example.test", "mailto:someone@example.test", "javascript:alert(1)"]:
            with pytest.raises(ValidationError):
                TemplateVersionInput(body=url)


@pytest.mark.parametrize("value", ["password=synthetic", "https://external.test", "line\nbreak"])
def test_render_inputs_reject_sensitive(value):
    with pytest.raises(ValidationError):
        RenderInputs(job_title=value)
    assert RenderInputs(job_title=service.safe_value(value, 160))


@pytest.mark.parametrize(
    "participants", [["real@customer.com"], ["a@example.test", "a@example.test"], []]
)
def test_calendar_participants_restricted(participants):
    with pytest.raises(ValidationError):
        CalendarCreate(
            connection_id=uuid.uuid4(),
            calendar="interviews",
            title="Test",
            starts_at=utcnow(),
            ends_at=utcnow() + timedelta(hours=1),
            participants=participants,
            request_key=uuid.uuid4(),
        )


def configured(engine, channel="SLACK", trigger="MANUAL"):
    with Session(engine) as db, db.begin():
        ctx = context(db)
        conn = integrations.create_connection(
            db,
            ctx,
            CreateConnection(
                name="Phase8-" + uuid.uuid4().hex,
                connector_type="synthetic_slack" if channel == "SLACK" else "synthetic_email",
            ),
        )
        template = service.new_template(
            db,
            ctx,
            TemplateCreate(
                name="Notify",
                channel=channel,
                subject="Job {job_title}" if channel == "EMAIL" else "",
                body="{job_title} · {sync_outcome} · {correlation_id}",
            ),
        )
        version = db.scalar(
            select(NotificationTemplateVersion).where(
                NotificationTemplateVersion.template_id == template.id
            )
        )
        service.activate_template(db, ctx, template.id, version.id)
        rule = service.new_rule(
            db,
            ctx,
            RuleCreate(
                name=channel + " rule",
                trigger=trigger,
                connection_id=conn.id,
                template_version_id=version.id,
                destination=conn.public_config[
                    "allowed_channels" if channel == "SLACK" else "allowed_recipients"
                ][0],
                enabled=True,
            ),
        )
        return rule.id, version.id, conn.id, worker.register_worker(db)


def manual(engine, ids, key=None, inputs=None):
    with Session(engine) as db, db.begin():
        intent = service.manual(
            db,
            context(db),
            ManualNotification(
                rule_id=ids[0], request_key=key or uuid.uuid4(), inputs=inputs or RenderInputs()
            ),
            uuid.uuid4(),
        )
        delivery = db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.intent_id == intent.id)
        )
        return delivery.id


def tick(engine, worker_id):
    with Session(engine) as db, db.begin():
        value = service.claim(db, worker_id)
    if value:
        service.run_claim(engine, value)
    return value


def fault(engine, connection_id, scenario, attempts=1, retry_after=None):
    with Session(engine) as db, db.begin():
        reliability.configure_fault(
            db,
            context(db),
            connection_id,
            FailureConfig(
                enabled=True, scenario=scenario, attempts=attempts, retry_after=retry_after
            ),
        )


def due(engine, delivery_id):
    with Session(engine) as db, db.begin():
        db.get(NotificationDelivery, delivery_id).next_attempt_at = utcnow() - timedelta(seconds=1)


def test_version_render_and_immutability(workflow_engine):
    ids = configured(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        rendered = service.render(db, context(db), ids[1], RenderInputs(job_title="<b>& @everyone"))
        assert "&lt;b&gt;&amp; ＠everyone" in rendered["body"]
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("UPDATE notification_template_versions SET body='changed' WHERE id=:id"),
                {"id": ids[1]},
            )
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(
                text("DELETE FROM notification_rule_versions WHERE rule_id=:id"), {"id": ids[0]}
            )


def test_rule_revision_and_destination_validation(workflow_engine):
    ids = configured(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        row = db.get(NotificationRule, ids[0])
        service.update_rule(db, ctx, row, RuleUpdate(expected_revision=1, enabled=False))
        with pytest.raises(HTTPException) as e:
            service.update_rule(db, ctx, row, RuleUpdate(expected_revision=1, enabled=True))
        assert e.value.status_code == 409
        with pytest.raises(HTTPException):
            service.new_rule(
                db,
                ctx,
                RuleCreate(
                    name="unsafe",
                    trigger="MANUAL",
                    connection_id=ids[2],
                    template_version_id=ids[1],
                    destination="arbitrary",
                    enabled=True,
                ),
            )


def test_manual_duplicate_and_content_guard(workflow_engine):
    ids = configured(workflow_engine)
    key = uuid.uuid4()
    first = manual(workflow_engine, ids, key)
    assert manual(workflow_engine, ids, key) == first
    with pytest.raises(HTTPException):
        manual(workflow_engine, ids, key, RenderInputs(job_title="Changed"))
    tick(workflow_engine, ids[3])
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, first).status == "SUCCEEDED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(
                    SyntheticNotification.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(NotificationReceipt)) == 1


def test_sync_atomic_outbox_and_duplicate_completion(workflow_engine):
    syncids = sync_setup(workflow_engine)
    notify = configured(workflow_engine, trigger="SYNC_CREATE")
    planned = make_plan(workflow_engine, syncids)
    assert apply_plan(workflow_engine, planned) == "SUCCEEDED"
    apply_plan(workflow_engine, planned)
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(NotificationIntent)) == 1
        intent = db.scalar(select(NotificationIntent))
        assert intent.sync_execution_id == planned[1]
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1
    tick(workflow_engine, notify[3])
    with Session(workflow_engine) as db:
        assert db.scalar(select(NotificationDelivery.status)) == "SUCCEEDED"


def test_outbox_rolls_back_with_business_transaction(workflow_engine):
    syncids = sync_setup(workflow_engine)
    configured(workflow_engine, trigger="SYNC_CREATE")
    planned = make_plan(workflow_engine, syncids)
    with Session(workflow_engine) as db:
        ctx = context(db)
        from app.services.synchronization import apply

        apply(db, ctx, db.get(SyncPlan, planned[0]), db.get(SyncExecution, planned[1]))
        db.rollback()
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(NotificationIntent)) == 0
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 0
        assert db.get(SyncExecution, planned[1]).state == "PLANNED"


@pytest.mark.parametrize("channel", ["SLACK", "EMAIL"])
def test_transient_retry_does_not_repeat_sync(workflow_engine, channel):
    syncids = sync_setup(workflow_engine)
    ids = configured(workflow_engine, channel, trigger="SYNC_CREATE")
    planned = make_plan(workflow_engine, syncids)
    apply_plan(workflow_engine, planned)
    fault(workflow_engine, ids[2], "HTTP_503", retry_after="7")
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        row = db.scalar(select(NotificationDelivery))
        delivery_id = row.id
        assert row.status == "RETRY_SCHEDULED" and row.next_attempt_at >= utcnow() + timedelta(
            seconds=5
        )
        assert db.scalar(select(NotificationDeliveryAttempt.retry_after)) == 7
        assert db.get(SyncExecution, planned[1]).state == "SUCCEEDED"
    due(workflow_engine, delivery_id)
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, delivery_id).status == "SUCCEEDED"
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1


def test_mixed_channels_only_failed_email_retried(workflow_engine):
    slack = configured(workflow_engine)
    email = configured(workflow_engine, "EMAIL")
    a = manual(workflow_engine, slack)
    b = manual(workflow_engine, email)
    fault(workflow_engine, email[2], "HTTP_503")
    tick(workflow_engine, slack[3])
    tick(workflow_engine, slack[3])
    due(workflow_engine, b)
    tick(workflow_engine, slack[3])
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, a).attempt_count == 1
        assert db.get(NotificationDelivery, b).attempt_count == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(
                    SyntheticNotification.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticEmail)
                .where(SyntheticEmail.connection_id.in_(select(NotificationDelivery.connection_id)))
            )
            == 1
        )


def test_terminal_failure_alert_and_no_resend(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "HTTP_403")
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db, db.begin():
        row = db.get(NotificationDelivery, delivery_id)
        assert row.status == "FAILED"
        assert "TERMINAL_NOTIFICATION_FAILURE" in list(
            db.scalars(select(OperationalAlert.category))
        )
        with pytest.raises(HTTPException):
            service.recover(db, context(db), row, "retry")
    assert tick(workflow_engine, ids[3]) is None


def test_unknown_timeout_blocks_resend_and_cancellation(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "READ_TIMEOUT")
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db, db.begin():
        row = db.get(NotificationDelivery, delivery_id)
        assert row.status == "UNKNOWN"
        result = service.recover(db, context(db), row, "review")
        assert result["resolved"] is False
        with pytest.raises(HTTPException):
            service.recover(db, context(db), row, "retry")
        service.recover(db, context(db), row, "cancel")
    assert tick(workflow_engine, ids[3]) is None


def test_provider_receipt_lookup_after_missing_checkpoint(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        row = db.get(NotificationDelivery, delivery_id)
        from app.connectors.contracts import ConnectorOperation
        from app.services import integrations

        integrations.execute(
            db,
            context(db),
            row.connection_id,
            ConnectorOperation(name="message.send", payload=row.payload),
        )
        row.status = "UNKNOWN"
        assert service.recover(db, context(db), row, "review")["resolved"]
    with Session(workflow_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(
                    SyntheticNotification.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )


def test_worker_lease_recovery_and_fencing(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        old = service.claim(db, ids[3])
        db.get(NotificationDelivery, delivery_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    with Session(workflow_engine) as db, db.begin():
        new = service.claim(db, ids[3])
    service.run_claim(workflow_engine, old)
    service.run_claim(workflow_engine, new)
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, delivery_id).status == "SUCCEEDED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(
                    SyntheticNotification.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )
        assert "LEASE_RECOVERED" in list(db.scalars(select(NotificationDeliveryAttempt.status)))

    # Five consecutive lost leases must not produce an unbounded retry loop.
    held_id = manual(workflow_engine, ids)
    for _ in range(5):
        with Session(workflow_engine) as db, db.begin():
            service.claim(db, ids[3])
            db.get(NotificationDelivery, held_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        held = db.get(NotificationDelivery, held_id)
        assert held.status == "UNKNOWN" and held.error_category == "LEASE_EXHAUSTED"
        assert service.provider_receipt(db, held) is None
    assert tick(workflow_engine, ids[3]) is None


def test_crash_after_provider_rolls_back_effect_and_recovers(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        job = service.claim(db, ids[3])

    class Crash(BaseException):
        pass

    with patch.object(service, "complete", side_effect=Crash), pytest.raises(Crash):
        service.run_claim(workflow_engine, job)
    with Session(workflow_engine) as db, db.begin():
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(
                    SyntheticNotification.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 0
        )
        db.get(NotificationDelivery, delivery_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(
                    SyntheticNotification.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )


def test_concurrent_claim_and_intent_uniqueness(workflow_engine):
    ids = configured(workflow_engine)
    key = uuid.uuid4()
    barrier = Barrier(2)

    def enqueue(_):
        barrier.wait()
        return manual(workflow_engine, ids, key)

    with ThreadPoolExecutor(2) as pool:
        assert len(set(pool.map(enqueue, range(2)))) == 1
    barrier = Barrier(2)

    def claim(_):
        with Session(workflow_engine) as db, db.begin():
            barrier.wait()
            return service.claim(db, ids[3])

    with ThreadPoolExecutor(2) as pool:
        assert sum(x is not None for x in pool.map(claim, range(2))) == 1


def calendar_input(engine, start=None):
    with Session(engine) as db:
        ctx = context(db)
        conn = integrations.create_connection(
            db,
            ctx,
            CreateConnection(
                name="Calendar-" + uuid.uuid4().hex, connector_type="synthetic_calendar"
            ),
        )
        db.commit()
        return CalendarCreate(
            connection_id=conn.id,
            calendar="interviews",
            title="Synthetic interview",
            starts_at=start or utcnow() + timedelta(days=1),
            ends_at=(start or utcnow() + timedelta(days=1)) + timedelta(hours=1),
            participants=["recruiter@example.test"],
            request_key=uuid.uuid4(),
        )


def queue_calendar(engine, data, event_id=None):
    with Session(engine) as db, db.begin():
        return service.calendar_queue(db, context(db), data, uuid.uuid4(), event_id).id


def calendar_tick(engine):
    with Session(engine) as db, db.begin():
        worker_id = worker.register_worker(db)
    return tick(engine, worker_id)


def test_calendar_create_reschedule_cancel_idempotency(workflow_engine):
    data = calendar_input(workflow_engine)
    request = queue_calendar(workflow_engine, data)
    assert queue_calendar(workflow_engine, data) == request
    calendar_tick(workflow_engine)
    with Session(workflow_engine) as db:
        event = db.scalar(
            select(SyntheticCalendarEvent).where(
                SyntheticCalendarEvent.connection_id == data.connection_id
            )
        )
        event_id = event.id
        revision = event.updated_at.isoformat()
    changed = CalendarChange(
        calendar="interviews",
        title="Rescheduled interview",
        starts_at=data.starts_at + timedelta(hours=2),
        ends_at=data.ends_at + timedelta(hours=2),
        participants=data.participants,
        request_key=uuid.uuid4(),
        expected_updated_at=revision,
    )
    request2 = queue_calendar(workflow_engine, changed, event_id)
    calendar_tick(workflow_engine)
    assert queue_calendar(workflow_engine, changed, event_id) == request2
    with Session(workflow_engine) as db:
        revision = db.get(SyntheticCalendarEvent, event_id).updated_at.isoformat()
    cancelled = CalendarCancel(request_key=uuid.uuid4(), expected_updated_at=revision)
    request3 = queue_calendar(workflow_engine, cancelled, event_id)
    calendar_tick(workflow_engine)
    assert queue_calendar(workflow_engine, cancelled, event_id) == request3
    with Session(workflow_engine) as db:
        assert db.get(SyntheticCalendarEvent, event_id).status == "CANCELLED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticCalendarEvent)
                .where(
                    SyntheticCalendarEvent.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(NotificationReceipt)) == 3


def test_calendar_overlap_and_availability(workflow_engine):
    data = calendar_input(workflow_engine)
    queue_calendar(workflow_engine, data)
    calendar_tick(workflow_engine)
    with Session(workflow_engine) as db:
        result = service.availability(
            db, context(db), data.connection_id, data.calendar, data.starts_at, data.ends_at
        )
        assert not result["available"]
    overlapping = data.model_copy(update={"request_key": uuid.uuid4(), "title": "Overlapping"})
    request = queue_calendar(workflow_engine, overlapping)
    calendar_tick(workflow_engine)
    with Session(workflow_engine) as db:
        action = db.get(CalendarCoordinationRequest, request)
        row = db.get(NotificationDelivery, action.delivery_id)
        assert row.status == "FAILED" and row.error_category == "CONFLICT"
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticCalendarEvent)
                .where(
                    SyntheticCalendarEvent.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )
        assert "CALENDAR_CONFLICT" in list(db.scalars(select(OperationalAlert.category)))


def test_calendar_concurrent_overlap_only_one_effect(workflow_engine):
    data = calendar_input(workflow_engine)
    queue_calendar(workflow_engine, data)
    queue_calendar(workflow_engine, data.model_copy(update={"request_key": uuid.uuid4()}))
    with Session(workflow_engine) as db, db.begin():
        w1 = worker.register_worker(db)
        w2 = worker.register_worker(db)
    with Session(workflow_engine) as db, db.begin():
        a = service.claim(db, w1)
    with Session(workflow_engine) as db, db.begin():
        b = service.claim(db, w2)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda x: service.run_claim(workflow_engine, x), [a, b]))
    with Session(workflow_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticCalendarEvent)
                .where(
                    SyntheticCalendarEvent.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 1
        )


def test_cancellation_during_retry(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "HTTP_503")
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db, db.begin():
        service.recover(db, context(db), db.get(NotificationDelivery, delivery_id), "cancel")
    due(workflow_engine, delivery_id)
    assert tick(workflow_engine, ids[3]) is None
    with Session(workflow_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(
                    SyntheticNotification.connection_id.in_(
                        select(NotificationDelivery.connection_id)
                    )
                )
            )
            == 0
        )


def test_cross_tenant_delivery_calendar_and_foreign_keys(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    data = calendar_input(workflow_engine)
    request_id = queue_calendar(workflow_engine, data)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        other = db.scalar(select(Tenant).where(Tenant.id != ctx.tenant.id))
        ctx.tenant = other
        for model, rid in [
            (NotificationDelivery, delivery_id),
            (CalendarCoordinationRequest, request_id),
        ]:
            with pytest.raises(HTTPException) as e:
                service.scoped(db, ctx, model, rid)
            assert e.value.status_code == 404
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(
                text("UPDATE notification_deliveries SET tenant_id=:t WHERE id=:id"),
                {"t": other.id, "id": delivery_id},
            )

        foreign_event = db.scalar(
            select(SyntheticCalendarEvent).where(SyntheticCalendarEvent.tenant_id == other.id)
        )
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(
                text("UPDATE calendar_coordination_requests SET event_id=:event WHERE id=:id"),
                {"event": foreign_event.id, "id": request_id},
            )


def test_api_roles_origin_validation_and_redaction(identity_env):
    env = identity_env
    client = env["client"]
    login(env)
    mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["a"].id)})
    response = mutation(
        client,
        "POST",
        "notifications/templates",
        {"name": "Synthetic", "channel": "SLACK", "body": "{job_title}"},
    )
    assert response.status_code == 201
    template = response.json()
    assert "tenant_id" not in template and "password" not in response.text
    assert client.post("/api/v1/notifications/templates", json={}).status_code == 403
    assert (
        mutation(
            client,
            "POST",
            "notifications/templates",
            {"name": "Bad", "channel": "SLACK", "body": "{password}"},
        ).status_code
        == 422
    )
    assert client.get("/api/v1/notifications/metrics").status_code == 200
    mutation(client, "POST", "auth/logout")
    login(env, "OPERATIONS_USER")
    assert (
        mutation(
            client,
            "POST",
            "notifications/templates",
            {"name": "No", "channel": "SLACK", "body": "test"},
        ).status_code
        == 403
    )
    assert client.get("/api/v1/notifications/deliveries").status_code == 200
    assert mutation(client, "POST", "calendar/events", {}).status_code in {403, 422}


def test_rule_trigger_scope_and_noop_suppression(workflow_engine):
    syncids = sync_setup(workflow_engine)
    configured(workflow_engine, trigger="SYNC_UPDATE")
    planned = make_plan(workflow_engine, syncids)
    apply_plan(workflow_engine, planned)
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(NotificationIntent)) == 0
        assert service.matching_rules(db, context(db).tenant.id, "SYNC_CREATE", syncids[0]) == []


def test_terminal_workflow_failure_creates_one_intent(workflow_engine):
    from test_workflows import dispatched, setup
    from test_workflows import tick as workflow_tick

    configured(workflow_engine, trigger="WORKFLOW_FAILURE")
    with Session(workflow_engine) as db:
        slack = fixture_ids(db)[3].id
    ids = setup(
        workflow_engine,
        [
            {
                "id": "fail",
                "type": "MOCK_CONNECTOR_ACTION",
                "config": {
                    "connection_id": str(slack),
                    "operation": "message.send",
                    "payload": {"channel": "recruiting", "text": "Synthetic failing action"},
                    "fault": {"mode": "PERMANENT"},
                },
                "retry": {"max_attempts": 1},
            }
        ],
    )
    execution_id = dispatched(workflow_engine, ids)
    workflow_tick(workflow_engine, ids[3])
    from app.models.workflow import WorkflowExecution

    with Session(workflow_engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        assert row.state in {"FAILED", "DEAD_LETTERED"}
        service.emit_failure(db, row)
        assert db.scalar(select(func.count()).select_from(NotificationIntent)) == 1
        assert db.scalar(select(NotificationIntent.workflow_execution_id)) == row.id


def test_retry_exhaustion_persists_terminal_and_repeated_alert(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "HTTP_503", attempts=3)
    for _ in range(3):
        due(workflow_engine, delivery_id)
        tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, delivery_id).status == "FAILED"
        assert db.get(NotificationDelivery, delivery_id).attempt_count == 3
        categories = set(db.scalars(select(OperationalAlert.category)))
        assert {"REPEATED_DELIVERY_FAILURE", "TERMINAL_NOTIFICATION_FAILURE"} <= categories
    assert tick(workflow_engine, ids[3]) is None


def test_unknown_receipt_digest_mismatch_never_resends(workflow_engine):
    from app.connectors.contracts import ConnectorOperation

    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        row = db.get(NotificationDelivery, delivery_id)
        integrations.execute(
            db,
            context(db),
            row.connection_id,
            ConnectorOperation(
                name="message.send", payload={**row.payload, "text": "Different synthetic action"}
            ),
        )
        row.status = "UNKNOWN"
        assert not service.recover(db, context(db), row, "review")["resolved"]
        assert service.provider_receipt(db, row) == {"mismatch": True}
    assert tick(workflow_engine, ids[3]) is None


def test_revoked_principal_cannot_deliver(workflow_engine):
    from app.models.identity import Membership

    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant.id, Membership.user_id == ctx.identity.user.id
            )
        ).status = "INACTIVE"
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, delivery_id).status == "FAILED"


def test_calendar_stale_revision_does_not_overwrite(workflow_engine):
    data = calendar_input(workflow_engine)
    queue_calendar(workflow_engine, data)
    calendar_tick(workflow_engine)
    with Session(workflow_engine) as db:
        event = db.scalar(
            select(SyntheticCalendarEvent).where(
                SyntheticCalendarEvent.connection_id == data.connection_id
            )
        )
        event_id = event.id
        revision = event.updated_at
    change = CalendarChange(
        calendar="interviews",
        title="Reviewed interview",
        starts_at=data.starts_at + timedelta(hours=3),
        ends_at=data.ends_at + timedelta(hours=3),
        participants=data.participants,
        request_key=uuid.uuid4(),
        expected_updated_at=revision,
    )
    request_id = queue_calendar(workflow_engine, change, event_id)
    with Session(workflow_engine) as db, db.begin():
        db.get(SyntheticCalendarEvent, event_id).title = "Concurrent synthetic edit"
    calendar_tick(workflow_engine)
    with Session(workflow_engine) as db:
        request = db.get(CalendarCoordinationRequest, request_id)
        assert db.get(NotificationDelivery, request.delivery_id).error_category == "CONFLICT"
        assert db.get(SyntheticCalendarEvent, event_id).title == "Concurrent synthetic edit"


def test_api_operations_recovery_tenant_and_audit(identity_env):
    from app.models.identity import AuditEvent

    env = identity_env
    client = env["client"]
    login(env)
    mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["a"].id)})
    conn = mutation(
        client,
        "POST",
        "connections",
        {"name": "Local Slack", "connector_type": "synthetic_slack", "config": {}},
    ).json()
    assert "id" in conn
    t = mutation(
        client,
        "POST",
        "notifications/templates",
        {"name": "Local template", "channel": "SLACK", "body": "{job_title}"},
    ).json()
    version = client.get(f"/api/v1/notifications/templates/{t['id']}/versions").json()["items"][0][
        "id"
    ]
    assert (
        mutation(
            client, "POST", f"notifications/templates/{t['id']}/activate", {"version_id": version}
        ).status_code
        == 200
    )
    r = mutation(
        client,
        "POST",
        "notifications/rules",
        {
            "name": "Manual",
            "trigger": "MANUAL",
            "connection_id": conn["id"],
            "template_version_id": version,
            "destination": "recruiting",
            "enabled": True,
        },
    ).json()
    intent = mutation(
        client,
        "POST",
        "notifications/manual",
        {
            "rule_id": r["id"],
            "request_key": str(uuid.uuid4()),
            "inputs": {"job_title": "Synthetic operator notice"},
        },
    ).json()
    delivery = client.get("/api/v1/notifications/intents/" + intent["id"]).json()["delivery"]
    mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    assert client.get("/api/v1/notifications/deliveries/" + delivery["id"]).status_code == 404
    assert client.get("/api/v1/notifications/templates/" + t["id"] + "/versions").status_code == 404
    mutation(client, "POST", "auth/logout")
    login(env, "OPERATIONS_USER")
    assert (
        mutation(
            client,
            "POST",
            "notifications/deliveries/" + delivery["id"] + "/cancel",
            {"reason": "Cancel synthetic queued action"},
        ).status_code
        == 200
    )
    valid = {
        "connection_id": str(uuid.uuid4()),
        "calendar": "interviews",
        "title": "Synthetic",
        "starts_at": utcnow().isoformat(),
        "ends_at": (utcnow() + timedelta(hours=1)).isoformat(),
        "participants": ["recruiter@example.test"],
        "request_key": str(uuid.uuid4()),
    }
    assert mutation(client, "POST", "calendar/events", valid).status_code == 403
    with Session(env["connection"]) as db:
        actions = list(db.scalars(select(AuditEvent.action)))
        assert "notification.cancel" in actions
        assert all("secret" not in str(d) for d in db.scalars(select(AuditEvent.details)))
