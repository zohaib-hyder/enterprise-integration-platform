"""Workflow security and real PostgreSQL dispatch/worker/checkpoint recovery."""

import base64
import copy
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from alembic.config import Config
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_identity import login, mutation

from alembic import command
from app.api.dependencies import Identity, authorized_context
from app.connectors.contracts import ConnectorError, ErrorCategory
from app.core.security import utcnow
from app.models.identity import AuditEvent, Membership, User
from app.models.integration import IntegrationConnection, SyntheticNotification
from app.models.tenant import Tenant
from app.models.webhook import IncomingEvent, WebhookEndpoint
from app.models.workflow import (
    DeadLetterRecord,
    RetrySchedule,
    WorkflowActionReceipt,
    WorkflowDefinition,
    WorkflowDispatchRecord,
    WorkflowExecution,
    WorkflowExecutionEvent,
    WorkflowStepExecution,
    WorkflowVersion,
)
from app.schemas.workflow import RetryPolicy, WorkflowCreate, WorkflowJSON
from app.seed_demo import seed_demo
from app.services import workflows as service
from app.workflows import engine as worker


def plan(conn=None, steps=None):
    return {
        "schema_version": 1,
        "trigger": {
            "connection_id": str(conn or uuid.uuid4()),
            "source": "synthetic_crm",
            "event_types": ["job.updated"],
        },
        "steps": steps or [{"id": "result", "type": "RECORD_RESULT", "config": {}}],
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema_version=2),
        lambda p: p["steps"][0].update(type="PYTHON"),
        lambda p: p["steps"][0]["config"].update(script="print(1)"),
        lambda p: p["steps"].append(copy.deepcopy(p["steps"][0])),
        lambda p: p["steps"][0].update(timeout_seconds=100),
        lambda p: p["steps"][0].update(retry={"max_attempts": 100}),
        lambda p: p["steps"][0]["config"].update(
            canonical_id={"source": "event", "path": "tenant_id"}
        ),
        lambda p: p["steps"][0]["config"].update(
            canonical_id={"source": "step", "step_id": "future", "path": "canonical_id"}
        ),
        lambda p: p["steps"][0].update(output_schema="eval"),
    ],
)
def test_schema_rejects_unsafe_and_unbounded_definitions(change):
    p = plan()
    change(p)
    with pytest.raises(ValidationError):
        WorkflowJSON.model_validate(p)


def test_valid_schema_references_and_all_step_types():
    p = plan(
        steps=[
            {
                "id": "normalize",
                "type": "NORMALIZE_EVENT",
                "config": {"mapping_version_id": str(uuid.uuid4())},
            },
            {
                "id": "transform",
                "type": "TRANSFORM",
                "config": {"mapping_version_id": str(uuid.uuid4())},
            },
            {
                "id": "condition",
                "type": "CONDITION",
                "config": {
                    "reference": {"source": "step", "step_id": "normalize", "path": "canonical_id"},
                    "operator": "EXISTS",
                },
            },
            {"id": "delay", "type": "DELAY", "config": {"seconds": 1}},
            {
                "id": "action",
                "type": "MOCK_CONNECTOR_ACTION",
                "config": {
                    "connection_id": str(uuid.uuid4()),
                    "operation": "message.send",
                    "payload": {"channel": "recruiting", "text": "Synthetic"},
                },
            },
            {"id": "result", "type": "RECORD_RESULT", "config": {}},
        ]
    )
    assert len(WorkflowJSON.model_validate(p).steps) == 6


@pytest.mark.parametrize("attempt,expected", [(1, 2), (2, 4), (3, 8), (4, 10), (5, 10)])
def test_backoff_exponential_and_cap(attempt, expected):
    assert (
        worker.backoff(
            RetryPolicy(base_delay_seconds=2, max_delay_seconds=10, jitter_fraction=0), attempt
        )
        == expected
    )


def test_jitter_and_retry_after_are_bounded():
    policy = RetryPolicy(base_delay_seconds=2, max_delay_seconds=10, jitter_fraction=0.3)
    assert worker.backoff(policy, 1, random_value=1) == 2.6
    assert worker.backoff(policy, 1, retry_after=8, random_value=0) == 8
    assert worker.backoff(policy, 1, retry_after=500) == 10


