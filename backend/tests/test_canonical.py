import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_connectors import create
from test_identity import login, mutation

from alembic import command
from app.api.dependencies import Identity, authorized_context
from app.models.canonical import (
    CanonicalRecord,
    ExternalRecordMapping,
    FieldAuthorityRule,
    QuarantinedRecord,
    TransformationAttempt,
    TransformationMapping,
    TransformationMappingVersion,
)
from app.models.identity import AuthSession, Membership, User
from app.models.integration import IntegrationConnection
from app.models.tenant import Tenant
from app.schemas.canonical import (
    ENTITY_MODELS,
    Activity,
    Application,
    Candidate,
    CanonicalEnvelope,
    Client,
    Contact,
    JobRequisition,
    MappingCreate,
    MappingDefinition,
    TransformInput,
    TransformStep,
)
from app.seed_demo import seed_demo
from app.services import canonical as service
from app.transformations.engine import (
    TransformationFailure,
    source_payload,
    template,
    transform,
    validate_definition,
)

TIME = datetime(2030, 1, 15, 12, tzinfo=UTC)
CONTACT = {
    "data": {
        "name": " Synthetic Contact ",
        "email": "CONTACT@EXAMPLE.TEST ",
        "company": " Fictional Harbor ",
    }
}
JOB = {
    "data": {
        "title": "Synthetic Job",
        "department": "Technology",
        "location": "Toronto",
        "status": "OPEN",
    }
}
CANDIDATE = {
    "data": {
        "name": "Synthetic Candidate",
        "email": "candidate@example.test",
        "stage": "SCREENING",
        "skills": ["Python"],
    }
}


@pytest.fixture
def canon_env(identity_env):
    env = identity_env
    assert login(env).status_code == 200
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}
        ).status_code
        == 200
    )
    env["crm"] = create(env, name="Canonical synthetic CRM")
    env["ats"] = create(env, "synthetic_ats", name="Canonical synthetic ATS")
    env["mapping"] = mapping(env, env["crm"], "contact")
    return env


def mapping(env, connection, kind):
    response = mutation(
        env["client"],
        "POST",
        "mappings",
        {"connection_id": connection, "source_kind": kind, "name": "Synthetic " + kind},
    )
    assert response.status_code == 201, response.text
    return response.json()


def normalize(
    env,
    payload=None,
    external_id="source-contact-1",
    updated=TIME,
    mapping_id=None,
    version_id=None,
):
    data = {
        "external_id": external_id,
        "payload": payload if payload is not None else CONTACT,
        "source_updated_at": updated.isoformat() if updated else None,
    }
    if version_id:
        data["mapping_version_id"] = version_id
    return mutation(
        env["client"], "POST", f"mappings/{mapping_id or env['mapping']['id']}/normalize", data
    )


def version(env, mapping_id=None):
    return (
        env["client"]
        .get(f"/api/v1/mappings/{mapping_id or env['mapping']['id']}/versions")
        .json()["items"][0]
    )


@pytest.mark.parametrize(
    "model,data",
    [
        (Client, {"name": "Synthetic Client", "headcount": 10}),
        (
            Contact,
            {"name": "Synthetic Contact", "email": "contact@example.test", "company": "Fictional"},
        ),
        (
            JobRequisition,
            {
                "title": "Synthetic Job",
                "department": "Technology",
                "location": "Toronto",
                "commercial_rate": "100.50",
            },
        ),
        (
            Candidate,
            {"name": "Synthetic Candidate", "email": "candidate@example.test", "stage": "NEW"},
        ),
        (
            Application,
            {
                "candidate_id": str(uuid.uuid4()),
                "job_requisition_id": str(uuid.uuid4()),
                "stage": "SUBMITTED",
            },
        ),
        (
            Activity,
            {
                "kind": "INTERVIEW",
                "summary": "Synthetic",
                "starts_at": "2030-01-15T12:00:00Z",
                "ends_at": "2030-01-15T13:00:00Z",
            },
        ),
    ],
)
def test_six_canonical_contracts(model, data):
    validated = model.model_validate(data)
    assert validated.model_dump(mode="json")
    with pytest.raises(ValidationError):
        model.model_validate({**data, "secret": "forbidden"})


