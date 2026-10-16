"""Real PostgreSQL reliability, encrypted inventory, isolation and concurrency."""

import base64
import copy
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from threading import Barrier

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session
from test_identity import login, mutation
from test_workflows import dispatched, fixture_ids, setup, tick, workflow_engine  # noqa: F401

from app.api.dependencies import Identity, authorized_context
from app.connectors.contracts import ConnectorOperation, ErrorCategory
from app.connectors.reliability import ReliabilityError, parse_retry_after
from app.core.config import Settings
from app.core.security import utcnow
from app.models.identity import AuditEvent
from app.models.integration import IntegrationConnection, SyntheticNotification
from app.models.reliability import (
    AlertEvent,
    CircuitBreaker,
    CircuitTransition,
    ConnectionCredential,
    ConnectorObservation,
    CredentialRefreshAttempt,
    FailureInjection,
    OperationalAlert,
    RateLimitBucket,
)
from app.models.tenant import Tenant
from app.models.workflow import (
    RetrySchedule,
    WorkflowActionReceipt,
    WorkflowExecution,
)
from app.schemas.reliability import Budget, CredentialSubmission, FailureConfig, ReliabilityPolicy
from app.services import credentials, integrations, observability, reliability
from app.workflows import engine as worker


@pytest.fixture
def rel_engine(request, monkeypatch):
    engine = request.getfixturevalue("workflow_engine")
    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    with Session(engine) as db, db.begin():
        db.execute(delete(ConnectorObservation))
        db.execute(delete(RateLimitBucket))
    return engine


def context(db, tenant_id=None):
    tenant, user, _, _ = fixture_ids(db)
    return authorized_context(db, Identity(user, None), tenant_id or tenant.id)


def perform(engine, connection_id=None, operation=None):
    with Session(engine) as db, db.begin():
        conn = connection_id or fixture_ids(db)[2].id
        try:
            return integrations.execute(
                db, context(db), conn, operation or ConnectorOperation(name="requisition.list")
            )
        except ReliabilityError as error:
            return error


def configure(engine, *, fault=None, policy=None, budget=None):
    with Session(engine) as db, db.begin():
        conn = fixture_ids(db)[2].id
        if policy:
            reliability.configure_policy(db, context(db), conn, ReliabilityPolicy(**policy))
        if budget:
            reliability.configure_budget(db, context(db), conn, Budget(**budget))
        if fault:
            reliability.configure_fault(db, context(db), conn, FailureConfig(**fault))
        return conn


def oauth_pair(engine, expired=True):
    with Session(engine) as db, db.begin():
        conn = fixture_ids(db)[2].id
        access = credentials.submit(
            db,
            context(db),
            Settings(),
            conn,
            CredentialSubmission(
                credential_type="OAUTH_ACCESS_TOKEN",
                value=SecretStr("sim_access_test_value_only"),
                expires_at=utcnow() + timedelta(seconds=-1 if expired else 300),
            ),
        )
        refresh = credentials.submit(
            db,
            context(db),
            Settings(),
            conn,
            CredentialSubmission(
                credential_type="OAUTH_REFRESH_TOKEN",
                value=SecretStr("sim_refresh_test_value_only"),
                expires_at=utcnow() + timedelta(days=1),
            ),
        )
        reliability.configure_policy(
            db, context(db), conn, ReliabilityPolicy(credential_mode="OAUTH")
        )
        return access.id, refresh.id


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("0", 0),
        ("2", 2),
        ("9999999", 300),
        ("-1", None),
        ("nonsense", None),
        ("١", None),
    ],
)
def test_retry_after_delta_seconds_and_invalid(value, expected):
    assert parse_retry_after(value) == expected


def test_retry_after_http_date_and_timezone():
    now = datetime(2030, 1, 15, 12, tzinfo=UTC)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=17), usegmt=True), now) == 17
    assert parse_retry_after(format_datetime(now - timedelta(seconds=1), usegmt=True), now) == 0
    assert parse_retry_after("Tue, 15 Jan 2030 12:00:17", now) is None