@pytest.mark.parametrize(
    "category,retry",
    [
        (ErrorCategory.TIMEOUT, True),
        (ErrorCategory.RATE_LIMITED, True),
        (ErrorCategory.TEMPORARY_UNAVAILABLE, True),
        (ErrorCategory.AUTHORIZATION_FAILED, False),
        (ErrorCategory.VALIDATION_FAILED, False),
        (ErrorCategory.CONFLICT, False),
    ],
)
def test_retry_classification(category, retry):
    assert worker.classify(ConnectorError(category)).retryable == retry


def test_legal_transitions_reject_terminal_reactivation():
    row = WorkflowExecution(state="SUCCEEDED")
    with pytest.raises(ValueError):
        service.transition(row, "RUNNING")
    step = WorkflowStepExecution(state="SUCCEEDED")
    with pytest.raises(ValueError):
        service.transition(step, "PENDING")


@pytest.fixture
def workflow_engine(pg_engine):
    schema = "workflow_test_" + uuid.uuid4().hex
    with pg_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        pg_engine.url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=4,
        max_overflow=2,
    )
    try:
        with engine.begin() as connection:
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with Session(engine) as db, db.begin():
            seed_demo(
                db, "workflow-admin@example.test", secrets.token_urlsafe(24), "Synthetic operator"
            )
        yield engine
    finally:
        engine.dispose()
        with pg_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def fixture_ids(db):
    tenant = db.scalar(select(Tenant).where(Tenant.slug == "demo-recruiting-alpha"))
    user = db.scalar(select(User).where(User.email == "workflow-admin@example.test"))
    crm = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.tenant_id == tenant.id,
            IntegrationConnection.connector_type == "synthetic_crm",
        )
    )
    slack = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.tenant_id == tenant.id,
            IntegrationConnection.connector_type == "synthetic_slack",
        )
    )
    return tenant, user, crm, slack


def setup(engine, step_specs=None, event_type="job.updated", event_prefix=None):
    with Session(engine) as db, db.begin():
        tenant, user, crm, slack = fixture_ids(db)
        context = authorized_context(db, Identity(user, None), tenant.id)
        if step_specs == "slack":
            step_specs = [
                {
                    "id": "action",
                    "type": "MOCK_CONNECTOR_ACTION",
                    "config": {
                        "connection_id": str(slack.id),
                        "operation": "message.send",
                        "payload": {"channel": "recruiting", "text": "Synthetic workflow test"},
                    },
                },
                {"id": "result", "type": "RECORD_RESULT", "config": {}},
            ]
        definition = plan(crm.id, step_specs)
        if event_prefix:
            definition["trigger"]["event_id_prefix"] = event_prefix
        wf = service.create(
            db, context, WorkflowCreate(name="Test " + uuid.uuid4().hex, definition=definition)
        )
        service.activate(db, context, wf, True)
        endpoint = WebhookEndpoint(
            tenant_id=tenant.id,
            connection_id=crm.id,
            source="synthetic_crm",
            public_id=secrets.token_urlsafe(32),
        )
        db.add(endpoint)
        db.flush()
        event = IncomingEvent(
            tenant_id=tenant.id,
            endpoint_id=endpoint.id,
            connection_id=crm.id,
            source="synthetic_crm",
            provider_event_id="test-" + uuid.uuid4().hex,
            event_type=event_type,
            schema_version=1,
            payload={
                "schema_version": 1,
                "source": "synthetic_crm",
                "event_id": "test-event",
                "event_type": event_type,
                "external_id": "demo-job-1",
                "occurred_at": "2030-01-15T12:00:00Z",
                "data": {
                    "title": "Synthetic",
                    "department": "Technology",
                    "location": "Toronto",
                    "status": "OPEN",
                },
            },
            payload_digest="a" * 64,
            occurred_at=utcnow(),
            correlation_id=uuid.uuid4(),
        )
        db.add(event)
        db.flush()
        worker_id = worker.register_worker(db)
        return tenant.id, wf.id, event.id, worker_id


def dispatched(engine, ids):
    with Session(engine) as db, db.begin():
        assert worker.dispatch(db, ids[0]) == 1
        execution = db.scalar(
            select(WorkflowExecution).where(WorkflowExecution.workflow_id == ids[1])
        )
        return execution.id