@pytest.mark.parametrize("entity", list(ENTITY_MODELS))
def test_required_canonical_fields(entity):
    with pytest.raises(ValidationError):
        ENTITY_MODELS[entity].model_validate({})


@pytest.mark.parametrize("version_number", [0, 2, "v1"])
def test_unsupported_envelope_versions(version_number):
    with pytest.raises(ValidationError):
        CanonicalEnvelope(
            schema_version=version_number,
            entity_type="client",
            canonical_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            source_connection_id=uuid.uuid4(),
            source_external_id="client-1",
            normalized_at=TIME,
            correlation_id=uuid.uuid4(),
            normalized_data={"name": "Synthetic"},
        )


def test_envelope_entity_and_aware_timestamp_validation():
    data = {
        "schema_version": 1,
        "entity_type": "client",
        "canonical_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "source_connection_id": uuid.uuid4(),
        "source_external_id": "client-1",
        "normalized_at": TIME,
        "correlation_id": uuid.uuid4(),
        "normalized_data": {"name": "Synthetic"},
    }
    assert CanonicalEnvelope(**data).normalized_data["name"] == "Synthetic"
    with pytest.raises(ValidationError):
        CanonicalEnvelope(**{**data, "normalized_at": "2030-01-15T12:00:00"})
    with pytest.raises(ValidationError):
        CanonicalEnvelope(**{**data, "entity_type": "candidate"})


def test_trim_email_nested_extraction_and_determinism():
    definition = template("synthetic_crm", "contact")
    first, _ = transform(definition, "contact", "contact", CONTACT)
    assert first == {
        "name": "Synthetic Contact",
        "email": "contact@example.test",
        "company": "Fictional Harbor",
    }
    assert transform(definition, "contact", "contact", CONTACT)[0] == first
    definition.fields[1].source = "data.profile.email"
    nested = {
        "data": {
            "name": "Synthetic",
            "company": "Fictional",
            "profile": {"email": "NESTED@EXAMPLE.TEST "},
        }
    }
    assert transform(definition, "contact", "contact", nested)[0]["email"] == "nested@example.test"


def test_status_datetime_numeric_normalization():
    definition = template("synthetic_ats", "candidate")
    definition.fields[2].steps[0].values["screening"] = "SCREENING"
    data = {
        "data": {
            **CANDIDATE["data"],
            "stage": "screening",
            "years_experience": " 4.50 ",
            "available_at": "2030-01-15T08:00:00-04:00",
        }
    }
    result, _ = transform(definition, "candidate", "candidate", data)
    assert result["stage"] == "SCREENING" and Decimal(result["years_experience"]) == Decimal("4.50")
    assert result["available_at"] == "2030-01-15T12:00:00Z"


@pytest.mark.parametrize(
    "bad",
    [
        "NaN",
        "Infinity",
        "1e100",
        "1e-999999999",
        "1e+999999999",
        "0e+999999999",
        "0e-999999999",
        "abc",
        True,
    ],
)
def test_numeric_conversion_rejects_nonfinite_or_unsafe(bad):
    definition = template("synthetic_ats", "candidate")
    with pytest.raises(TransformationFailure):
        transform(
            definition,
            "candidate",
            "candidate",
            {"data": {**CANDIDATE["data"], "years_experience": bad}},
        )


def test_null_default_and_bounded_string_operations():
    definition = template("synthetic_crm", "contact")
    definition.fields[2].null_handling = "default"
    definition.fields[2].default = "Fictional Default"
    result, _ = transform(
        definition,
        "contact",
        "contact",
        {"data": {"name": "Synthetic", "email": "a@example.test", "company": None}},
    )
    assert result["company"] == "Fictional Default"
    definition.fields[2].null_handling = "reject"
    with pytest.raises(TransformationFailure):
        transform(
            definition,
            "contact",
            "contact",
            {"data": {"name": "Synthetic", "email": "a@example.test", "company": None}},
        )
    definition.fields[2].steps = [TransformStep(operation="string", max_length=3)]
    with pytest.raises(TransformationFailure):
        transform(definition, "contact", "contact", CONTACT)