@pytest.mark.parametrize(
    "change",
    [
        {"max_attempts": 0},
        {"operation_deadline_ms": 100},
        {"jitter_fraction": float("nan")},
        {"circuit_failure_threshold": 0},
        {"credential_mode": "REAL_OAUTH"},
        {"url": "https://provider.example.test"},
    ],
)
def test_policy_rejects_unsafe_or_incoherent_configuration(change):
    with pytest.raises(ValidationError):
        ReliabilityPolicy(**change)


@pytest.mark.parametrize(
    "scenario,category,retryable",
    [
        ("HTTP_401", "AUTHENTICATION_FAILED", False),
        ("HTTP_403", "AUTHORIZATION_FAILED", False),
        ("HTTP_429", "RATE_LIMITED", True),
        ("HTTP_500", "INTERNAL_ERROR", True),
        ("HTTP_503", "TEMPORARY_UNAVAILABLE", True),
        ("CONNECT_TIMEOUT", "TIMEOUT", True),
        ("READ_TIMEOUT", "TIMEOUT", True),
        ("RECOVERING_OUTAGE", "TEMPORARY_UNAVAILABLE", True),
    ],
)
def test_injected_failure_is_single_attempt_audited_and_persisted(
    rel_engine, scenario, category, retryable
):
    configure(
        rel_engine, fault={"enabled": True, "scenario": scenario, "attempts": 1, "retry_after": "2"}
    )
    error = perform(rel_engine)
    assert (
        isinstance(error, ReliabilityError)
        and error.category.value == category
        and error.retryable == retryable
    )
    with Session(rel_engine) as db:
        fault = db.scalar(select(FailureInjection))
        assert fault.consumed == 1 and not fault.enabled
        row = db.scalar(select(ConnectorObservation))
        assert row.reason == scenario and row.latency_ms >= 0
    assert not isinstance(perform(rel_engine), ReliabilityError)


@pytest.mark.parametrize("scope", ["TENANT", "CONNECTION", "CONNECTOR"])
def test_atomic_budgets_and_retry_after_status(rel_engine, scope):
    conn = configure(rel_engine, budget={"scope": scope, "limit": 1, "window_seconds": 120})
    assert not isinstance(perform(rel_engine), ReliabilityError)
    error = perform(rel_engine)
    assert error.category == ErrorCategory.RATE_LIMITED and 1 <= error.retry_after <= 120
    with Session(rel_engine) as db:
        status = next(
            r
            for r in reliability.rate_status(db, db.get(IntegrationConnection, conn))
            if r["scope"] == scope
        )
        assert status["used"] == 1 and status["remaining"] == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationalAlert)
                .where(OperationalAlert.category == "RATE_LIMIT_EXCEEDED")
            )
            == 1
        )


def test_concurrent_rate_admission_and_tenant_quota_isolation(rel_engine):
    configure(rel_engine, budget={"scope": "TENANT", "limit": 1, "window_seconds": 120})
    barrier = Barrier(2)

    def request(_):
        barrier.wait()
        return perform(rel_engine)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(request, range(2)))
    assert sum(isinstance(r, ReliabilityError) for r in results) == 1
    with Session(rel_engine) as db, db.begin():
        beta = db.scalar(select(Tenant).where(Tenant.slug == "demo-recruiting-beta"))
        connection = db.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.tenant_id == beta.id,
                IntegrationConnection.connector_type == "synthetic_crm",
            )
        )
        result = integrations.execute(
            db, context(db, beta.id), connection.id, ConnectorOperation(name="requisition.list")
        )
        assert result.synthetic


def test_connector_budget_shared_by_connections_not_other_types(rel_engine):
    from app.schemas.integration import CreateConnection

    configure(rel_engine, budget={"scope": "CONNECTOR", "limit": 1, "window_seconds": 120})
    with Session(rel_engine) as db, db.begin():
        additional = integrations.create_connection(
            db,
            context(db),
            CreateConnection(name="Second CRM", connector_type="synthetic_crm", config={}),
        )
        new_id = additional.id
        slack = fixture_ids(db)[3].id
    assert not isinstance(perform(rel_engine), ReliabilityError)
    assert perform(rel_engine, new_id).category == ErrorCategory.RATE_LIMITED
    assert not isinstance(
        perform(
            rel_engine,
            slack,
            ConnectorOperation(
                name="message.send",
                payload={
                    "channel": "recruiting",
                    "text": "Synthetic quota isolation",
                    "idempotency_key": "quota-isolation-once",
                },
            ),
        ),
        ReliabilityError,
    )