def tick(engine, worker_id):
    with Session(engine) as db, db.begin():
        job = worker.claim(db, worker_id)
    if job:
        worker.run_claim(engine, job)
    return job


def test_dispatch_duplicate_no_match_and_trigger_filter(workflow_engine):
    engine = workflow_engine
    setup(engine, event_prefix="unmatched-")
    with Session(engine) as db, db.begin():
        assert worker.dispatch(db) == 0
        assert db.scalar(select(WorkflowDispatchRecord.state)) == "NO_MATCH"
        assert worker.dispatch(db) == 0
    ids2 = setup(engine)
    execution_id = dispatched(engine, ids2)
    with Session(engine) as db, db.begin():
        assert worker.dispatch(db) == 0
        assert db.scalar(select(func.count()).select_from(WorkflowExecution)) == 1
        assert (
            db.get(WorkflowExecution, execution_id).correlation_id
            == db.get(IncomingEvent, ids2[2]).correlation_id
        )


def test_concurrent_dispatch_exactly_one_execution(workflow_engine):
    ids = setup(workflow_engine)
    barrier = Barrier(2)

    def dispatch_once():
        with Session(workflow_engine) as db, db.begin():
            barrier.wait()
            return worker.dispatch(db, ids[0])

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(lambda _: dispatch_once(), range(2))) == 1
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(WorkflowExecution)) == 1


def test_concurrent_worker_claim_skip_locked(workflow_engine):
    ids = setup(workflow_engine)
    dispatched(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        other_worker = worker.register_worker(db)
    barrier = Barrier(2)

    def claim_once(w):
        with Session(workflow_engine) as db, db.begin():
            barrier.wait()
            return worker.claim(db, w)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim_once, [ids[3], other_worker]))
    assert sum(r is not None for r in results) == 1


def test_stale_lease_fencing_restart_and_completed_checkpoint(workflow_engine):
    engine = workflow_engine
    ids = setup(engine, "slack")
    execution_id = dispatched(engine, ids)
    assert tick(engine, ids[3])
    with Session(engine) as db, db.begin():
        stale = worker.claim(db, ids[3])
        db.get(WorkflowExecution, execution_id).lease_expires_at = utcnow() - timedelta(seconds=1)
        new_worker = worker.register_worker(db)
    with Session(engine) as db, db.begin():
        fresh = worker.claim(db, new_worker)
    assert fresh[1] != stale[1]
    with Session(engine) as db, db.begin():
        assert not worker.perform(db, *stale)
    worker.run_claim(engine, fresh)
    with Session(engine) as db:
        execution = db.get(WorkflowExecution, execution_id)
        assert execution.state == "SUCCEEDED"
        assert service.steps(db, execution)[0].attempt_count == 1
        assert db.scalar(select(func.count()).select_from(WorkflowActionReceipt)) == 1
        # seed notification + exactly one test notification
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(SyntheticNotification.tenant_id == ids[0])
            )
            == 2
        )
        assert "LEASE_RECOVERED" in list(db.scalars(select(WorkflowExecutionEvent.event_type)))