@pytest.mark.parametrize("operation", ["eval", "exec", "import", "javascript", "__import__"])
def test_operation_allowlist(operation):
    with pytest.raises(ValidationError):
        MappingDefinition.model_validate(
            {
                "fields": [
                    {"target": "name", "source": "data.name", "steps": [{"operation": operation}]}
                ]
            }
        )


@pytest.mark.parametrize(
    "path", ["__class__", "data.password", "data.__dict__", "data.profile.secret", "tenant_id"]
)
def test_source_path_allowlist(path):
    definition = template("synthetic_crm", "contact")
    definition.fields[0].source = path
    with pytest.raises(TransformationFailure):
        validate_definition(definition, "contact", "contact")


def test_duplicate_unknown_and_missing_targets_reject():
    definition = template("synthetic_crm", "contact")
    definition.fields.append(definition.fields[0].model_copy())
    with pytest.raises(TransformationFailure):
        validate_definition(definition, "contact", "contact")
    definition = template("synthetic_crm", "contact")
    definition.fields[0].target = "access_token"
    with pytest.raises(TransformationFailure):
        validate_definition(definition, "contact", "contact")


def test_source_shape_and_bounds():
    for payload in [
        {"secret": "do-not-store"},
        {"data": {"api_key": "do-not-store"}},
        {"data": {"name": "x" * 17000}},
    ]:
        with pytest.raises(TransformationFailure):
            source_payload("contact", payload)


def test_normalize_persists_identity_provenance_and_duplicate_safety(canon_env, db):
    env = canon_env
    response = normalize(env)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["outcome"] == "SUCCEEDED"
    second = normalize(env).json()
    assert first["id"] == second["id"] and first["canonical_id"] == second["canonical_id"]
    row = db.get(CanonicalRecord, uuid.UUID(first["canonical_id"]))
    assert row.normalized_data["email"] == "contact@example.test" and row.revision == 1
    assert row.field_provenance["name"]["mapping_version_id"] == first["mapping_version_id"]
    identity = db.scalar(
        select(ExternalRecordMapping).where(ExternalRecordMapping.tenant_id == env["a"].id)
    )
    assert identity.canonical_id == row.id and identity.last_source_updated_at == TIME
    assert "CONTACT@EXAMPLE.TEST" not in response.text and "[REDACTED]" in response.text


def test_preview_has_no_canonical_attempt_or_identity_writes(canon_env, db):
    before = {
        model: db.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == canon_env["a"].id)
        )
        for model in [CanonicalRecord, TransformationAttempt, ExternalRecordMapping]
    }
    response = mutation(
        canon_env["client"],
        "POST",
        f"mappings/{canon_env['mapping']['id']}/preview",
        {"external_id": "preview-1", "payload": CONTACT, "source_updated_at": TIME.isoformat()},
    )
    assert response.status_code == 200 and response.json()["persisted"] is False
    for model, count in before.items():
        assert (
            db.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == canon_env["a"].id)
            )
            == count
        )