def test_circuit_persists_and_half_open_probe_recovers(rel_engine):
    conn = configure(
        rel_engine,
        policy={"circuit_failure_threshold": 1, "circuit_cooldown_seconds": 1},
        fault={"enabled": True, "scenario": "HTTP_503", "attempts": 1},
    )
    assert perform(rel_engine).reason == "HTTP_503"
    error = perform(rel_engine)
    assert error.reason == "CIRCUIT_OPEN"
    with Session(rel_engine) as db, db.begin():
        row = db.scalar(select(CircuitBreaker).where(CircuitBreaker.connection_id == conn))
        assert row.state == "OPEN"
        row.open_until = utcnow() - timedelta(seconds=1)
    assert not isinstance(perform(rel_engine), ReliabilityError)
    with Session(rel_engine) as db:
        assert (
            db.scalar(select(CircuitBreaker.state).where(CircuitBreaker.connection_id == conn))
            == "CLOSED"
        )
        assert list(
            db.scalars(
                select(CircuitTransition.to_state).order_by(
                    CircuitTransition.created_at, CircuitTransition.id
                )
            )
        )
        assert {r.to_state for r in db.scalars(select(CircuitTransition))} == {
            "OPEN",
            "HALF_OPEN",
            "CLOSED",
        }


def test_concurrent_half_open_probes_are_suppressed(rel_engine):
    conn = configure(
        rel_engine,
        policy={"circuit_failure_threshold": 1, "circuit_cooldown_seconds": 30},
        fault={"enabled": True, "scenario": "HTTP_503", "attempts": 2},
    )
    perform(rel_engine)
    with Session(rel_engine) as db, db.begin():
        db.scalar(select(CircuitBreaker).where(CircuitBreaker.connection_id == conn)).open_until = (
            utcnow() - timedelta(seconds=1)
        )
    barrier = Barrier(2)

    def request(_):
        barrier.wait()
        return perform(rel_engine)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(request, range(2)))
    assert sorted(r.reason for r in results) == ["CIRCUIT_OPEN", "HTTP_503"]
    with Session(rel_engine) as db:
        assert db.scalar(select(FailureInjection.consumed)) == 2


def test_persisted_probe_lease_survives_session_restart(rel_engine):
    conn = configure(
        rel_engine,
        policy={"circuit_failure_threshold": 1, "circuit_cooldown_seconds": 1},
        fault={"enabled": True, "scenario": "HTTP_503"},
    )
    perform(rel_engine)
    with Session(rel_engine) as db, db.begin():
        row = db.scalar(select(CircuitBreaker).where(CircuitBreaker.connection_id == conn))
        row.open_until = utcnow() - timedelta(seconds=1)
        worker_probe = worker.uuid.uuid4()
        reliability.reserve_probe(db, row, worker_probe, worker_probe)
    assert perform(rel_engine).reason == "CIRCUIT_OPEN"
    with Session(rel_engine) as db, db.begin():
        db.scalar(
            select(CircuitBreaker).where(CircuitBreaker.connection_id == conn)
        ).probe_expires_at = utcnow() - timedelta(seconds=1)
    assert not isinstance(perform(rel_engine), ReliabilityError)


def test_credentials_encrypted_context_bound_rotation_revocation(rel_engine):
    with Session(rel_engine) as db, db.begin():
        conn = fixture_ids(db)[2].id
        value = "sim_private_fixture_value_only"
        row = credentials.submit(
            db,
            context(db),
            Settings(),
            conn,
            CredentialSubmission(credential_type="API_KEY", value=SecretStr(value)),
        )
        assert value.encode() not in row.encrypted_payload
        assert credentials.decrypt(Settings(), row) == value and value not in str(
            credentials.metadata(row)
        )
        copied = copy.copy(row)
        copied.tenant_id = uuid.uuid4()
        with pytest.raises(ReliabilityError):
            credentials.decrypt(Settings(), copied)
        replacement = credentials.submit(
            db,
            context(db),
            Settings(),
            conn,
            CredentialSubmission(
                credential_type="API_KEY", value=SecretStr("sim_replacement_value_only")
            ),
            replace_id=row.id,
        )
        assert (
            row.status == "ROTATED"
            and replacement.generation == 2
            and replacement.replaces_id == row.id
        )
        assert credentials.revoke(db, context(db), conn, replacement.id).status == "REVOKED"