def test_retry_exhaustion_dead_letter_and_checkpoint_replay(workflow_engine):
    engine = workflow_engine
    ids = setup(engine, "slack")
    with Session(engine) as db, db.begin():
        wf = db.get(WorkflowDefinition, ids[1])
        context = authorized_context(db, Identity(fixture_ids(db)[1], None), ids[0])
        p = plan(
            fixture_ids(db)[2].id,
            [
                {
                    "id": "action",
                    "type": "MOCK_CONNECTOR_ACTION",
                    "config": {
                        "connection_id": str(fixture_ids(db)[3].id),
                        "operation": "message.send",
                        "payload": {"channel": "recruiting", "text": "Synthetic once"},
                    },
                },
                {
                    "id": "fault",
                    "type": "MOCK_CONNECTOR_ACTION",
                    "config": {
                        "connection_id": str(fixture_ids(db)[3].id),
                        "operation": "message.send",
                        "payload": {"channel": "recruiting", "text": "Synthetic retry"},
                        "fault": {"mode": "TRANSIENT", "fail_first_attempts": 2},
                    },
                    "retry": {"max_attempts": 2, "jitter_fraction": 0},
                },
                {"id": "result", "type": "RECORD_RESULT", "config": {}},
            ],
        )
        version = service.add_version(db, context, wf, WorkflowJSON.model_validate(p))
        service.activate(db, context, wf, True, version.id)
    execution_id = dispatched(engine, ids)
    tick(engine, ids[3])
    tick(engine, ids[3])
    with Session(engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        assert row.state == "RETRY_SCHEDULED"
        row.next_run_at = utcnow()
        assert db.scalar(select(RetrySchedule.state)) == "SCHEDULED"
    tick(engine, ids[3])
    with Session(engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        assert row.state == "DEAD_LETTERED"
        assert db.scalar(select(func.count()).select_from(DeadLetterRecord)) == 1
        context = authorized_context(db, Identity(fixture_ids(db)[1], None), ids[0])
        replay = service.replay(db, context, execution_id)
        replay_id = replay.id
        assert service.replay(db, context, execution_id).id == replay_id
    tick(engine, ids[3])
    tick(engine, ids[3])
    with Session(engine) as db:
        row = db.get(WorkflowExecution, replay_id)
        assert row.state == "SUCCEEDED"
        assert service.steps(db, row)[0].attempt_count == 0
        assert db.scalar(select(func.count()).select_from(WorkflowActionReceipt)) == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(SyntheticNotification.tenant_id == ids[0])
            )
            == 3
        )
        assert db.get(WorkflowExecution, execution_id).state == "DEAD_LETTERED"


def test_delay_wait_and_cancel(workflow_engine):
    engine = workflow_engine
    ids = setup(
        engine,
        [
            {"id": "delay", "type": "DELAY", "config": {"seconds": 300}},
            {"id": "result", "type": "RECORD_RESULT", "config": {}},
        ],
    )
    execution_id = dispatched(engine, ids)
    tick(engine, ids[3])
    with Session(engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        assert row.state == "WAITING" and row.lease_token is None
        assert worker.claim(db, ids[3]) is None
        context = authorized_context(db, Identity(fixture_ids(db)[1], None), ids[0])
        assert service.cancel(db, context, execution_id).state == "CANCELLED"
        with pytest.raises(HTTPException):
            service.replay(db, context, execution_id)


def test_running_cancel_cooperative(workflow_engine):
    engine = workflow_engine
    ids = setup(engine)
    execution_id = dispatched(engine, ids)
    with Session(engine) as db, db.begin():
        job = worker.claim(db, ids[3])
    with Session(engine) as db, db.begin():
        context = authorized_context(db, Identity(fixture_ids(db)[1], None), ids[0])
        assert service.cancel(db, context, execution_id).state == "RUNNING"
    worker.run_claim(engine, job)
    with Session(engine) as db:
        assert db.get(WorkflowExecution, execution_id).state == "CANCELLED"


def test_immutable_version_and_trigger_database_enforced(workflow_engine):
    ids = setup(workflow_engine)
    for table in ["workflow_versions", "workflow_triggers"]:
        with Session(workflow_engine) as db, pytest.raises(DBAPIError):
            db.execute(
                text(f"UPDATE {table} SET updated_at = now() WHERE workflow_id = :id"),
                {"id": ids[1]},
            )


def test_unknown_receipt_replay_refused_and_principal_revocation(workflow_engine):
    engine = workflow_engine
    ids = setup(engine, "slack")
    execution_id = dispatched(engine, ids)
    tick(engine, ids[3])
    with Session(engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        # Model an uncertain future external action; API cannot manufacture this marker.
        db.scalar(select(WorkflowActionReceipt)).state = "UNKNOWN"
        row.state = "FAILED"
        context = authorized_context(db, Identity(fixture_ids(db)[1], None), ids[0])
        with pytest.raises(HTTPException):
            service.replay(db, context, execution_id)
        context.membership.status = "INACTIVE"
    tick(engine, ids[3])
    with Session(engine) as db:
        context_user = fixture_ids(db)[1]
        with pytest.raises(HTTPException):
            authorized_context(db, Identity(context_user, None), ids[0])


def test_api_rbac_tenant_csrf_and_audit_redaction(identity_env):
    env = identity_env
    assert login(env).status_code == 200
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)})
    from test_connectors import create

    conn = create(env)
    response = mutation(
        env["client"],
        "POST",
        "workflows",
        {"name": "Synthetic safe workflow", "definition": plan(conn)},
    )
    assert response.status_code == 201, response.text
    wf = response.json()["id"]
    assert (
        env["client"]
        .post("/api/v1/workflows/" + wf + "/activate", json={"enabled": True})
        .status_code
        == 403
    )
    assert (
        mutation(
            env["client"], "POST", "workflows/" + wf + "/activate", {"enabled": True}
        ).status_code
        == 200
    )
    assert env["client"].get("/api/v1/workflows/" + wf + "/versions").status_code == 200
    assert login(env, "OPERATIONS_USER").status_code == 200
    assert env["client"].get("/api/v1/workflows").status_code == 200
    assert (
        mutation(
            env["client"], "POST", "workflows", {"name": "Denied", "definition": plan(conn)}
        ).status_code
        == 403
    )
    assert mutation(env["client"], "POST", "workflow-dispatch").status_code == 403
    assert login(env).status_code == 200
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    assert env["client"].get("/api/v1/workflows/" + wf).status_code == 404
    with Session(env["connection"]) as db:
        audits = list(db.scalars(select(AuditEvent).where(AuditEvent.action.like("workflow.%"))))
        assert audits and all(a.details == {} for a in audits)