def test_mapping_versions_immutable_history_and_activation(canon_env, db):
    env = canon_env
    original = version(env)
    attempt = normalize(env).json()
    definition = original["definition"]
    definition["fields"][2]["source"] = None
    definition["fields"][2]["default"] = "Fictional New Company"
    created = mutation(
        env["client"],
        "POST",
        f"mappings/{env['mapping']['id']}/versions",
        {"definition": definition},
    )
    assert created.status_code == 201 and created.json()["version"] == 2
    assert (
        env["client"].get(f"/api/v1/mappings/{env['mapping']['id']}").json()["active_version"] == 1
    )
    activated = mutation(
        env["client"],
        "POST",
        f"mappings/{env['mapping']['id']}/activate",
        {"mapping_version_id": created.json()["id"]},
    )
    assert activated.status_code == 200 and activated.json()["active_version"] == 2
    newer = normalize(env, updated=TIME + timedelta(seconds=1)).json()
    assert newer["mapping_version_id"] == created.json()["id"]
    assert db.get(TransformationAttempt, uuid.UUID(attempt["id"])).mapping_version_id == uuid.UUID(
        original["id"]
    )
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(
            text("UPDATE transformation_mapping_versions SET definition='{}'::jsonb WHERE id=:id"),
            {"id": uuid.UUID(original["id"])},
        )


def test_invalid_mapping_not_activated_or_versioned(canon_env):
    original = version(canon_env)
    definition = original["definition"]
    definition["fields"][0]["source"] = "data.password"
    response = mutation(
        canon_env["client"],
        "POST",
        f"mappings/{canon_env['mapping']['id']}/versions",
        {"definition": definition},
    )
    assert response.status_code == 422
    assert (
        canon_env["client"]
        .get(f"/api/v1/mappings/{canon_env['mapping']['id']}/versions")
        .json()["total"]
        == 1
    )


def test_quarantine_and_corrected_version_reprocessing(canon_env, db):
    env = canon_env
    broken = {"data": {"name": "Synthetic", "email": "a@example.test"}}
    response = normalize(env, broken)
    assert response.status_code == 422 and response.json()["outcome"] == "QUARANTINED"
    invalid = env["client"].get("/api/v1/invalid-records").json()["items"][0]
    definition = version(env)["definition"]
    definition["fields"][2]["null_handling"] = "default"
    definition["fields"][2]["default"] = "Fictional Default"
    corrected = mutation(
        env["client"],
        "POST",
        f"mappings/{env['mapping']['id']}/versions",
        {"definition": definition},
    ).json()
    path = f"invalid-records/{invalid['id']}/reprocess"
    first = mutation(env["client"], "POST", path, {"mapping_version_id": corrected["id"]})
    second = mutation(env["client"], "POST", path, {"mapping_version_id": corrected["id"]})
    assert (
        first.status_code == second.status_code == 200 and first.json()["id"] == second.json()["id"]
    )
    assert (
        env["client"].get(f"/api/v1/invalid-records/{invalid['id']}").json()["status"] == "RESOLVED"
    )
    assert db.get(TransformationAttempt, uuid.UUID(response.json()["id"])).outcome == "QUARANTINED"


@pytest.mark.parametrize(
    "updated,category",
    [
        (TIME - timedelta(seconds=1), "STALE_UPDATE"),
        (TIME, "OUT_OF_ORDER"),
        (None, "MANUAL_REVIEW"),
    ],
)
def test_stale_out_of_order_and_missing_time(canon_env, db, updated, category):
    first = normalize(canon_env).json()
    response = normalize(
        canon_env, {"data": {**CONTACT["data"], "company": "Fictional Changed"}}, updated=updated
    )
    assert response.status_code == 409 and category in {
        issue["category"] for issue in response.json()["issues"]
    }
    row = db.get(CanonicalRecord, uuid.UUID(first["canonical_id"]))
    assert row.normalized_data["company"] == "Fictional Harbor" and row.revision == 1