def test_missing_wrong_encryption_and_previous_key_reference(rel_engine):
    with Session(rel_engine) as db, db.begin():
        conn = fixture_ids(db)[2].id
        empty = Settings(_env_file=None, credential_encryption_key=None)
        data = CredentialSubmission(
            credential_type="API_KEY", value=SecretStr("sim_test_submission_value")
        )
        with pytest.raises(ReliabilityError):
            credentials.submit(db, context(db), empty, conn, data)
        settings = Settings()
        row = credentials.submit(db, context(db), settings, conn, data)
        wrong = Settings(
            _env_file=None,
            credential_encryption_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()),
        )
        with pytest.raises(ReliabilityError):
            credentials.decrypt(wrong, row)
        new = Settings(
            _env_file=None,
            credential_key_reference="local-v2",
            credential_encryption_key=wrong.credential_encryption_key,
            credential_previous_keys={"local-v1": settings.credential_encryption_key},
        )
        assert credentials.decrypt(new, row) == data.value.get_secret_value()


def test_expired_api_key_is_permanent_and_alerted(rel_engine):
    with Session(rel_engine) as db, db.begin():
        conn = fixture_ids(db)[2].id
        credentials.submit(
            db,
            context(db),
            Settings(),
            conn,
            CredentialSubmission(
                credential_type="API_KEY",
                value=SecretStr("sim_expired_api_key_only"),
                expires_at=utcnow() - timedelta(seconds=1),
            ),
        )
        reliability.configure_policy(
            db, context(db), conn, ReliabilityPolicy(credential_mode="API_KEY")
        )
    error = perform(rel_engine)
    assert not error.retryable and error.reason == "CREDENTIAL_EXPIRED"
    with Session(rel_engine) as db:
        assert db.scalar(
            select(OperationalAlert.id).where(OperationalAlert.category == "CREDENTIAL_EXPIRED")
        )
        assert db.scalar(select(ConnectionCredential.status)) == "EXPIRED"


def test_expired_access_refreshes_and_rotates_both_tokens_once(rel_engine):
    old_access, old_refresh = oauth_pair(rel_engine)
    assert not isinstance(perform(rel_engine), ReliabilityError)
    assert not isinstance(perform(rel_engine), ReliabilityError)
    with Session(rel_engine) as db:
        assert db.get(ConnectionCredential, old_access).status == "ROTATED"
        assert db.get(ConnectionCredential, old_refresh).status == "ROTATED"
        assert db.scalar(select(func.count()).select_from(CredentialRefreshAttempt)) == 1
        assert db.scalar(select(CredentialRefreshAttempt.outcome)) == "SUCCEEDED"


def test_concurrent_refresh_only_one_rotation(rel_engine):
    oauth_pair(rel_engine)
    barrier = Barrier(2)

    def refresh_once(_):
        with Session(rel_engine) as db, db.begin():
            barrier.wait()
            ctx = context(db)
            credentials.manage_lock(db, ctx)
            conn = integrations.get_connection(db, ctx, fixture_ids(db)[2].id, True)
            return credentials.refresh(db, Settings(), conn, uuid.uuid4())["outcome"]

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(refresh_once, range(2)))
    assert sorted(results) == ["NOT_NEEDED", "SUCCEEDED"]
    with Session(rel_engine) as db:
        assert db.scalar(select(func.count()).select_from(CredentialRefreshAttempt)) == 1