def test_expired_leases_exhaust_attempts_without_illegal_transition(workflow_engine):
    engine = workflow_engine
    ids = setup(
        engine,
        [{"id": "result", "type": "RECORD_RESULT", "config": {}, "retry": {"max_attempts": 1}}],
    )
    execution_id = dispatched(engine, ids)
    with Session(engine) as db, db.begin():
        assert worker.claim(db, ids[3])
        db.get(WorkflowExecution, execution_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    with Session(engine) as db, db.begin():
        assert worker.claim(db, ids[3]) is None
        assert db.get(WorkflowExecution, execution_id).state == "DEAD_LETTERED"
        assert db.scalar(select(DeadLetterRecord.category)) == "LEASE_EXHAUSTED"


def test_principal_revocation_blocks_action_without_side_effect(workflow_engine):
    engine = workflow_engine
    ids = setup(engine, "slack")
    execution_id = dispatched(engine, ids)
    with Session(engine) as db, db.begin():
        tenant, user, _, _ = fixture_ids(db)
        member = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id, Membership.tenant_id == tenant.id
            )
        )
        member.status = "INACTIVE"
    tick(engine, ids[3])
    with Session(engine) as db:
        assert db.get(WorkflowExecution, execution_id).error_category == "AUTHORIZATION_FAILED"
        assert db.scalar(select(func.count()).select_from(WorkflowActionReceipt)) == 0


def test_condition_skips_remaining_and_transform_preview_is_checkpointed(workflow_engine):
    engine = workflow_engine
    ids = setup(
        engine,
        [
            {
                "id": "condition",
                "type": "CONDITION",
                "config": {
                    "reference": {"source": "event", "path": "data.status"},
                    "expected": "CLOSED",
                },
            },
            {"id": "result", "type": "RECORD_RESULT", "config": {}},
        ],
    )
    execution_id = dispatched(engine, ids)
    tick(engine, ids[3])
    with Session(engine) as db:
        row = db.get(WorkflowExecution, execution_id)
        assert row.state == "SUCCEEDED" and service.steps(db, row)[1].state == "SKIPPED"


def test_multiple_workflows_and_rescan_are_explicit(workflow_engine):
    engine = workflow_engine
    ids = setup(engine, event_prefix="not-yet-")
    with Session(engine) as db, db.begin():
        assert worker.dispatch(db) == 0
        wf = db.get(WorkflowDefinition, ids[1])
        tenant, user, crm, _ = fixture_ids(db)
        context = authorized_context(db, Identity(user, None), tenant.id)
        version = service.add_version(db, context, wf, WorkflowJSON.model_validate(plan(crm.id)))
        service.activate(db, context, wf, True, version.id)
        service.activate(
            db,
            context,
            service.create(
                db, context, WorkflowCreate(name="Second observer", definition=plan(crm.id))
            ),
            True,
        )
        assert worker.dispatch(db) == 0
        assert worker.dispatch(db, rescan=True) == 2
        assert worker.dispatch(db, rescan=True) == 0