def test_authority_linked_job_updates_and_conflict(canon_env, db):
    env = canon_env
    crm_job, ats_job = (
        mapping(env, env["crm"], "requisition"),
        mapping(env, env["ats"], "requisition"),
    )
    first = normalize(env, JOB, "crm-job-1", mapping_id=crm_job["id"]).json()
    linked = mutation(
        env["client"],
        "POST",
        "external-identities",
        {
            "canonical_id": first["canonical_id"],
            "connection_id": env["ats"],
            "source_kind": "requisition",
            "external_id": "ats-job-1",
        },
    )
    assert linked.status_code == 201
    ok = normalize(env, JOB, "ats-job-1", mapping_id=ats_job["id"])
    assert ok.status_code == 200
    bad = normalize(
        env,
        {"data": {**JOB["data"], "commercial_rate": "99.00"}},
        "ats-job-1",
        updated=TIME + timedelta(seconds=1),
        mapping_id=ats_job["id"],
    )
    assert bad.status_code == 409 and "AUTHORITY_CONFLICT" in {
        issue["category"] for issue in bad.json()["issues"]
    }
    assert (
        "commercial_rate"
        not in db.get(CanonicalRecord, uuid.UUID(first["canonical_id"])).normalized_data
    )
    altered = normalize(
        env,
        {"data": {**JOB["data"], "title": "Different ATS title"}},
        "ats-job-1",
        updated=TIME + timedelta(seconds=2),
        mapping_id=ats_job["id"],
    )
    assert altered.status_code == 409
    assert env["client"].get("/api/v1/transformation-conflicts").json()["total"] == 2


def test_missing_field_authority_requires_review(canon_env, db):
    rule = db.scalar(
        select(FieldAuthorityRule).where(
            FieldAuthorityRule.tenant_id == canon_env["a"].id,
            FieldAuthorityRule.entity_type == "contact",
            FieldAuthorityRule.field == "company",
        )
    )
    rule.owner = "REVIEW"
    db.flush()
    response = normalize(canon_env)
    assert response.status_code == 409 and response.json()["outcome"] == "MANUAL_REVIEW"


def test_external_identity_collision_and_namespace(canon_env):
    env = canon_env
    first = normalize(env).json()
    second = normalize(env, external_id="source-contact-2").json()
    payload = {
        "canonical_id": second["canonical_id"],
        "connection_id": env["crm"],
        "source_kind": "contact",
        "external_id": "source-contact-1",
    }
    assert mutation(env["client"], "POST", "external-identities", payload).status_code == 409
    other = create(env, name="Another synthetic CRM")
    payload["connection_id"] = other
    payload["canonical_id"] = first["canonical_id"]
    assert mutation(env["client"], "POST", "external-identities", payload).status_code == 201
    assert (
        env["client"]
        .get(
            f"/api/v1/external-identities?connection_id={other}&source_kind=contact&external_id=source-contact-1"
        )
        .json()["total"]
        == 1
    )


def test_tenant_isolation_all_ids_and_identity_links(canon_env):
    env = canon_env
    first = normalize(env).json()
    mapping_id = env["mapping"]["id"]
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    for path in [
        f"mappings/{mapping_id}",
        f"mappings/{mapping_id}/versions",
        f"canonical-records/{first['canonical_id']}",
        f"transformation-attempts/{first['id']}",
    ]:
        assert env["client"].get("/api/v1/" + path).status_code == 404
    assert normalize(env).status_code == 404
    assert (
        mutation(
            env["client"],
            "POST",
            "external-identities",
            {
                "canonical_id": first["canonical_id"],
                "connection_id": env["crm"],
                "source_kind": "contact",
                "external_id": "malicious-link",
            },
        ).status_code
        == 404
    )
    for path in [
        "canonical-records",
        "mappings",
        "external-identities",
        "invalid-records",
        "transformation-attempts",
        "field-authority",
    ]:
        assert env["client"].get("/api/v1/" + path).json()["total"] == 0


@pytest.mark.parametrize("role", ["TENANT_ADMIN", "INTEGRATION_ENGINEER", "OPERATIONS_USER"])
def test_mapping_rbac(canon_env, role):
    env = canon_env
    mutation(env["client"], "POST", "auth/logout")
    login(env, role)
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)})
    assert env["client"].get("/api/v1/mappings").status_code == 200
    assert env["client"].get("/api/v1/canonical-schemas").status_code == 200
    assert normalize(env).status_code == (403 if role == "OPERATIONS_USER" else 200)
    response = mutation(
        env["client"],
        "POST",
        f"mappings/{env['mapping']['id']}/versions",
        {"definition": version(env)["definition"]},
    )
    assert response.status_code == (403 if role == "OPERATIONS_USER" else 201)