@pytest.mark.parametrize("scenario", ["REVOKED_REFRESH_TOKEN", "REFRESH_FAILED"])
def test_failed_refresh_is_persisted_permanent_and_actionable(rel_engine, scenario):
    oauth_pair(rel_engine, False)
    configure(rel_engine, fault={"enabled": True, "scenario": scenario})
    error = perform(rel_engine)
    assert error.reason == "REFRESH_FAILED" and not error.retryable
    with Session(rel_engine) as db:
        assert db.scalar(select(CredentialRefreshAttempt.outcome)) == "FAILED"
        assert db.scalar(
            select(OperationalAlert.id).where(OperationalAlert.category == "REFRESH_FAILED")
        )
        assert (
            db.scalar(
                select(IntegrationConnection.health_status).where(
                    IntegrationConnection.id == fixture_ids(db)[2].id
                )
            )
            == "UNAVAILABLE"
        )


def test_rate_retry_worker_restart_and_duplicate_action_suppression(rel_engine):
    with Session(rel_engine) as db:
        crm, slack = fixture_ids(db)[2:]
    specs = [
        {
            "id": "read",
            "type": "MOCK_CONNECTOR_ACTION",
            "config": {
                "connection_id": str(crm.id),
                "operation": "requisition.list",
                "payload": {},
            },
            "retry": {"max_attempts": 5, "jitter_fraction": 0},
        },
        {
            "id": "notify",
            "type": "MOCK_CONNECTOR_ACTION",
            "config": {
                "connection_id": str(slack.id),
                "operation": "message.send",
                "payload": {"channel": "recruiting", "text": "Synthetic reliable workflow"},
            },
        },
    ]
    ids = setup(rel_engine, specs)
    execution_id = dispatched(rel_engine, ids)
    configure(
        rel_engine,
        fault={"enabled": True, "scenario": "HTTP_429", "retry_after": "2"},
        policy={"max_attempts": 5, "jitter_fraction": 0},
    )
    before = utcnow()
    tick(rel_engine, ids[3])
    with Session(rel_engine) as db, db.begin():
        row = db.get(WorkflowExecution, execution_id)
        assert row.state == "RETRY_SCHEDULED" and row.next_run_at >= before + timedelta(seconds=2)
        row.next_run_at = utcnow()
        new_worker = worker.register_worker(db)
        assert db.scalar(select(FailureInjection.consumed)) == 1
    tick(rel_engine, new_worker)
    tick(rel_engine, new_worker)
    with Session(rel_engine) as db:
        assert db.get(WorkflowExecution, execution_id).state == "SUCCEEDED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkflowActionReceipt)
                .where(WorkflowActionReceipt.root_execution_id == execution_id)
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(SyntheticNotification.tenant_id == ids[0])
            )
            == 2
        )
        assert db.scalar(select(func.count()).select_from(RetrySchedule)) == 1
        assert (
            db.scalar(
                select(ConnectorObservation.outcome).where(
                    ConnectorObservation.reason == "HTTP_429"
                )
            )
            == "DEFERRED"
        )


def test_alert_dedup_ack_resolve_reopen_and_safe_context(rel_engine):
    with Session(rel_engine) as db, db.begin():
        ctx = context(db)
        conn = fixture_ids(db)[2].id
        source = str(uuid.uuid4())
        alert = observability.emit(
            db,
            ctx.tenant.id,
            "CONNECTION_UNHEALTHY",
            conn,
            uuid.uuid4(),
            source,
            {"connection_id": conn, "password": "private", "reason": "private-secret"},
        )
        assert alert.context == {"connection_id": str(conn)}
        assert (
            observability.emit(
                db, ctx.tenant.id, "CONNECTION_UNHEALTHY", conn, uuid.uuid4(), source
            ).occurrence_count
            == 1
        )
        assert (
            observability.update_alert(db, ctx, alert.id, "acknowledge", uuid.uuid4()).status
            == "ACKNOWLEDGED"
        )
        assert (
            observability.update_alert(db, ctx, alert.id, "resolve", uuid.uuid4()).status
            == "RESOLVED"
        )
        assert (
            observability.emit(
                db, ctx.tenant.id, "CONNECTION_UNHEALTHY", conn, uuid.uuid4(), str(uuid.uuid4())
            ).status
            == "OPEN"
        )
        assert alert.occurrence_count == 2
        assert db.scalar(select(func.count()).select_from(AlertEvent)) >= 3