def test_seed_engine_runs_real_success_retry_and_terminal_scenarios(workflow_engine, monkeypatch):
    from app.seed_workflows import run_demo

    # A fresh checkout has no operator key; use one ephemeral key for both runs.
    monkeypatch.setenv(
        "WEBHOOK_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    result = run_demo(workflow_engine, "workflow-admin@example.test")
    assert result["workflow_states"] == {"DEAD_LETTERED": 2, "SUCCEEDED": 4}
    assert result == run_demo(workflow_engine, "workflow-admin@example.test")
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(RetrySchedule)) == 2
        assert db.scalar(select(func.count()).select_from(WorkflowActionReceipt)) == 4
        assert db.scalar(select(func.count()).select_from(DeadLetterRecord)) == 2
        assert all(
            s.state in {"SUCCEEDED", "FAILED", "PENDING"}
            for s in db.scalars(select(WorkflowStepExecution))
        )


def test_receipt_digest_mismatch_and_lost_completed_receipt_block_replay(workflow_engine):
    engine = workflow_engine
    ids = setup(engine, "slack")
    execution_id = dispatched(engine, ids)
    tick(engine, ids[3])
    with Session(engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        receipt = db.scalar(select(WorkflowActionReceipt))
        receipt.result = {"schema_version": 1, "synthetic": True, "data": {"status": "UNKNOWN"}}
        row.state = "FAILED"
        context = authorized_context(db, Identity(fixture_ids(db)[1], None), ids[0])
        with pytest.raises(HTTPException):
            service.replay(db, context, execution_id)
        db.rollback()


def test_timeout_fault_retries_and_fail_behavior(workflow_engine):
    engine = workflow_engine
    with Session(engine) as db:
        conn = fixture_ids(db)[3].id
    ids = setup(
        engine,
        [
            {
                "id": "action",
                "type": "MOCK_CONNECTOR_ACTION",
                "config": {
                    "connection_id": str(conn),
                    "operation": "message.send",
                    "payload": {"channel": "recruiting", "text": "Synthetic timeout"},
                    "fault": {
                        "mode": "TIMEOUT",
                        "fail_first_attempts": 1,
                        "retry_after_seconds": 2,
                    },
                },
                "retry": {"max_attempts": 1},
                "failure_behavior": "FAIL",
            }
        ],
    )
    execution_id = dispatched(engine, ids)
    tick(engine, ids[3])
    with Session(engine) as db:
        assert db.get(WorkflowExecution, execution_id).state == "FAILED"
        assert db.get(WorkflowExecution, execution_id).error_category == "TIMEOUT"
        assert db.scalar(select(func.count()).select_from(DeadLetterRecord)) == 0


def test_transformation_preview_and_normalize_pin_source_mapping(workflow_engine):
    from app.models.canonical import TransformationMapping, TransformationMappingVersion
    from app.models.integration import SyntheticCRMRecord

    engine = workflow_engine
    with Session(engine) as db:
        _, _, crm, _ = fixture_ids(db)
        mapping = db.scalar(
            select(TransformationMapping).where(
                TransformationMapping.connection_id == crm.id,
                TransformationMapping.source_kind == "requisition",
            )
        )
        version = db.scalar(
            select(TransformationMappingVersion).where(
                TransformationMappingVersion.mapping_id == mapping.id
            )
        )
        version_id = str(version.id)
        payload = db.scalar(
            select(SyntheticCRMRecord.payload).where(
                SyntheticCRMRecord.connection_id == crm.id,
                SyntheticCRMRecord.external_id == "demo-job-1",
            )
        )
    ids = setup(
        engine,
        [
            {"id": "preview", "type": "TRANSFORM", "config": {"mapping_version_id": version_id}},
            {
                "id": "normalize",
                "type": "NORMALIZE_EVENT",
                "config": {"mapping_version_id": version_id},
            },
        ],
    )
    with Session(engine) as db, db.begin():
        event = db.get(IncomingEvent, ids[2])
        event.payload = {**event.payload, "data": payload}
    execution_id = dispatched(engine, ids)
    tick(engine, ids[3])
    tick(engine, ids[3])
    with Session(engine) as db:
        row = db.get(WorkflowExecution, execution_id)
        assert row.state == "SUCCEEDED"
        checkpoints = service.steps(db, row)
        assert checkpoints[0].output["transformation_attempt_id"] is None
        assert checkpoints[1].output["transformation_attempt_id"]
        assert checkpoints[0].output["canonical_id"] == checkpoints[1].output["canonical_id"]


def test_operations_cancel_and_replay_permission_without_definition_editing(identity_env):
    from test_connectors import create

    env = identity_env
    assert login(env).status_code == 200
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)})
    conn_id = create(env)
    created = mutation(
        env["client"],
        "POST",
        "workflows",
        {"name": "Recovery permission test", "definition": plan(conn_id)},
    ).json()
    with Session(env["connection"], join_transaction_mode="create_savepoint") as db, db.begin():
        endpoint = WebhookEndpoint(
            tenant_id=env["a"].id,
            connection_id=uuid.UUID(conn_id),
            source="synthetic_crm",
            public_id=secrets.token_urlsafe(32),
        )
        db.add(endpoint)
        db.flush()
        event = IncomingEvent(
            tenant_id=env["a"].id,
            connection_id=uuid.UUID(conn_id),
            endpoint_id=endpoint.id,
            source="synthetic_crm",
            provider_event_id="recovery-event",
            event_type="job.updated",
            schema_version=1,
            payload={},
            payload_digest="c" * 64,
            occurred_at=utcnow(),
            correlation_id=uuid.uuid4(),
        )
        db.add(event)
        db.flush()
        version = db.scalar(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == created["id"])
        )
        row = WorkflowExecution(
            id=uuid.uuid4(),
            tenant_id=env["a"].id,
            workflow_id=uuid.UUID(created["id"]),
            version_id=version.id,
            event_id=event.id,
            connection_id=uuid.UUID(conn_id),
            principal_id=env["users"]["TENANT_ADMIN"].id,
            correlation_id=uuid.uuid4(),
            next_run_at=utcnow(),
        )
        row.root_id = row.id
        db.add(row)
        db.flush()
        db.add(
            WorkflowStepExecution(
                tenant_id=row.tenant_id,
                execution_id=row.id,
                step_id="result",
                position=0,
                step_type="RECORD_RESULT",
            )
        )
        execution_id = str(row.id)
    assert login(env, "OPERATIONS_USER").status_code == 200
    response = env["client"].get("/api/v1/executions/" + execution_id)
    assert response.status_code == 200 and "lease_token" not in response.json()
    assert (
        mutation(
            env["client"], "POST", f"executions/{execution_id}/replay", {"confirmed": True}
        ).status_code
        == 409
    )
    assert (
        mutation(env["client"], "POST", f"executions/{execution_id}/cancel").json()["state"]
        == "CANCELLED"
    )
    with Session(env["connection"], join_transaction_mode="create_savepoint") as db, db.begin():
        row = db.get(WorkflowExecution, uuid.UUID(execution_id))
        row.state = "FAILED"
        service.steps(db, row)[0].state = "FAILED"
    assert (
        mutation(
            env["client"], "POST", f"executions/{execution_id}/replay", {"confirmed": True}
        ).status_code
        == 201
    )
    assert (
        mutation(
            env["client"], "POST", f"workflows/{created['id']}/activate", {"enabled": True}
        ).status_code
        == 403
    )
    assert login(env).status_code == 200
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    for suffix in ["", "/history", "/retries"]:
        assert env["client"].get("/api/v1/executions/" + execution_id + suffix).status_code == 404
    assert mutation(env["client"], "POST", f"executions/{execution_id}/cancel").status_code == 404


