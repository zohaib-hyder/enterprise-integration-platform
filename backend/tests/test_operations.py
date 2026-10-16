# ruff: noqa: F811
"""Phase 9 deterministic contracts and real PostgreSQL operational governance."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_identity import login, mutation
from test_notifications import (
    calendar_input,
    calendar_tick,
    configured,
    fault,
    manual,
    queue_calendar,
    tick,
)
from test_synchronization import apply_plan, conflict_setup, context, make_plan
from test_synchronization import setup as sync_setup
from test_workflows import dispatched, fixture_ids, workflow_engine  # noqa:F401
from test_workflows import setup as workflow_setup
from test_workflows import tick as workflow_tick

from app.api.dependencies import Identity, authorized_context
from app.core.security import utcnow
from app.models.identity import Membership, User
from app.models.notification import NotificationDelivery, NotificationIntent
from app.models.operations import (
    Incident,
    IncidentEvidence,
    OperationalFinding,
    OperationsActivity,
    RecoveryApproval,
    RecoveryRequest,
)
from app.models.tenant import Tenant
from app.models.webhook import IncomingEvent
from app.models.workflow import WorkflowActionReceipt, WorkflowExecution
from app.operations.runbooks import CATALOG, runbook
from app.schemas.operations import (
    ApprovalDecision,
    Confirmation,
    IncidentChange,
    Reason,
    RecoveryCreate,
    ScanInput,
)
from app.services import operation_recovery as recovery
from app.services import operational_reconciliation as reconciliation
from app.services import operations as ops


@pytest.mark.parametrize(
    "reason", ["password=sim_bad", "https://external.test", "<script>", "        ", "token:sim_bad"]
)
def test_reason_redaction_contract(reason):
    with pytest.raises(ValidationError):
        Reason(reason=reason)


def test_runbook_catalog():
    assert len(CATALOG) == 10
    for key in CATALOG:
        book = runbook(key)
        assert book["version"] == 1 and all(
            book[k]
            for k in [
                "symptom",
                "evidence_to_inspect",
                "preconditions",
                "safe_recovery_options",
                "escalation_conditions",
                "verification_steps",
            ]
        )
    assert runbook("arbitrary") is None


def record_incident(engine, category="UNKNOWN_DELIVERY_OUTCOME", delivery=True):
    ids = configured(engine)
    delivery_id = manual(engine, ids)
    with Session(engine) as db, db.begin():
        ctx = context(db)
        row = ops.observe(
            db,
            ctx.tenant.id,
            category,
            "delivery",
            delivery_id,
            "delivery",
            delivery_id,
            "one",
            "TIMEOUT",
            uuid.uuid4(),
        )
        return row.id, delivery_id, ids


def request(engine, kind, identifier, action):
    with Session(engine) as db, db.begin():
        ctx = context(db)
        preview = recovery.plan(db, ctx, kind, identifier)
        row = recovery.create_request(
            db,
            ctx,
            RecoveryCreate(
                target_kind=kind,
                target_id=identifier,
                action=action,
                evidence_revision=preview["evidence_revision"],
                request_key=uuid.uuid4(),
                reason="Inspect and recover synthetic operation",
            ),
            uuid.uuid4(),
        )
        return row.id, row.evidence_revision


def confirm(engine, request_id, revision):
    with Session(engine) as db, db.begin():
        return recovery.confirm(
            db,
            context(db),
            db.get(RecoveryRequest, request_id),
            Confirmation(evidence_revision=revision, confirm=True),
        ).status


def reviewer(db):
    ctx = context(db)
    user = User(
        email="reviewer-" + uuid.uuid4().hex + "@example.test",
        display_name="Synthetic independent reviewer",
        password_hash=ctx.identity.user.password_hash,
    )
    db.add(user)
    db.flush()
    db.add(Membership(tenant_id=ctx.tenant.id, user_id=user.id, role="INTEGRATION_ENGINEER"))
    db.flush()
    return authorized_context(db, Identity(user, None), ctx.tenant.id)


def test_overview_real_counts_and_window(workflow_engine):
    ids = configured(workflow_engine)
    manual(workflow_engine, ids)
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        ctx = context(db)
        result = ops.overview(db, ctx, 24)
        assert result["deliveries"]["SUCCEEDED"] == 1
        assert sum(result["deliveries"].values()) == db.scalar(
            select(func.count())
            .select_from(NotificationDelivery)
            .where(NotificationDelivery.tenant_id == ctx.tenant.id)
        )
        assert result["snapshot"]["worker_health"] == "UNKNOWN"
        assert result["snapshot"]["tenant_lease_workers_fresh"] == 0
        assert result["window_hours"] == 24


def test_overview_tenant_scope(workflow_engine):
    ids = configured(workflow_engine)
    manual(workflow_engine, ids)
    with Session(workflow_engine) as db:
        ctx = context(db)
        other = db.scalar(select(Tenant).where(Tenant.id != ctx.tenant.id))
        ctx = authorized_context(db, ctx.identity, other.id)
        assert ops.overview(db, ctx, 720)["deliveries"] == {}


def test_timeline_real_sync_delivery_and_no_payload(workflow_engine):
    syncids = sync_setup(workflow_engine)
    ids = configured(workflow_engine, trigger="SYNC_CREATE")
    planned = make_plan(workflow_engine, syncids)
    apply_plan(workflow_engine, planned)
    tick(workflow_engine, ids[3])
    from app.models.synchronization import SyncPlan

    with Session(workflow_engine) as db:
        correlation = db.get(SyncPlan, planned[0]).correlation_id
        result = ops.timeline(db, context(db), correlation)
        kinds = {x["event_type"] for x in result["items"]}
        assert {
            "incoming_events",
            "sync_plans",
            "sync_executions",
            "sync_operations",
            "notification_intents",
            "notification_deliveries",
            "notification_delivery_attempts",
            "notification_receipts",
        } <= kinds
        assert [x["timestamp"] for x in result["items"]] == sorted(
            x["timestamp"] for x in result["items"]
        )
        assert all(x["detail_link"].startswith("/") and "payload" not in x for x in result["items"])


def test_timeline_missing_and_cross_tenant(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    with Session(workflow_engine) as db:
        ctx = context(db)
        row = db.get(NotificationDelivery, delivery_id)
        with pytest.raises(HTTPException):
            ops.timeline(db, ctx, uuid.uuid4())
        other = db.scalar(select(Tenant).where(Tenant.id != ctx.tenant.id))
        otherctx = authorized_context(db, ctx.identity, other.id)
        with pytest.raises(HTTPException) as e:
            ops.timeline(db, otherctx, row.correlation_id)
        assert e.value.status_code == 404


def test_incident_dedup_and_reopen(workflow_engine):
    incident_id, delivery_id, _ = record_incident(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        row = db.get(Incident, incident_id)
        duplicate = ops.observe(
            db,
            ctx.tenant.id,
            row.category,
            "delivery",
            delivery_id,
            "delivery",
            delivery_id,
            "one",
            "TIMEOUT",
        )
        assert duplicate.id == row.id and row.occurrence_count == 1
        ops.change_incident(
            db,
            ctx,
            row,
            IncidentChange(
                expected_revision=row.revision,
                status="RESOLVED",
                reason="Synthetic uncertainty recorded for investigation",
            ),
        )
        ops.observe(
            db,
            ctx.tenant.id,
            row.category,
            "delivery",
            delivery_id,
            "delivery",
            delivery_id,
            "two",
            "TIMEOUT",
        )
        assert row.status == "REOPENED" and row.occurrence_count == 2
        assert db.scalar(select(func.count()).select_from(Incident)) == 1


def test_incident_concurrent_dedup(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    barrier = Barrier(2)

    def observe(_):
        with Session(workflow_engine) as db, db.begin():
            ctx = context(db)
            barrier.wait()
            return ops.observe(
                db,
                ctx.tenant.id,
                "NOTIFICATION_DELIVERY_FAILURE",
                "delivery",
                delivery_id,
                "delivery",
                delivery_id,
                "one",
                "TEMPORARY_UNAVAILABLE",
            ).id

    with ThreadPoolExecutor(2) as pool:
        assert len(set(pool.map(observe, range(2)))) == 1
    with Session(workflow_engine) as db:
        assert db.scalar(select(Incident.occurrence_count)) == 1


def test_incident_assignment_and_stale_guard(workflow_engine):
    incident_id, _, _ = record_incident(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        row = db.get(Incident, incident_id)
        revision = row.revision
        ops.change_incident(
            db,
            ctx,
            row,
            IncidentChange(
                expected_revision=revision,
                assigned_user_id=ctx.identity.user.id,
                severity="CRITICAL",
                acknowledge=True,
                reason="Investigate verified synthetic failure",
            ),
        )
        assert row.assigned_user_id == ctx.identity.user.id and row.status == "INVESTIGATING"
        with pytest.raises(HTTPException):
            ops.change_incident(
                db,
                ctx,
                row,
                IncidentChange(
                    expected_revision=revision,
                    status="RESOLVED",
                    reason="Stale operator request must be rejected",
                ),
            )
        with pytest.raises(HTTPException):
            ops.change_incident(
                db,
                ctx,
                row,
                IncidentChange(
                    expected_revision=row.revision,
                    assigned_user_id=uuid.uuid4(),
                    reason="Assignee must belong to this tenant",
                ),
            )


def test_incident_history_immutable(workflow_engine):
    incident_id, _, _ = record_incident(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        for table in ["operation_activities", "operation_incident_evidence"]:
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(text(f"UPDATE {table} SET updated_at=now()"))
        assert db.scalar(select(IncidentEvidence.incident_id)) == incident_id


def test_readonly_preview_immutability_and_classification(workflow_engine):
    incident_id, delivery_id, ids = record_incident(workflow_engine)
    fault(workflow_engine, ids[2], "READ_TIMEOUT")
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        before = (
            db.scalar(select(func.count()).select_from(OperationsActivity)),
            db.get(NotificationDelivery, delivery_id).status,
        )
        p = recovery.plan(db, ctx, "delivery", delivery_id)
        assert p["classification"] == "TIMEOUT" and "Observed persisted" in p["certainty"]
        assert next(a for a in p["actions"] if a["action"] == "REVIEW_UNKNOWN_OUTCOME")["eligible"]
        assert not next(a for a in p["actions"] if a["action"] == "RETRY_NOTIFICATION")["eligible"]
        assert before == (
            db.scalar(select(func.count()).select_from(OperationsActivity)),
            db.get(NotificationDelivery, delivery_id).status,
        )
        assert recovery.plan(db, ctx, "incident", incident_id)["evidence_references"]


def test_recovery_confirmation_idempotency(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "HTTP_503")
    tick(workflow_engine, ids[3])
    request_id, revision = request(workflow_engine, "delivery", delivery_id, "RETRY_NOTIFICATION")
    assert confirm(workflow_engine, request_id, revision) == "EXECUTED"
    assert confirm(workflow_engine, request_id, revision) == "EXECUTED"
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, delivery_id).attempt_count == 1
        assert db.get(RecoveryRequest, request_id).result["outcome"] == "SCHEDULE_PRESERVED"


def test_stale_recovery_request_no_effect(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    request_id, revision = request(
        workflow_engine, "delivery", delivery_id, "ESCALATE_FOR_MANUAL_REVIEW"
    )
    with Session(workflow_engine) as db, db.begin():
        db.get(NotificationDelivery, delivery_id).status = "CANCELLED"
    assert confirm(workflow_engine, request_id, revision) == "STALE"


def test_recovery_request_key_binding_and_concurrency(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    key = uuid.uuid4()
    with Session(workflow_engine) as db:
        p = recovery.plan(db, context(db), "delivery", delivery_id)
    data = RecoveryCreate(
        target_kind="delivery",
        target_id=delivery_id,
        action="ESCALATE_FOR_MANUAL_REVIEW",
        evidence_revision=p["evidence_revision"],
        request_key=key,
        reason="Record a safe synthetic escalation",
    )
    barrier = Barrier(2)

    def create(_):
        with Session(workflow_engine) as db, db.begin():
            ctx = context(db)
            barrier.wait()
            return recovery.create_request(db, ctx, data, uuid.uuid4()).id

    with ThreadPoolExecutor(2) as pool:
        assert len(set(pool.map(create, range(2)))) == 1
    with Session(workflow_engine) as db, db.begin(), pytest.raises(HTTPException):
        recovery.create_request(
            db,
            context(db),
            data.model_copy(update={"reason": "Changed bound request reason"}),
            uuid.uuid4(),
        )


def test_unknown_outcome_cannot_be_retried(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "READ_TIMEOUT")
    tick(workflow_engine, ids[3])
    rid, rev = request(workflow_engine, "delivery", delivery_id, "RETRY_NOTIFICATION")
    assert confirm(workflow_engine, rid, rev) == "BLOCKED"
    review, revision = request(workflow_engine, "delivery", delivery_id, "REVIEW_UNKNOWN_OUTCOME")
    assert confirm(workflow_engine, review, revision) == "EXECUTED"
    with Session(workflow_engine) as db:
        assert db.get(NotificationDelivery, delivery_id).status == "UNKNOWN"
        assert db.get(RecoveryRequest, review).result["outcome"] == "REVIEWED_UNRESOLVED"


def terminal_workflow(engine):
    with Session(engine) as db:
        slack = fixture_ids(db)[3].id
    ids = workflow_setup(
        engine,
        [
            {
                "id": "fail",
                "type": "MOCK_CONNECTOR_ACTION",
                "config": {
                    "connection_id": str(slack),
                    "operation": "message.send",
                    "payload": {"channel": "recruiting", "text": "Synthetic operational failure"},
                    "fault": {"mode": "PERMANENT"},
                },
                "retry": {"max_attempts": 1},
            }
        ],
    )
    eid = dispatched(engine, ids)
    workflow_tick(engine, ids[3])
    return eid, ids


def test_replay_approval_distinct_actor_and_expiration(workflow_engine):
    execution_id, _ = terminal_workflow(workflow_engine)
    rid, rev = request(workflow_engine, "workflow", execution_id, "REPLAY_WORKFLOW")
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        approval = db.scalar(select(RecoveryApproval).where(RecoveryApproval.request_id == rid))
        with pytest.raises(HTTPException):
            recovery.approve(
                db,
                ctx,
                approval,
                ApprovalDecision(
                    evidence_revision=rev,
                    decision="APPROVED",
                    reason="Independent review is required",
                ),
            )
        other = reviewer(db)
        approval.expires_at = utcnow() - timedelta(seconds=1)
        with pytest.raises(HTTPException):
            recovery.approve(
                db,
                other,
                approval,
                ApprovalDecision(
                    evidence_revision=rev,
                    decision="APPROVED",
                    reason="Expired approval must not execute",
                ),
            )


def test_approved_checkpoint_preserving_replay(workflow_engine):
    execution_id, _ = terminal_workflow(workflow_engine)
    rid, rev = request(workflow_engine, "workflow", execution_id, "REPLAY_WORKFLOW")
    with Session(workflow_engine) as db, db.begin():
        approval = db.scalar(select(RecoveryApproval).where(RecoveryApproval.request_id == rid))
        recovery.approve(
            db,
            reviewer(db),
            approval,
            ApprovalDecision(
                evidence_revision=rev,
                decision="APPROVED",
                reason="Verified receipt state and synthetic replay scope",
            ),
        )
    assert confirm(workflow_engine, rid, rev) == "EXECUTED"
    assert confirm(workflow_engine, rid, rev) == "EXECUTED"
    with Session(workflow_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkflowExecution)
                .where(WorkflowExecution.replay_of_id == execution_id)
            )
            == 1
        )
        assert db.get(RecoveryRequest, rid).result["outcome"] == "QUEUED"


def test_approval_never_overrides_unknown_receipt(workflow_engine):
    execution_id, _ = terminal_workflow(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        db.add(
            WorkflowActionReceipt(
                tenant_id=row.tenant_id,
                root_execution_id=row.root_id,
                connection_id=row.connection_id,
                step_id="uncertain",
                request_digest="a" * 64,
                state="UNKNOWN",
                result={},
            )
        )
    rid, rev = request(workflow_engine, "workflow", execution_id, "REPLAY_WORKFLOW")
    with Session(workflow_engine) as db, db.begin():
        approval = db.scalar(select(RecoveryApproval).where(RecoveryApproval.request_id == rid))
        recovery.approve(
            db,
            reviewer(db),
            approval,
            ApprovalDecision(
                evidence_revision=rev,
                decision="APPROVED",
                reason="Record independent review without bypassing safety",
            ),
        )
    assert confirm(workflow_engine, rid, rev) == "BLOCKED"


def test_reconciliation_overdue_and_normal_pending(workflow_engine):
    ids = configured(workflow_engine)
    one = manual(workflow_engine, ids)
    two = manual(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        db.get(NotificationDelivery, one).next_attempt_at = utcnow() - timedelta(minutes=10)
        run = reconciliation.scan(db, context(db), ScanInput(), uuid.uuid4())
        findings = list(
            db.scalars(select(OperationalFinding).where(OperationalFinding.run_id == run.id))
        )
        assert any(x.target_id == one and x.category == "DELIVERY_OVERDUE" for x in findings)
        assert not any(x.target_id == two for x in findings)
        assert db.get(NotificationDelivery, one).status == "PENDING"
        assert run.summary["read_only_business_state"]


def test_reconciliation_accepted_eligible_without_dispatch(workflow_engine):
    ids = sync_setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        event = db.get(IncomingEvent, ids[1])
        event.received_at = utcnow() - timedelta(minutes=10)
        run = reconciliation.scan(db, context(db), ScanInput(), uuid.uuid4())
        assert "ACCEPTED_EVENT_UNDISPATCHED" in run.summary["counts"]


def test_reconciliation_missing_notification_intent(workflow_engine):
    syncids = sync_setup(workflow_engine)
    configured(workflow_engine, trigger="SYNC_CREATE")
    planned = make_plan(workflow_engine, syncids)
    from unittest.mock import patch

    with patch("app.services.notifications.emit_sync", return_value=0):
        apply_plan(workflow_engine, planned)
    with Session(workflow_engine) as db, db.begin():
        assert db.scalar(select(func.count()).select_from(NotificationIntent)) == 0
        run = reconciliation.scan(db, context(db), ScanInput(), uuid.uuid4())
        assert run.summary["counts"]["NOTIFICATION_INTENT_MISSING"] == 1
        assert db.scalar(select(func.count()).select_from(NotificationIntent)) == 0
        finding = db.scalar(
            select(OperationalFinding).where(
                OperationalFinding.run_id == run.id,
                OperationalFinding.category == "NOTIFICATION_INTENT_MISSING",
            )
        )
        assert recovery.plan(db, context(db), "finding", finding.id)["actions"]


def test_worker_restart_evidence_visible_in_timeline(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    from app.services import notifications

    with Session(workflow_engine) as db, db.begin():
        notifications.claim(db, ids[3])
        db.get(NotificationDelivery, delivery_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db:
        row = db.get(NotificationDelivery, delivery_id)
        timeline = ops.timeline(db, context(db), row.correlation_id)
        assert "LEASE_RECOVERED" in {x["status"] for x in timeline["items"]}


def test_connector_outage_automatically_correlates_incident(workflow_engine):
    ids = configured(workflow_engine)
    manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "HTTP_503")
    tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db, db.begin():
        assert db.scalar(select(func.count()).select_from(Incident)) >= 1
        before = db.scalar(select(func.count()).select_from(IncidentEvidence))
        ops.collect_incidents(db, context(db))
        after = db.scalar(select(func.count()).select_from(IncidentEvidence))
        ops.collect_incidents(db, context(db))
        assert (
            db.scalar(select(func.count()).select_from(IncidentEvidence)) == after
            and after >= before
        )

    # One failed calendar action emits two alert types, but it remains one incident.
    data = calendar_input(workflow_engine)
    queue_calendar(workflow_engine, data)
    calendar_tick(workflow_engine)
    request_id = queue_calendar(
        workflow_engine, data.model_copy(update={"request_key": uuid.uuid4()})
    )
    calendar_tick(workflow_engine)
    from app.models.notification import CalendarCoordinationRequest

    with Session(workflow_engine) as db:
        delivery = db.get(CalendarCoordinationRequest, request_id).delivery_id
        cases = list(
            db.scalars(
                select(Incident).where(
                    Incident.target_kind == "delivery", Incident.target_id == delivery
                )
            )
        )
        assert len(cases) == 1 and cases[0].category == "CALENDAR_CONFLICT"


def test_sync_conflict_incident_preserves_authority(workflow_engine):
    _, conflict_id = conflict_setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        ops.collect_incidents(db, ctx)
        row = db.scalar(
            select(Incident).where(
                Incident.target_kind == "conflict", Incident.target_id == conflict_id
            )
        )
        assert row is not None
        preview = recovery.plan(db, ctx, "incident", row.id)
        assert "REVIEW_SYNC_CONFLICT" in {a["action"] for a in preview["actions"]}
        assert preview["runbook_id"] == "sync-conflict"


def test_api_security_and_strict_responses(identity_env):
    env = identity_env
    client = env["client"]
    assert client.get("/api/v1/operations/overview").status_code == 401
    login(env)
    mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["a"].id)})
    assert client.get("/api/v1/operations/overview?hours=24").status_code == 200
    assert client.get("/api/v1/operations/overview?hours=999").status_code == 422
    assert len(client.get("/api/v1/operations/runbooks").json()) == 10
    assert client.post("/api/v1/operations/recovery/preview", json={}).status_code == 403
    assert (
        mutation(
            client,
            "POST",
            "operations/recovery/preview",
            {"target_kind": "connection", "target_id": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    assert client.get("/api/v1/operations/incidents/" + str(uuid.uuid4())).status_code == 404


def test_operations_role_assignment_and_cross_tenant(identity_env, db):
    env = identity_env
    ctx = authorized_context(db, Identity(env["users"]["TENANT_ADMIN"], None), env["a"].id)
    row = ops.observe(
        db,
        ctx.tenant.id,
        "SYNC_INCONSISTENCY",
        "event",
        uuid.uuid4(),
        "event",
        uuid.uuid4(),
        "one",
        "SYNTHETIC_INCONSISTENCY",
        uuid.uuid4(),
    )
    db.commit()
    client = env["client"]
    login(env, "OPERATIONS_USER")
    assert (
        mutation(
            client,
            "PATCH",
            "operations/incidents/" + str(row.id),
            {
                "expected_revision": row.revision,
                "reason": "Assign synthetic incident",
                "assigned_user_id": str(env["users"]["OPERATIONS_USER"].id),
            },
        ).status_code
        == 403
    )
    assert (
        mutation(
            client,
            "PATCH",
            "operations/incidents/" + str(row.id),
            {
                "expected_revision": row.revision,
                "reason": "Acknowledge synthetic incident",
                "acknowledge": True,
            },
        ).status_code
        == 200
    )
    mutation(client, "POST", "auth/logout")
    login(env)
    mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    assert client.get("/api/v1/operations/incidents/" + str(row.id)).status_code == 404
    assert client.get("/api/v1/operations/timeline/" + str(row.correlation_id)).status_code == 404
    assert (
        mutation(
            client,
            "POST",
            "operations/recovery/preview",
            {"target_kind": "incident", "target_id": str(row.id)},
        ).status_code
        == 404
    )


def test_stale_worker_is_not_healthy(workflow_engine):
    from app.models.workflow import WorkerLease
    from app.workflows import engine as worker

    ids = workflow_setup(workflow_engine)
    dispatched(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        worker.claim(db, ids[3])
        db.get(WorkerLease, ids[3]).heartbeat_at = utcnow() - timedelta(minutes=5)
    with Session(workflow_engine) as db:
        result = ops.overview(db, context(db), 24)
        assert result["snapshot"]["worker_health"] == "STALE"
        assert result["snapshot"]["tenant_lease_workers_fresh"] == 0


def test_api_confirm_is_tenant_scoped_idempotent_and_audited(identity_env):
    env = identity_env
    client = env["client"]
    login(env, "OPERATIONS_USER")
    # Low-risk escalation may target an incident even if historical source evidence is gone.
    with Session(env["connection"], join_transaction_mode="create_savepoint") as db, db.begin():
        incident = ops.observe(
            db,
            env["a"].id,
            "SYNC_INCONSISTENCY",
            "event",
            uuid.uuid4(),
            "event",
            uuid.uuid4(),
            "one",
            "SOURCE_EVIDENCE_UNAVAILABLE",
            uuid.uuid4(),
        )
        iid = str(incident.id)
    preview = mutation(
        client, "POST", "operations/recovery/preview", {"target_kind": "incident", "target_id": iid}
    )
    assert preview.status_code == 200
    data = {
        "target_kind": "incident",
        "target_id": iid,
        "action": "ESCALATE_FOR_MANUAL_REVIEW",
        "evidence_revision": preview.json()["evidence_revision"],
        "request_key": str(uuid.uuid4()),
        "reason": "Escalate missing historical synthetic evidence",
    }
    requested = mutation(client, "POST", "operations/recovery/requests", data)
    assert requested.status_code == 201
    rid = requested.json()["id"]
    confirmation = {"evidence_revision": data["evidence_revision"], "confirm": True}
    first = mutation(client, "POST", f"operations/recovery/requests/{rid}/confirm", confirmation)
    assert first.status_code == 200 and first.json()["result"]["outcome"] == "ESCALATED"
    assert (
        mutation(
            client, "POST", f"operations/recovery/requests/{rid}/confirm", confirmation
        ).json()["id"]
        == rid
    )
    assert client.get(f"/api/v1/operations/recovery/requests/{rid}/activity").json()["total"] == 2


def test_approval_rejection_and_request_expiry_audited(workflow_engine):
    execution_id, _ = terminal_workflow(workflow_engine)
    rid, revision = request(workflow_engine, "workflow", execution_id, "REPLAY_WORKFLOW")
    with Session(workflow_engine) as db, db.begin():
        approval = db.scalar(select(RecoveryApproval).where(RecoveryApproval.request_id == rid))
        recovery.approve(
            db,
            reviewer(db),
            approval,
            ApprovalDecision(
                evidence_revision=revision,
                decision="REJECTED",
                reason="Synthetic request needs further investigation",
            ),
        )
    with pytest.raises(HTTPException):
        confirm(workflow_engine, rid, revision)
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    expired, rev = request(workflow_engine, "delivery", delivery_id, "ESCALATE_FOR_MANUAL_REVIEW")
    with Session(workflow_engine) as db, db.begin():
        db.get(RecoveryRequest, expired).expires_at = utcnow() - timedelta(seconds=1)
    assert confirm(workflow_engine, expired, rev) == "EXPIRED"
    with Session(workflow_engine) as db:
        assert db.scalar(
            select(OperationsActivity.id).where(
                OperationsActivity.target_id == expired, OperationsActivity.outcome == "EXPIRED"
            )
        )


def test_concurrent_confirm_executes_once(workflow_engine):
    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    rid, revision = request(workflow_engine, "delivery", delivery_id, "ESCALATE_FOR_MANUAL_REVIEW")
    barrier = Barrier(2)

    def execute(_):
        barrier.wait()
        return confirm(workflow_engine, rid, revision)

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(execute, range(2))) == ["EXECUTED", "EXECUTED"]
    with Session(workflow_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationsActivity)
                .where(
                    OperationsActivity.target_id == rid,
                    OperationsActivity.action == "RECOVERY_CONFIRMED",
                )
            )
            == 1
        )


def test_reconciliation_expired_workflow_lease_no_repair(workflow_engine):
    from app.workflows import engine as worker

    ids = workflow_setup(workflow_engine)
    eid = dispatched(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        worker.claim(db, ids[3])
        db.get(WorkflowExecution, eid).lease_expires_at = utcnow() - timedelta(minutes=1)
    with Session(workflow_engine) as db, db.begin():
        run = reconciliation.scan(db, context(db), ScanInput(), uuid.uuid4())
        assert "WORKER_LEASE_EXPIRED" in run.summary["counts"]
        assert db.get(WorkflowExecution, eid).state == "RUNNING"


def test_reconciliation_orphaned_retry(workflow_engine):
    with Session(workflow_engine) as db:
        slack = fixture_ids(db)[3].id
    ids = workflow_setup(
        workflow_engine,
        [
            {
                "id": "retry",
                "type": "MOCK_CONNECTOR_ACTION",
                "config": {
                    "connection_id": str(slack),
                    "operation": "message.send",
                    "payload": {"channel": "recruiting", "text": "Synthetic retry orphan"},
                    "fault": {"mode": "TRANSIENT", "fail_first_attempts": 1},
                },
                "retry": {"max_attempts": 3},
            }
        ],
    )
    eid = dispatched(workflow_engine, ids)
    workflow_tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db, db.begin():
        execution = db.get(WorkflowExecution, eid)
        assert execution.state == "RETRY_SCHEDULED"
        execution.state = "FAILED"
        db.flush()
        run = reconciliation.scan(db, context(db), ScanInput(), uuid.uuid4())
        assert "ORPHANED_RETRY_SCHEDULE" in run.summary["counts"]


def test_reconciliation_terminal_failure_missing_alert(workflow_engine):
    from unittest.mock import patch

    ids = configured(workflow_engine)
    delivery_id = manual(workflow_engine, ids)
    fault(workflow_engine, ids[2], "HTTP_403")
    with patch("app.services.observability.emit", return_value=None):
        tick(workflow_engine, ids[3])
    with Session(workflow_engine) as db, db.begin():
        assert db.get(NotificationDelivery, delivery_id).status == "FAILED"
        run = reconciliation.scan(db, context(db), ScanInput(), uuid.uuid4())
        assert "TERMINAL_FAILURE_WITHOUT_ALERT" in run.summary["counts"]


def test_reconciliation_does_not_guess_historical_rule_activation(workflow_engine):
    syncids = sync_setup(workflow_engine)
    planned = make_plan(workflow_engine, syncids)
    apply_plan(workflow_engine, planned)
    configured(workflow_engine, trigger="SYNC_CREATE")
    with Session(workflow_engine) as db, db.begin():
        run = reconciliation.scan(db, context(db), ScanInput(), uuid.uuid4())
        assert "NOTIFICATION_INTENT_MISSING" not in run.summary["counts"]
        assert run.summary["historically_indeterminate_rules_skipped"] == 1


def test_unlinked_incident_evidence_is_explicit(workflow_engine):
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        row = ops.observe(
            db,
            ctx.tenant.id,
            "WEBHOOK_INGESTION_FAILURE",
            "receipt",
            uuid.uuid4(),
            "receipt",
            uuid.uuid4(),
            "one",
            "LEGACY_REFERENCE_UNAVAILABLE",
            uuid.uuid4(),
        )
        result = recovery.plan(db, ctx, "incident", row.id)
        assert result["classification"] == "SOURCE_EVIDENCE_UNAVAILABLE"
        assert [a["action"] for a in result["actions"]] == ["ESCALATE_FOR_MANUAL_REVIEW"]


def test_simulated_refresh_is_reviewed_and_masks_secrets(workflow_engine, monkeypatch):
    import base64
    import os

    from test_reliability import oauth_pair

    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    oauth_pair(workflow_engine)
    with Session(workflow_engine) as db:
        connection_id = fixture_ids(db)[2].id
    with Session(workflow_engine) as db:
        preview = recovery.plan(db, context(db), "connection", connection_id)
        action = next(
            a for a in preview["actions"] if a["action"] == "REFRESH_SIMULATED_CREDENTIAL"
        )
        assert action["eligible"] and action["requires_approval"]
        assert "sim_" not in str(preview)
    rid, revision = request(
        workflow_engine, "connection", connection_id, "REFRESH_SIMULATED_CREDENTIAL"
    )
    with Session(workflow_engine) as db, db.begin():
        approval = db.scalar(select(RecoveryApproval).where(RecoveryApproval.request_id == rid))
        recovery.approve(
            db,
            reviewer(db),
            approval,
            ApprovalDecision(
                evidence_revision=revision,
                decision="APPROVED",
                reason="Review synthetic token lifecycle metadata",
            ),
        )
    assert confirm(workflow_engine, rid, revision) == "EXECUTED"
    with Session(workflow_engine) as db:
        assert db.get(RecoveryRequest, rid).result["outcome"] == "SUCCEEDED"