def test_metrics_correct_and_retention_does_not_delete_business_data(rel_engine):
    configure(rel_engine, fault={"enabled": True, "scenario": "HTTP_503", "attempts": 1})
    assert isinstance(perform(rel_engine), ReliabilityError)
    perform(rel_engine)
    with Session(rel_engine) as db, db.begin():
        tenant_id = fixture_ids(db)[0].id
        data = observability.metrics(db, tenant_id, Settings())
        assert (
            data["operation_count"] == 2
            and data["failure_count"] == 1
            and data["success_count"] == 1
        )
        assert data["latency_ms"]["average"] is not None
        row = db.scalar(select(ConnectorObservation))
        row.created_at = utcnow() - timedelta(days=10)
        db.flush()
        observability.prune(db, tenant_id, Settings())
        assert (
            db.scalar(
                select(func.count())
                .select_from(ConnectorObservation)
                .where(ConnectorObservation.tenant_id == tenant_id)
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(IntegrationConnection)) == 10


def test_api_permissions_secret_redaction_and_cross_tenant(identity_env):
    from test_connectors import create

    env = identity_env
    settings = env["app"].state.settings
    settings.credential_encryption_key = SecretStr(
        base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    assert login(env).status_code == 200
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)})
    conn = create(env)
    secret = "sim_private_api_submission_only"
    created = mutation(
        env["client"],
        "POST",
        f"connections/{conn}/credentials",
        {"credential_type": "API_KEY", "value": secret},
    )
    assert created.status_code == 201 and secret not in created.text
    credential = created.json()["id"]
    assert env["client"].get(f"/api/v1/connections/{conn}/credentials").status_code == 200
    policy_response = mutation(
        env["client"],
        "PUT",
        f"connections/{conn}/reliability",
        ReliabilityPolicy(credential_mode="API_KEY").model_dump(),
    )
    assert policy_response.status_code == 200
    fault = mutation(
        env["client"],
        "PUT",
        f"connections/{conn}/failure-injection",
        {"enabled": True, "scenario": "HTTP_401"},
    )
    assert fault.status_code == 200
    response = mutation(
        env["client"],
        "POST",
        f"connections/{conn}/execute",
        {"name": "requisition.list", "payload": {}},
    )
    assert response.status_code == 401 and secret not in response.text
    assert env["client"].get("/api/v1/operations/metrics").json()["failure_count"] == 1
    assert login(env, "OPERATIONS_USER").status_code == 200
    assert env["client"].get(f"/api/v1/connections/{conn}/reliability").status_code == 200
    assert (
        mutation(
            env["client"], "PUT", f"connections/{conn}/failure-injection", {"enabled": False}
        ).status_code
        == 403
    )
    assert (
        mutation(
            env["client"], "POST", f"connections/{conn}/credentials/{credential}/revoke"
        ).status_code
        == 403
    )
    alerts = env["client"].get("/api/v1/alerts").json()["items"]
    assert alerts
    assert (
        mutation(env["client"], "POST", f"alerts/{alerts[0]['id']}/acknowledge").status_code == 200
    )
    assert mutation(env["client"], "POST", f"alerts/{alerts[0]['id']}/resolve").status_code == 403
    assert login(env).status_code == 200
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    for path in [
        f"connections/{conn}/reliability",
        f"connections/{conn}/credentials",
        f"alerts/{alerts[0]['id']}",
    ]:
        assert env["client"].get("/api/v1/" + path).status_code == 404
    with Session(env["connection"]) as db:
        rows = list(db.scalars(select(AuditEvent)))
        assert all(secret not in str(row.details) for row in rows)


def test_sql_read_timeout_rolls_back_effect_and_records_failure(rel_engine, monkeypatch):
    from app.connectors.registry import registry

    configure(
        rel_engine,
        policy={"connect_timeout_ms": 100, "read_timeout_ms": 100, "operation_deadline_ms": 500},
    )
    adapter = registry.resolve("synthetic_crm")

    def slow_database(db, *_):
        db.execute(text("SELECT pg_sleep(1)"))

    monkeypatch.setattr(adapter, "perform", slow_database)
    error = perform(rel_engine)
    assert error.category == ErrorCategory.TIMEOUT and error.reason == "READ_TIMEOUT"
    with Session(rel_engine) as db:
        assert db.scalar(select(ConnectorObservation.reason)) == "READ_TIMEOUT"