def test_real_postgresql_statement_timeout_classified_without_action(workflow_engine, monkeypatch):
    from app.connectors.registry import registry

    engine = workflow_engine
    with Session(engine) as db:
        conn = fixture_ids(db)[3].id
    ids = setup(
        engine,
        [
            {
                "id": "action",
                "type": "MOCK_CONNECTOR_ACTION",
                "timeout_seconds": 1,
                "retry": {"max_attempts": 1},
                "config": {
                    "connection_id": str(conn),
                    "operation": "message.send",
                    "payload": {"channel": "recruiting", "text": "Synthetic bounded SQL"},
                },
            }
        ],
    )
    execution_id = dispatched(engine, ids)

    def slow_database(db, *_):
        db.execute(text("SELECT pg_sleep(2)"))
        raise AssertionError("Statement timeout did not interrupt database work")

    monkeypatch.setattr(registry.resolve("synthetic_slack"), "perform", slow_database)
    tick(engine, ids[3])
    with Session(engine) as db:
        assert db.get(WorkflowExecution, execution_id).error_category == "TIMEOUT"
        assert db.scalar(select(func.count()).select_from(WorkflowActionReceipt)) == 0


def test_rescan_does_not_starve_matching_event_behind_unmatched_backlog(workflow_engine):
    engine = workflow_engine
    ids = setup(engine, event_prefix="not-matching-")
    with Session(engine) as db, db.begin():
        original = db.get(IncomingEvent, ids[2])
        original.provider_event_id = "target-event"
        for i in range(30):
            db.add(
                IncomingEvent(
                    tenant_id=original.tenant_id,
                    connection_id=original.connection_id,
                    endpoint_id=original.endpoint_id,
                    source=original.source,
                    provider_event_id=f"unmatched-{i}",
                    event_type=original.event_type,
                    schema_version=1,
                    payload=original.payload,
                    payload_digest=original.payload_digest,
                    occurred_at=original.occurred_at,
                    correlation_id=uuid.uuid4(),
                )
            )
        db.flush()
        assert worker.dispatch(db, limit=100) == 0
        tenant, user, crm, _ = fixture_ids(db)
        context = authorized_context(db, Identity(user, None), tenant.id)
        definition = plan(crm.id)
        definition["trigger"]["event_id_prefix"] = "target-"
        wf = db.get(WorkflowDefinition, ids[1])
        version = service.add_version(db, context, wf, WorkflowJSON.model_validate(definition))
        service.activate(db, context, wf, True, version.id)
        assert worker.dispatch(db, limit=1, rescan=True) == 1
        assert db.scalar(select(WorkflowExecution.event_id)) == original.id