def test_unknown_payload_fields_not_logged_or_stored(canon_env, db):
    with patch("app.main.logger.info") as logs:
        response = normalize(canon_env, {"data": {"password": "private-never-store"}})
    assert response.status_code == 422
    row = db.get(TransformationAttempt, uuid.UUID(response.json()["id"]))
    assert row.source_snapshot == {} and "private-never-store" not in response.text
    assert "private-never-store" not in str(logs.call_args_list)


def test_commit_failure_rolls_back_canonical_identity_and_attempt(canon_env, db):
    token = canon_env["client"].get("/api/v1/auth/csrf").json()["csrf_token"]
    with patch.object(Session, "commit", side_effect=RuntimeError("private")):
        response = canon_env["client"].post(
            f"/api/v1/mappings/{canon_env['mapping']['id']}/normalize",
            json={"external_id": "failed-commit-1", "payload": CONTACT},
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": token},
        )
    assert response.status_code == 500
    for model in [CanonicalRecord, ExternalRecordMapping, TransformationAttempt]:
        assert (
            db.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == canon_env["a"].id)
            )
            == 0
        )


def test_database_external_identity_ownership_and_uniqueness(canon_env, db):
    row = normalize(canon_env).json()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            ExternalRecordMapping(
                tenant_id=canon_env["a"].id,
                connection_id=uuid.UUID(canon_env["crm"]),
                source_kind="contact",
                external_id="source-contact-1",
                canonical_id=uuid.UUID(row["canonical_id"]),
                entity_type="contact",
            )
        )
        db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(
            ExternalRecordMapping(
                tenant_id=canon_env["b"].id,
                connection_id=uuid.UUID(canon_env["crm"]),
                source_kind="contact",
                external_id="wrong-tenant-1",
                canonical_id=uuid.UUID(row["canonical_id"]),
                entity_type="contact",
            )
        )
        db.flush()


def test_seed_canonical_fixtures_repeatable(identity_env, db):
    env = identity_env
    args = (db, env["users"]["TENANT_ADMIN"].email, env["password"], "Synthetic")
    seed_demo(*args)
    models = [
        CanonicalRecord,
        TransformationMapping,
        TransformationMappingVersion,
        ExternalRecordMapping,
        TransformationAttempt,
        QuarantinedRecord,
    ]
    before = [db.scalar(select(func.count()).select_from(model)) for model in models]
    seed_demo(*args)
    assert [db.scalar(select(func.count()).select_from(model)) for model in models] == before
    assert before == [20, 8, 8, 22, 24, 4]