def test_overall_deadline_rolls_back_local_side_effect(rel_engine, monkeypatch):
    import time

    from app.connectors.registry import registry

    with Session(rel_engine) as db, db.begin():
        ctx = context(db)
        slack = fixture_ids(db)[3].id
        reliability.configure_policy(
            db,
            ctx,
            slack,
            ReliabilityPolicy(connect_timeout_ms=50, read_timeout_ms=50, operation_deadline_ms=100),
        )
    adapter = registry.resolve("synthetic_slack")
    original = adapter.perform

    def slow_effect(*args):
        result = original(*args)
        time.sleep(0.15)
        return result

    monkeypatch.setattr(adapter, "perform", slow_effect)
    error = perform(
        rel_engine,
        slack,
        ConnectorOperation(
            name="message.send",
            payload={
                "channel": "recruiting",
                "text": "Synthetic deadline rollback",
                "idempotency_key": "deadline-rollback-only",
            },
        ),
    )
    assert error.reason == "OPERATION_DEADLINE"
    with Session(rel_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SyntheticNotification)
                .where(SyntheticNotification.connection_id == slack)
            )
            == 1
        )


def test_connection_lock_timeout_has_no_effect_or_invented_metric(rel_engine):
    configure(
        rel_engine,
        policy={"connect_timeout_ms": 50, "read_timeout_ms": 50, "operation_deadline_ms": 100},
    )
    with Session(rel_engine) as owner, owner.begin():
        tenant = fixture_ids(owner)[0]
        owner.execute(select(Tenant).where(Tenant.id == tenant.id).with_for_update())
        error = perform(rel_engine)
        assert error.reason == "CONNECT_TIMEOUT"
        assert owner.scalar(select(func.count()).select_from(ConnectorObservation)) == 0


def test_disabled_injection_and_permanent_workflow_auth_failure(rel_engine):
    with Session(rel_engine) as db:
        crm = fixture_ids(db)[2].id
    ids = setup(
        rel_engine,
        [
            {
                "id": "read",
                "type": "MOCK_CONNECTOR_ACTION",
                "config": {
                    "connection_id": str(crm),
                    "operation": "requisition.list",
                    "payload": {},
                },
            }
        ],
    )
    execution_id = dispatched(rel_engine, ids)
    configure(rel_engine, fault={"enabled": True, "scenario": "HTTP_403", "attempts": 3})
    tick(rel_engine, ids[3])
    with Session(rel_engine) as db:
        assert db.get(WorkflowExecution, execution_id).state == "DEAD_LETTERED"
        assert db.scalar(select(func.count()).select_from(RetrySchedule)) == 0
        assert db.scalar(
            select(OperationalAlert.id).where(OperationalAlert.category == "WORKFLOW_DEAD_LETTERED")
        )
    configure(rel_engine, fault={"enabled": False})
    assert not isinstance(perform(rel_engine), ReliabilityError)


def test_health_probe_cannot_override_missing_key_or_open_breaker(rel_engine):
    oauth_pair(rel_engine, False)
    with Session(rel_engine) as db, db.begin():
        db.info["settings"] = Settings(_env_file=None, credential_encryption_key=None)
        conn = fixture_ids(db)[2].id
        assert integrations.test_health(db, context(db), conn).status == "UNAVAILABLE"
    configure(
        rel_engine,
        policy={"credential_mode": "OAUTH", "circuit_failure_threshold": 1},
        fault={"enabled": True, "scenario": "HTTP_503"},
    )
    perform(rel_engine)
    with Session(rel_engine) as db, db.begin():
        conn = fixture_ids(db)[2].id
        assert integrations.test_health(db, context(db), conn).status == "UNAVAILABLE"
        assert (
            db.scalar(select(CircuitBreaker.state).where(CircuitBreaker.connection_id == conn))
            == "OPEN"
        )