def test_static_action_fields_validate_even_when_other_fields_are_bound(workflow_engine):
    with Session(workflow_engine) as db, db.begin():
        tenant, user, crm, slack = fixture_ids(db)
        context = authorized_context(db, Identity(user, None), tenant.id)
        definition = plan(
            crm.id,
            [
                {
                    "id": "action",
                    "type": "MOCK_CONNECTOR_ACTION",
                    "config": {
                        "connection_id": str(slack.id),
                        "operation": "message.send",
                        "payload": {"channel": "arbitrary-channel"},
                        "bindings": {"text": {"source": "event", "path": "data.title"}},
                    },
                }
            ],
        )
        with pytest.raises(HTTPException):
            service.validate_definition(db, context, WorkflowJSON.model_validate(definition))
        definition["steps"][0]["config"]["payload"]["channel"] = "recruiting"
        service.validate_definition(db, context, WorkflowJSON.model_validate(definition))
        email = db.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.tenant_id == tenant.id,
                IntegrationConnection.connector_type == "synthetic_email",
            )
        )
        definition["steps"][0]["config"] = {
            "connection_id": str(email.id),
            "operation": "message.send",
            "payload": {
                "recipient": "recruiter@example.test",
                "subject": "Synthetic\ninvalid subject",
            },
            "bindings": {"body": {"source": "event", "path": "data.title"}},
        }
        with pytest.raises(HTTPException):
            service.validate_definition(db, context, WorkflowJSON.model_validate(definition))


def test_nonfinite_action_json_rejected_by_schema():
    definition = plan(
        steps=[
            {
                "id": "action",
                "type": "MOCK_CONNECTOR_ACTION",
                "config": {
                    "connection_id": str(uuid.uuid4()),
                    "operation": "requisition.list",
                    "payload": {"limit": float("nan")},
                },
            }
        ]
    )
    with pytest.raises(ValidationError):
        WorkflowJSON.model_validate(definition)