@pytest.fixture
def canonical_race_engine(pg_engine):
    # Commit-capable races use a separate test-owned schema, preserving baseline fixture counts.
    schema = "canonical_race_" + uuid.uuid4().hex
    with pg_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        pg_engine.url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=2,
        max_overflow=2,
    )
    try:
        with engine.begin() as connection:
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with pg_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def test_concurrent_normalization_prevents_lost_updates(canonical_race_engine):
    pg_engine = canonical_race_engine
    tenant_id, user_id, connection_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with Session(pg_engine) as db, db.begin():
        db.add(
            Tenant(
                id=tenant_id,
                name="Synthetic canonical race",
                slug="canonical-race-" + uuid.uuid4().hex,
            )
        )
        db.flush()
        db.add(
            User(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="Synthetic",
                password_hash="unused",
            )
        )
        db.flush()
        db.add(Membership(user_id=user_id, tenant_id=tenant_id, role="TENANT_ADMIN"))
        db.add(
            IntegrationConnection(
                id=connection_id,
                tenant_id=tenant_id,
                connector_type="synthetic_crm",
                name="Race CRM",
                public_config={},
            )
        )
        db.flush()
        context = authorized_context(
            db,
            Identity(
                db.get(User, user_id), AuthSession(user_id=user_id, active_tenant_id=tenant_id)
            ),
            tenant_id,
        )
        mapping_row = service.create_mapping(
            db,
            context,
            MappingCreate(connection_id=connection_id, source_kind="contact", name="Race mapping"),
        )
        mapping_id = mapping_row.id
        service.normalize(
            db,
            context,
            mapping_id,
            TransformInput(external_id="race-contact", payload=CONTACT, source_updated_at=TIME),
            uuid.uuid4(),
        )
    barrier = Barrier(2)

    def run(index):
        barrier.wait()
        with Session(pg_engine) as db, db.begin():
            context = authorized_context(
                db,
                Identity(
                    db.get(User, user_id), AuthSession(user_id=user_id, active_tenant_id=tenant_id)
                ),
                tenant_id,
            )
            attempt = service.normalize(
                db,
                context,
                mapping_id,
                TransformInput(
                    external_id="race-contact",
                    source_updated_at=TIME + timedelta(seconds=index + 1),
                    payload={"data": {**CONTACT["data"], "company": f"Fictional {index}"}},
                ),
                uuid.uuid4(),
            )
            return attempt.outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[1] == "SUCCEEDED" and results[0] in {"SUCCEEDED", "CONFLICT"}
    with Session(pg_engine) as db:
        row = db.scalar(select(CanonicalRecord).where(CanonicalRecord.tenant_id == tenant_id))
        assert row.normalized_data["company"] == "Fictional 1"
        assert row.revision in {2, 3}


