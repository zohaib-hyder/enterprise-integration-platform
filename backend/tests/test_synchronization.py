# ruff: noqa: F811
"""Phase 7 planner and real PostgreSQL integration."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_identity import login, mutation
from test_workflows import fixture_ids, tick, workflow_engine  # noqa: F401

from app.api.dependencies import Identity, authorized_context
from app.models.canonical import FieldAuthorityRule, TransformationMappingVersion
from app.models.integration import IntegrationConnection, SyntheticCRMRecord
from app.models.synchronization import (
    SyncConfiguration,
    SyncConfigurationVersion,
    SyncConflict,
    SyncExecution,
    SyncOperation,
    SyncPlan,
)
from app.models.webhook import IncomingEvent, WebhookEndpoint
from app.models.workflow import WorkflowExecution
from app.schemas.canonical import MappingCreate
from app.schemas.synchronization import (
    PlanPreview,
    ReconcileRequest,
    SyncActivation,
    SyncCreate,
    SyncDefinition,
)
from app.services import canonical
from app.services import synchronization as sync
from app.workflows import engine as worker

FIELDS = {"title": "title", "department": "department", "location": "location"}
SOURCE = {
    "title": "Synthetic sync job",
    "department": "Technology",
    "location": "Toronto",
    "status": "OPEN",
}
OWNERS = {k: "synthetic_crm" for k in FIELDS}


def pure(**kwargs):
    return sync.calculate(
        source=SOURCE,
        destination_data=kwargs.pop("destination_data", None),
        owners=kwargs.pop("owners", OWNERS),
        fields=FIELDS,
        baseline=kwargs.pop("baseline", None),
        **kwargs,
    )


@pytest.mark.parametrize(
    "dest,decision", [(None, "CREATE"), ({**SOURCE, "title": "Old"}, "UPDATE"), (SOURCE, "NO_OP")]
)
def test_planner_decisions(dest, decision):
    assert pure(destination_data=dest)["decision"] == decision


def test_planner_preserves_ats_status():
    result = pure(destination_data={**SOURCE, "title": "Old", "status": "CLOSED"})
    assert (
        result["preserved_destination_fields"]["status"] == "CLOSED"
        and "status" not in result["proposed_changes"]
    )


@pytest.mark.parametrize(
    "kwargs,category",
    [
        ({"stale": True}, "STALE_SOURCE"),
        ({"collision": True}, "IDENTITY_COLLISION"),
        ({"ambiguous": True}, "AMBIGUOUS_MATCH"),
        ({"owners": {"title": "REVIEW"}}, "FIELD_AUTHORITY_VIOLATION"),
        (
            {"destination_data": {**SOURCE, "title": "ATS edit"}, "baseline": SOURCE},
            "DESTINATION_CHANGED",
        ),
    ],
)
def test_planner_holds(kwargs, category):
    result = pure(**kwargs)
    assert result["decision"] == "CONFLICT" and category in result["conflict_reasons"]


@pytest.mark.parametrize(
    "change",
    [
        {"destination_fields": {"status": "status"}},
        {"entity_type": "candidate"},
        {"match_rule": "FUZZY"},
        {"conflict_policy": "LAST_WRITE_WINS"},
        {"trigger_event_types": []},
    ],
)
def test_configuration_rejects_unsafe(change):
    with pytest.raises(ValidationError):
        SyncDefinition(mapping_version_id=uuid.uuid4(), **change)


def context(db):
    tenant, user, _, _ = fixture_ids(db)
    return authorized_context(db, Identity(user, None), tenant.id)


def setup(engine, timezone_offset_hours=0):
    with Session(engine) as db, db.begin():
        ctx = context(db)
        _, _, crm, _ = fixture_ids(db)
        ats = db.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.tenant_id == ctx.tenant.id,
                IntegrationConnection.connector_type == "synthetic_ats",
            )
        )
        from app.models.canonical import TransformationMapping

        mapping = db.scalar(
            select(TransformationMapping).where(
                TransformationMapping.connection_id == crm.id,
                TransformationMapping.source_kind == "requisition",
            )
        )
        if mapping is None:
            mapping = canonical.create_mapping(
                db,
                ctx,
                MappingCreate(
                    connection_id=crm.id, source_kind="requisition", name="Sync requisitions"
                ),
            )
        mv = db.scalar(
            select(TransformationMappingVersion).where(
                TransformationMappingVersion.mapping_id == mapping.id
            )
        )
        cfg = sync.create(
            db,
            ctx,
            SyncCreate(
                name="CRM ATS",
                source_connection_id=crm.id,
                destination_connection_id=ats.id,
                definition=SyncDefinition(mapping_version_id=mv.id),
            ),
        )
        sync.activate(db, ctx, cfg, SyncActivation(enabled=True, expected_revision=cfg.revision))
        source = SyntheticCRMRecord(
            tenant_id=ctx.tenant.id,
            connection_id=crm.id,
            kind="requisition",
            external_id="sync-test-job",
            payload=SOURCE,
        )
        endpoint = WebhookEndpoint(
            tenant_id=ctx.tenant.id,
            connection_id=crm.id,
            source="synthetic_crm",
            public_id=uuid.uuid4().hex,
        )
        db.add_all([source, endpoint])
        db.flush()
        event = IncomingEvent(
            tenant_id=ctx.tenant.id,
            connection_id=crm.id,
            endpoint_id=endpoint.id,
            source="synthetic_crm",
            provider_event_id=uuid.uuid4().hex,
            event_type="job.created",
            schema_version=1,
            payload={
                "schema_version": 1,
                "source": "synthetic_crm",
                "event_id": uuid.uuid4().hex,
                "event_type": "job.created",
                "external_id": source.external_id,
                "occurred_at": source.updated_at.astimezone(
                    timezone(timedelta(hours=timezone_offset_hours))
                ).isoformat(),
                "data": SOURCE,
            },
            payload_digest="b" * 64,
            occurred_at=source.updated_at,
            correlation_id=uuid.uuid4(),
        )
        db.add(event)
        db.flush()
        return cfg.id, event.id, worker.register_worker(db)


def make_plan(engine, ids):
    with Session(engine) as db, db.begin():
        ctx = context(db)
        cfg = sync.scoped(db, ctx, SyncConfiguration, ids[0])
        version = sync.scoped(db, ctx, SyncConfigurationVersion, cfg.active_version_id)
        row, execution = sync.plan(
            db, ctx, cfg, version, PlanPreview(event_id=ids[1]), uuid.uuid4(), normalize=True
        )
        return row.id, execution.id


def apply_plan(engine, ids):
    with Session(engine) as db, db.begin():
        ctx = context(db)
        return sync.apply(
            db,
            ctx,
            sync.scoped(db, ctx, SyncPlan, ids[0]),
            sync.scoped(db, ctx, SyncExecution, ids[1]),
        ).state


def test_real_create_identity_and_idempotency(workflow_engine):
    ids = setup(workflow_engine)
    planned = make_plan(workflow_engine, ids)
    assert (
        apply_plan(workflow_engine, planned) == "SUCCEEDED"
        and apply_plan(workflow_engine, planned) == "SUCCEEDED"
    )
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1
        op = db.scalar(select(SyncOperation))
        links = sync.identities(db, context(db), op.destination_connection_id, op.canonical_id)
        assert (
            len(links) == 1
            and sync.destination(
                db, context(db), op.destination_connection_id, links[0].external_id
            ).payload["title"]
            == SOURCE["title"]
        )


def test_versions_and_plans_are_database_immutable(workflow_engine):
    ids = setup(workflow_engine)
    make_plan(workflow_engine, ids)
    for table in ["sync_configuration_versions", "sync_plans"]:
        with pytest.raises(DBAPIError), workflow_engine.begin() as conn:
            conn.execute(text(f"UPDATE {table} SET updated_at=now()"))


def test_concurrent_create_suppression(workflow_engine):
    ids = setup(workflow_engine)
    planned = make_plan(workflow_engine, ids)
    barrier = Barrier(2)

    def concurrent(_):
        barrier.wait()
        return apply_plan(workflow_engine, planned)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(concurrent, range(2))) == ["SUCCEEDED", "SUCCEEDED"]
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1


def test_apply_rechecks_authority(workflow_engine):
    ids = setup(workflow_engine)
    planned = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        rule = db.scalar(
            select(FieldAuthorityRule).where(
                FieldAuthorityRule.tenant_id == context(db).tenant.id,
                FieldAuthorityRule.entity_type == "job_requisition",
                FieldAuthorityRule.field == "title",
            )
        )
        rule.owner = "REVIEW"
    assert apply_plan(workflow_engine, planned) == "HELD"
    with Session(workflow_engine) as db:
        assert db.scalar(select(SyncConflict.category)) == "FIELD_AUTHORITY_VIOLATION"
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 0


def test_existing_engine_dispatch_and_duplicate_suppression(workflow_engine):
    ids = setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        assert worker.dispatch(db) == 1 and worker.dispatch(db) == 0
    for _ in range(3):
        assert tick(workflow_engine, ids[2])
    with Session(workflow_engine) as db:
        assert db.scalar(select(WorkflowExecution.state)) == "SUCCEEDED"
        assert db.scalar(select(SyncExecution.state)) == "SUCCEEDED"
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1


def test_reconciliation_is_read_only_and_real(workflow_engine):
    ids = setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        cfg = sync.scoped(db, ctx, SyncConfiguration, ids[0])
        run = sync.reconcile(db, ctx, cfg, ReconcileRequest())
        assert run.summary["counts"]["MISSING_DESTINATION"] >= 1
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 0
        assert db.scalar(select(func.count()).select_from(SyncPlan)) == 0


def test_tenant_reference_constraint(workflow_engine):
    ids = setup(workflow_engine)
    with Session(workflow_engine) as db:
        cfg = db.get(SyncConfiguration, ids[0])
        from app.models.tenant import Tenant

        other = db.scalar(select(Tenant).where(Tenant.id != cfg.tenant_id))
        other_id = other.id
    with pytest.raises(IntegrityError), workflow_engine.begin() as conn:
        conn.execute(
            text("UPDATE sync_configurations SET tenant_id=:tenant WHERE id=:id"),
            {"tenant": other_id, "id": ids[0]},
        )


def test_api_rbac_origin_and_empty_states(identity_env):
    env = identity_env
    client = env["client"]
    assert login(env, "OPERATIONS_USER").status_code == 200
    assert client.get("/api/v1/sync/configurations").json()["items"] == []
    assert client.get("/api/v1/sync/status").status_code == 200
    assert mutation(client, "POST", "sync/configurations", {}).status_code == 403
    assert (
        client.post("/api/v1/sync/conflicts/" + str(uuid.uuid4()) + "/resolve", json={}).status_code
        == 403
    )
    assert client.get("/api/v1/sync/configurations/" + str(uuid.uuid4())).status_code == 404


def revise_source(engine, ids, title="Updated synthetic job", older=False):
    from datetime import timedelta

    from app.core.security import utcnow

    with Session(engine) as db, db.begin():
        ctx = context(db)
        cfg = sync.scoped(db, ctx, SyncConfiguration, ids[0])
        old = db.get(IncomingEvent, ids[1])
        source = sync.current_source(db, ctx, cfg, "sync-test-job")
        occurred = source.updated_at - timedelta(seconds=10) if older else utcnow()
        if not older:
            source.payload = {**SOURCE, "title": title}
            db.flush()
            occurred = source.updated_at
        payload = {
            **old.payload,
            "event_id": uuid.uuid4().hex,
            "event_type": "job.updated",
            "occurred_at": occurred.isoformat(),
            "data": {**SOURCE, "title": title},
        }
        event = IncomingEvent(
            tenant_id=ctx.tenant.id,
            connection_id=cfg.source_connection_id,
            endpoint_id=old.endpoint_id,
            source="synthetic_crm",
            provider_event_id=payload["event_id"],
            event_type="job.updated",
            schema_version=1,
            payload=payload,
            payload_digest="c" * 64,
            occurred_at=occurred,
            correlation_id=uuid.uuid4(),
        )
        db.add(event)
        db.flush()
        return ids[0], event.id, ids[2]


def test_real_update_noop_and_status_preservation(workflow_engine):
    ids = setup(workflow_engine)
    assert apply_plan(workflow_engine, make_plan(workflow_engine, ids)) == "SUCCEEDED"
    with Session(workflow_engine) as db, db.begin():
        op = db.scalar(select(SyncOperation))
        dest = sync.destination(
            db,
            context(db),
            op.destination_connection_id,
            db.scalar(select(SyncPlan)).evidence["destination_identity"]["external_id"],
        )
        dest.payload = {**dest.payload, "status": "CLOSED"}
    updated = revise_source(workflow_engine, ids)
    planned = make_plan(workflow_engine, updated)
    with Session(workflow_engine) as db:
        assert db.get(SyncPlan, planned[0]).decision == "UPDATE"
    assert apply_plan(workflow_engine, planned) == "SUCCEEDED"
    with Session(workflow_engine) as db:
        op = db.scalar(select(SyncOperation).order_by(SyncOperation.created_at.desc()))
        assert op.result["destination_data"]["status"] == "CLOSED"
    no_op = make_plan(workflow_engine, updated)
    with Session(workflow_engine) as db:
        assert db.get(SyncPlan, no_op[0]).decision == "NO_OP"


def test_real_stale_source_is_held(workflow_engine):
    ids = setup(workflow_engine)
    apply_plan(workflow_engine, make_plan(workflow_engine, ids))
    stale = revise_source(workflow_engine, ids, older=True)
    planned = make_plan(workflow_engine, stale)
    assert apply_plan(workflow_engine, planned) == "HELD"
    with Session(workflow_engine) as db:
        assert "STALE_SOURCE" in db.get(SyncPlan, planned[0]).evidence["conflict_reasons"]
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1


def conflict_setup(engine):
    ids = setup(engine)
    apply_plan(engine, make_plan(engine, ids))
    with Session(engine) as db, db.begin():
        op = db.scalar(select(SyncOperation))
        plan = db.scalar(select(SyncPlan))
        dest = sync.destination(
            db,
            context(db),
            op.destination_connection_id,
            plan.evidence["destination_identity"]["external_id"],
        )
        dest.payload = {**dest.payload, "title": "Recruiter edit"}
    updated = revise_source(engine, ids)
    planned = make_plan(engine, updated)
    with Session(engine) as db:
        conflict = db.scalar(select(SyncConflict).where(SyncConflict.plan_id == planned[0]))
        assert conflict.category == "DESTINATION_CHANGED"
        return updated, conflict.id


def resolution_request(db, ctx, conflict, action="ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS"):
    from app.schemas.synchronization import Resolution

    cfg = sync.scoped(
        db, ctx, SyncConfiguration, db.get(SyncPlan, conflict.plan_id).configuration_id
    )
    plan = db.get(SyncPlan, conflict.plan_id)
    return Resolution(
        action=action,
        reason="Reviewed synthetic source and destination",
        expected_revision=conflict.revision,
        expected_source_digest=sync.destination_digest(
            sync.current_source(db, ctx, cfg, "sync-test-job")
        ),
        expected_destination_digest=sync.destination_digest(
            sync.destination(
                db,
                ctx,
                cfg.destination_connection_id,
                plan.evidence["destination_identity"]["external_id"],
            )
        ),
        request_key=uuid.uuid4(),
    )


def test_resolution_replans_and_queues_existing_worker(workflow_engine):
    ids, conflict_id = conflict_setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        conflict = sync.scoped(db, ctx, SyncConflict, conflict_id)
        request = resolution_request(db, ctx, conflict)
        resolution = sync.resolve(db, ctx, conflict, request, uuid.uuid4())
        assert sync.resolve(db, ctx, conflict, request, uuid.uuid4()).id == resolution.id
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1
    for _ in range(2):
        assert tick(workflow_engine, ids[2])
    with Session(workflow_engine) as db:
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 2
        assert db.get(SyncConflict, conflict_id).status == "RESOLVED"


def test_resolution_optimistic_source_and_destination(workflow_engine):
    _, conflict_id = conflict_setup(workflow_engine)
    from fastapi import HTTPException

    for field in ["expected_source_digest", "expected_destination_digest", "expected_revision"]:
        with Session(workflow_engine) as db, db.begin():
            ctx = context(db)
            conflict = sync.scoped(db, ctx, SyncConflict, conflict_id)
            request = resolution_request(db, ctx, conflict)
            request = request.model_copy(
                update={field: 999 if field == "expected_revision" else "0" * 64}
            )
            with pytest.raises(HTTPException) as error:
                sync.resolve(db, ctx, conflict, request, uuid.uuid4())
            assert error.value.status_code == 409


def test_manual_resolution_cannot_override_authority(workflow_engine):
    _, conflict_id = conflict_setup(workflow_engine)
    from fastapi import HTTPException

    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        rule = db.scalar(
            select(FieldAuthorityRule).where(
                FieldAuthorityRule.tenant_id == ctx.tenant.id,
                FieldAuthorityRule.entity_type == "job_requisition",
                FieldAuthorityRule.field == "title",
            )
        )
        rule.owner = "REVIEW"
        db.flush()
        conflict = sync.scoped(db, ctx, SyncConflict, conflict_id)
        with pytest.raises(HTTPException):
            sync.resolve(db, ctx, conflict, resolution_request(db, ctx, conflict), uuid.uuid4())
        assert db.scalar(select(func.count()).select_from(SyncOperation)) == 1


def test_transient_failure_retains_plan_and_retries_without_duplicate(workflow_engine):
    from app.core.security import utcnow
    from app.models.workflow import RetrySchedule
    from app.schemas.reliability import FailureConfig
    from app.services import reliability

    ids = setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        cfg = db.get(SyncConfiguration, ids[0])
        reliability.configure_fault(
            db,
            context(db),
            cfg.destination_connection_id,
            FailureConfig(enabled=True, scenario="HTTP_503", attempts=1, ttl_seconds=300),
        )
        assert worker.dispatch(db) == 1
    assert tick(workflow_engine, ids[2])
    assert tick(workflow_engine, ids[2])
    with Session(workflow_engine) as db, db.begin():
        wf = db.scalar(select(WorkflowExecution))
        assert wf.state == "RETRY_SCHEDULED"
        assert db.scalar(select(func.count()).select_from(RetrySchedule)) == 1
        wf.next_run_at = utcnow()
    assert tick(workflow_engine, ids[2])
    assert tick(workflow_engine, ids[2])
    with Session(workflow_engine) as db:
        assert (
            db.scalar(select(WorkflowExecution.state)) == "SUCCEEDED"
            and db.scalar(select(func.count()).select_from(SyncOperation)) == 1
        )


def test_identity_collision_suppresses_create(workflow_engine):
    from app.models.canonical import ExternalRecordMapping

    ids = setup(workflow_engine)
    planned = make_plan(workflow_engine, ids)
    from app.models.integration import SyntheticATSRecord

    with Session(workflow_engine) as db, db.begin():
        plan = db.get(SyncPlan, planned[0])
        cfg = db.get(SyncConfiguration, ids[0])
        db.add(
            SyntheticATSRecord(
                tenant_id=cfg.tenant_id,
                connection_id=cfg.destination_connection_id,
                kind="requisition",
                external_id=plan.evidence["destination_identity"]["external_id"],
                payload={**SOURCE, "title": "Unlinked existing ATS job", "status": "CLOSED"},
            )
        )
    occupied = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db:
        assert "IDENTITY_COLLISION" in db.get(SyncPlan, occupied[0]).evidence["conflict_reasons"]
    assert apply_plan(workflow_engine, occupied) == "HELD"
    with Session(workflow_engine) as db, db.begin():
        plan = db.get(SyncPlan, planned[0])
        cfg = db.get(SyncConfiguration, ids[0])
        db.add_all(
            [
                ExternalRecordMapping(
                    tenant_id=cfg.tenant_id,
                    connection_id=cfg.destination_connection_id,
                    source_kind="requisition",
                    external_id=ext,
                    canonical_id=plan.canonical_id,
                    entity_type="job_requisition",
                    provenance="MANUAL_LINK",
                )
                for ext in ["one", "two"]
            ]
        )
    second = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db:
        assert "IDENTITY_COLLISION" in db.get(SyncPlan, second[0]).evidence["conflict_reasons"]
    assert apply_plan(workflow_engine, second) == "HELD"


def test_unknown_receipt_outcome_is_held_without_write(workflow_engine):
    ids = setup(workflow_engine)
    planned = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        plan = db.get(SyncPlan, planned[0])
        execution = db.get(SyncExecution, planned[1])
        cfg = db.get(SyncConfiguration, ids[0])
        db.add(
            SyncOperation(
                tenant_id=cfg.tenant_id,
                execution_id=execution.id,
                plan_id=plan.id,
                canonical_id=plan.canonical_id,
                destination_connection_id=cfg.destination_connection_id,
                action_key="sync:" + execution.business_key,
                outcome="UNKNOWN",
                result={},
            )
        )
    assert apply_plan(workflow_engine, planned) == "HELD"
    with Session(workflow_engine) as db:
        assert db.scalar(select(SyncConflict.category)) == "UNKNOWN_WRITE_OUTCOME"
    refreshed = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db:
        assert (
            "UNKNOWN_WRITE_OUTCOME" in db.get(SyncPlan, refreshed[0]).evidence["conflict_reasons"]
        )


def test_source_changes_after_plan_are_held(workflow_engine):
    ids = setup(workflow_engine)
    planned = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db, db.begin():
        cfg = db.get(SyncConfiguration, ids[0])
        source = sync.current_source(db, context(db), cfg, "sync-test-job")
        source.payload = {**SOURCE, "title": "Concurrent CRM edit"}
    assert apply_plan(workflow_engine, planned) == "HELD"


def test_origin_authority_is_connection_specific(workflow_engine):
    from app.models.canonical import CanonicalRecord

    ids = setup(workflow_engine)
    planned = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db:
        record = db.get(CanonicalRecord, db.get(SyncPlan, planned[0]).canonical_id)
        owners = sync.authority(db, context(db), record, uuid.uuid4())
        assert owners["title"] == "ORIGIN"
        assert pure(owners=owners)["decision"] == "CONFLICT"


def test_activation_requires_current_revision(workflow_engine):
    from fastapi import HTTPException

    ids = setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        cfg = db.get(SyncConfiguration, ids[0])
        with pytest.raises(HTTPException) as error:
            sync.activate(db, context(db), cfg, SyncActivation(enabled=False, expected_revision=1))
        assert error.value.status_code == 409 and cfg.enabled


@pytest.mark.parametrize("candidate_count,decision", [(1, "NO_OP"), (2, "CONFLICT")])
def test_explicit_preexisting_match_is_bounded_and_ambiguity_safe(
    workflow_engine, candidate_count, decision
):
    from app.models.integration import SyntheticATSRecord

    ids = setup(workflow_engine)
    with Session(workflow_engine) as db, db.begin():
        ctx = context(db)
        cfg = db.get(SyncConfiguration, ids[0])
        definition = SyncDefinition.model_validate(
            db.get(SyncConfigurationVersion, cfg.active_version_id).definition
        ).model_copy(update={"match_rule": "EXACT_TITLE_DEPARTMENT_LOCATION"})
        version = sync.add_version(db, ctx, cfg, definition)
        sync.activate(
            db,
            ctx,
            cfg,
            SyncActivation(enabled=True, expected_revision=cfg.revision, version_id=version.id),
        )
        db.add_all(
            [
                SyntheticATSRecord(
                    tenant_id=ctx.tenant.id,
                    connection_id=cfg.destination_connection_id,
                    kind="requisition",
                    external_id=f"preexisting-{n}",
                    payload={**SOURCE, "status": "CLOSED"},
                )
                for n in range(candidate_count)
            ]
        )
    planned = make_plan(workflow_engine, ids)
    with Session(workflow_engine) as db:
        assert db.get(SyncPlan, planned[0]).decision == decision
    assert apply_plan(workflow_engine, planned) == ("SUCCEEDED" if candidate_count == 1 else "HELD")


def test_api_configuration_versions_review_permissions_and_tenant_denial(identity_env):
    from test_connectors import create as create_connection

    env = identity_env
    client = env["client"]
    assert login(env).status_code == 200
    assert (
        mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}).status_code
        == 200
    )
    crm = create_connection(env, name="CRM sync API")
    ats = create_connection(env, kind="synthetic_ats", name="ATS sync API")
    mapping = mutation(
        client,
        "POST",
        "mappings",
        {"connection_id": crm, "source_kind": "requisition", "name": "Job mapping"},
    ).json()
    version = client.get(f"/api/v1/mappings/{mapping['id']}/versions").json()["items"][0]
    response = mutation(
        client,
        "POST",
        "sync/configurations",
        {
            "name": "API sync",
            "source_connection_id": crm,
            "destination_connection_id": ats,
            "definition": {"mapping_version_id": version["id"]},
        },
    )
    assert response.status_code == 201
    cfg = response.json()
    definition = {"mapping_version_id": version["id"], "create_enabled": False}
    assert (
        mutation(
            client, "POST", f"sync/configurations/{cfg['id']}/versions", definition
        ).status_code
        == 201
    )
    assert client.get(f"/api/v1/sync/configurations/{cfg['id']}/versions").json()["total"] == 2
    assert (
        mutation(
            client,
            "POST",
            f"sync/configurations/{cfg['id']}/activate",
            {"enabled": True, "expected_revision": cfg["revision"]},
        ).status_code
        == 200
    )
    assert (
        mutation(
            client,
            "POST",
            f"sync/configurations/{cfg['id']}/activate",
            {"enabled": False, "expected_revision": cfg["revision"]},
        ).status_code
        == 409
    )
    assert (
        mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["b"].id)}).status_code
        == 200
    )
    assert client.get(f"/api/v1/sync/configurations/{cfg['id']}").status_code == 404
    assert (
        mutation(
            client,
            "POST",
            "sync/configurations",
            {
                "name": "Foreign route",
                "source_connection_id": crm,
                "destination_connection_id": ats,
                "definition": {"mapping_version_id": version["id"]},
            },
        ).status_code
        == 404
    )
    assert (
        mutation(client, "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}).status_code
        == 200
    )
    assert (
        mutation(
            client,
            "POST",
            f"connections/{crm}/execute",
            {
                "name": "requisition.upsert",
                "payload": {"external_id": "api-sync-job", "data": SOURCE},
            },
        ).status_code
        == 200
    )
    with Session(env["connection"], join_transaction_mode="create_savepoint") as db, db.begin():
        rule = db.scalar(
            select(FieldAuthorityRule).where(
                FieldAuthorityRule.tenant_id == env["a"].id,
                FieldAuthorityRule.entity_type == "job_requisition",
                FieldAuthorityRule.field == "title",
            )
        )
        rule.owner = "REVIEW"
    preview = mutation(
        client,
        "POST",
        f"sync/configurations/{cfg['id']}/preview",
        {"source_external_id": "api-sync-job"},
    )
    assert preview.status_code == 200 and preview.json()["plan"]["decision"] == "CONFLICT"
    conflict = client.get("/api/v1/sync/conflicts").json()["items"][0]
    assert login(env, "OPERATIONS_USER").status_code == 200
    assert client.get(f"/api/v1/sync/configurations/{cfg['id']}").status_code == 200
    review = client.get(f"/api/v1/sync/conflicts/{conflict['id']}").json()
    resolution = {
        "action": "ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS",
        "reason": "Reviewed synthetic state without authority override",
        "expected_revision": conflict["revision"],
        "expected_source_digest": review["current_source_digest"],
        "expected_destination_digest": review["current_destination_digest"],
        "request_key": str(uuid.uuid4()),
    }
    assert (
        mutation(client, "POST", f"sync/conflicts/{conflict['id']}/resolve", resolution).status_code
        == 403
    )
    resolution["action"] = "KEEP_DESTINATION"
    kept = mutation(client, "POST", f"sync/conflicts/{conflict['id']}/resolve", resolution)
    assert kept.status_code == 200
    assert (
        mutation(client, "POST", f"sync/conflicts/{conflict['id']}/resolve", resolution).json()[
            "id"
        ]
        == kept.json()["id"]
    )

    assert (
        mutation(client, "POST", f"sync/configurations/{cfg['id']}/reconcile", {}).status_code
        == 403
    )