def test_accepted_event_preview_and_normalization_preserves_inbox(canon_env, db):
    import base64
    import os

    from pydantic import SecretStr

    from app.connectors.webhook_sender import event_bytes, signed_request
    from app.models.webhook import IncomingEvent

    env = canon_env
    env["app"].state.settings.webhook_encryption_key = SecretStr(
        base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    mapping_row = mapping(env, env["crm"], "requisition")
    endpoint = mutation(
        env["client"],
        "POST",
        "webhook-endpoints",
        {"connection_id": env["crm"], "source": "synthetic_crm"},
    ).json()
    body, headers = signed_request(
        endpoint["signing_secret"], event_bytes("synthetic_crm", "job.updated", "canonical-event-1")
    )
    accepted = env["client"].post(
        "/api/v1/webhooks/ingest/" + endpoint["endpoint"]["public_id"],
        content=body,
        headers=headers,
    )
    assert accepted.status_code == 202
    event_id = accepted.json()["event_id"]
    original = dict(db.get(IncomingEvent, uuid.UUID(event_id)).payload)
    preview = mutation(env["client"], "POST", f"events/{event_id}/preview-transformation", {})
    assert preview.status_code == 200 and preview.json()["persisted"] is False
    first = mutation(env["client"], "POST", f"events/{event_id}/normalize", {})
    repeated = mutation(env["client"], "POST", f"events/{event_id}/normalize", {})
    assert (
        first.status_code == repeated.status_code == 200
        and first.json()["id"] == repeated.json()["id"]
    )
    row = db.get(IncomingEvent, uuid.UUID(event_id))
    assert row.payload == original and row.processing_status == "PENDING"
    assert first.json()["event_id"] == event_id
    assert db.get(
        TransformationAttempt, uuid.UUID(first.json()["id"])
    ).mapping_version_id == uuid.UUID(version(env, mapping_row["id"])["id"])
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    assert mutation(env["client"], "POST", f"events/{event_id}/normalize", {}).status_code == 404


def test_numeric_integer_and_optional_null_handling():
    from app.schemas.canonical import TransformStep
    from app.transformations.engine import step

    assert step("2.0", "number", TransformStep(operation="number", numeric_type="integer")) == 2
    with pytest.raises(ValueError):
        step("2.5", "number", TransformStep(operation="number", numeric_type="integer"))
    definition = template("synthetic_crm", "contact")
    definition.fields[3].null_handling = "preserve"
    output, _ = transform(definition, "contact", "contact", CONTACT)
    assert output["last_contacted_at"] is None


def test_unknown_enum_and_naive_date_rejected():
    with pytest.raises(TransformationFailure):
        transform(
            template("synthetic_ats", "candidate"),
            "candidate",
            "candidate",
            {"data": {**CANDIDATE["data"], "stage": "UNSUPPORTED"}},
        )
    with pytest.raises(TransformationFailure):
        transform(
            template("synthetic_crm", "contact"),
            "contact",
            "contact",
            {"data": {**CONTACT["data"], "last_contacted_at": "2030-01-15T12:00:00"}},
        )


def test_draft_version_cannot_bypass_stale_source_time(canon_env):
    first = normalize(canon_env)
    assert first.status_code == 200
    definition = version(canon_env)["definition"]
    draft = mutation(
        canon_env["client"],
        "POST",
        f"mappings/{canon_env['mapping']['id']}/versions",
        {"definition": definition},
    ).json()
    response = normalize(canon_env, updated=TIME - timedelta(seconds=1), version_id=draft["id"])
    assert response.status_code == 409 and response.json()["outcome"] == "CONFLICT"


def test_normalization_mutations_require_csrf_and_origin(canon_env):
    client = canon_env["client"]
    assert (
        client.post(f"/api/v1/mappings/{canon_env['mapping']['id']}/normalize", json={}).status_code
        == 403
    )
    assert (
        mutation(
            client,
            "POST",
            f"mappings/{canon_env['mapping']['id']}/normalize",
            {"external_id": "source-1", "payload": CONTACT},
            Origin="https://attacker.example.test",
        ).status_code
        == 403
    )


def test_enum_mapping_values_validated_before_activation():
    definition = template("synthetic_ats", "candidate")
    definition.fields[2].steps[0].values["NEW"] = "UNSUPPORTED"
    with pytest.raises(TransformationFailure):
        validate_definition(definition, "candidate", "candidate")


def test_preserved_null_can_clear_owned_optional_field(canon_env, db):
    env = canon_env
    original = version(env)
    definition = original["definition"]
    definition["fields"][3]["null_handling"] = "preserve"
    draft = mutation(
        env["client"],
        "POST",
        f"mappings/{env['mapping']['id']}/versions",
        {"definition": definition},
    ).json()
    first = normalize(
        env,
        {"data": {**CONTACT["data"], "last_contacted_at": TIME.isoformat()}},
        version_id=draft["id"],
    ).json()
    cleared = normalize(
        env,
        {"data": {**CONTACT["data"], "last_contacted_at": None}},
        updated=TIME + timedelta(seconds=1),
        version_id=draft["id"],
    )
    assert cleared.status_code == 200
    assert (
        db.get(CanonicalRecord, uuid.UUID(first["canonical_id"])).normalized_data[
            "last_contacted_at"
        ]
        is None
    )


def test_newer_confirmation_advances_field_time_without_revision(canon_env, db):
    first = normalize(canon_env).json()
    later = normalize(canon_env, updated=TIME + timedelta(seconds=10))
    assert later.status_code == 200 and later.json()["outcome"] == "NO_CHANGE"
    row = db.get(CanonicalRecord, uuid.UUID(first["canonical_id"]))
    assert row.revision == 1
    assert datetime.fromisoformat(
        row.field_provenance["company"]["source_updated_at"]
    ) == TIME + timedelta(seconds=10)


def test_canonical_metadata_is_bounded_and_synthetic():
    from app.schemas.canonical import CanonicalMetadata

    assert CanonicalMetadata(tags=["portfolio"]).synthetic is True
    with pytest.raises(ValidationError):
        CanonicalMetadata(synthetic=False)
    with pytest.raises(ValidationError):
        CanonicalMetadata(tags=["x" * 41])
    with pytest.raises(ValidationError):
        CanonicalMetadata(secret="forbidden")
