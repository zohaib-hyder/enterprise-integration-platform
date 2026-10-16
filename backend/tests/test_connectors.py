import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_identity import login, mutation

from app.api.dependencies import Identity, authorized_context
from app.connectors.contracts import (
    ConnectorError,
    ConnectorOperation,
    ConnectorType,
    ErrorCategory,
    RestConfig,
)
from app.connectors.registry import ConnectorRegistry, registry
from app.core.security import utcnow
from app.models.identity import AuthSession, Membership, User
from app.models.integration import (
    ConnectorDefinition,
    IntegrationConnection,
    SyntheticATSRecord,
    SyntheticCalendarEvent,
    SyntheticCRMRecord,
    SyntheticEmail,
    SyntheticNotification,
)
from app.models.tenant import Tenant
from app.seed_demo import seed_demo
from app.services import integrations


@pytest.fixture
def connector_env(identity_env):
    login(identity_env)
    assert (
        mutation(
            identity_env["client"],
            "POST",
            "tenant/switch",
            {"tenant_id": str(identity_env["a"].id)},
        ).status_code
        == 200
    )
    return identity_env


def create(env, kind="synthetic_crm", name="Synthetic test connection", config=None, enabled=True):
    response = mutation(
        env["client"],
        "POST",
        "connections",
        {"name": name, "connector_type": kind, "config": config or {}, "enabled": enabled},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def execute(env, connection, name, payload=None):
    return mutation(
        env["client"],
        "POST",
        f"connections/{connection}/execute",
        {"schema_version": 1, "name": name, "payload": payload or {}},
    )


@pytest.mark.parametrize("kind", list(ConnectorType))
def test_registry_resolves_allowlisted_types(kind):
    assert registry.resolve(kind)
    capability = registry.capabilities(kind)
    assert capability.connector_type == kind
    assert capability.config_schema_version == 1
    assert capability.enabled == (kind != ConnectorType.REST)


@pytest.mark.parametrize(
    "kind", ["unknown", "os.system", "https://evil.example.test", "app.connectors.other"]
)
def test_unknown_connector_rejected(kind):
    with pytest.raises(ConnectorError) as error:
        registry.resolve(kind)
    assert error.value.category == ErrorCategory.UNSUPPORTED_OPERATION


def test_registry_rejects_duplicate_and_unknown_registration():
    registry_copy = ConnectorRegistry()
    registry_copy.register(ConnectorType.CRM, "Synthetic", registry.resolve(ConnectorType.CRM))
    with pytest.raises(ValueError):
        registry_copy.register(ConnectorType.CRM, "Duplicate", registry.resolve(ConnectorType.CRM))
    with pytest.raises(ValueError):
        registry_copy.register("unknown", "Invalid", registry.resolve(ConnectorType.CRM))


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_sanitized_error_categories_and_retryability(category):
    error = ConnectorError(category)
    assert error.retryable == (
        category
        in {ErrorCategory.TIMEOUT, ErrorCategory.RATE_LIMITED, ErrorCategory.TEMPORARY_UNAVAILABLE}
    )
    assert str(error) == error.safe_message


@pytest.mark.parametrize(
    "data",
    [
        {"schema_version": 2, "name": "contact.list"},
        {"name": "unknown"},
        {"name": "contact.list", "tenant_id": str(uuid.uuid4())},
    ],
)
def test_versioned_operation_schema_rejects_unsafe_inputs(data):
    with pytest.raises(ValidationError):
        ConnectorOperation.model_validate(data)


@pytest.mark.parametrize(
    "kind,person,record_model",
    [
        ("synthetic_crm", "contact", SyntheticCRMRecord),
        ("synthetic_ats", "candidate", SyntheticATSRecord),
    ],
)
def test_business_crud_upsert_get_search_and_pagination(
    connector_env, db, kind, person, record_model
):
    env = connector_env
    connection = create(env, kind)
    for index in range(3):
        data = {"name": f"Synthetic Person {index}", "email": f"person-{index}@example.test"}
        data.update(
            {"company": "Fictional Company"}
            if person == "contact"
            else {"skills": ["Python"], "stage": "NEW"}
        )
        response = execute(
            env, connection, f"{person}.upsert", {"external_id": f"external-{index}", "data": data}
        )
        assert response.status_code == 200, response.text
        if index == 0:
            first = response.json()["data"]
    response = execute(env, connection, f"{person}.list", {"offset": 1, "limit": 1})
    assert response.status_code == 200 and response.json()["data"]["total"] == 3
    assert len(response.json()["data"]["items"]) == 1
    assert (
        execute(env, connection, f"{person}.get", {"id": first["id"]}).json()["data"]["external_id"]
        == "external-0"
    )
    updated = first["data"] | {"name": "Synthetic Updated Person"}
    assert (
        execute(
            env, connection, f"{person}.upsert", {"external_id": "external-0", "data": updated}
        ).json()["data"]["id"]
        == first["id"]
    )
    response = execute(
        env, connection, "record.search", {"kind": person, "external_id": "external-0"}
    )
    assert response.json()["data"]["data"]["name"] == "Synthetic Updated Person"
    assert db.scalar(select(func.count()).select_from(record_model)) == 3
    assert execute(env, connection, f"{person}.get", {"id": str(uuid.uuid4())}).status_code == 404
    assert (
        execute(
            env, connection, "record.search", {"kind": person, "external_id": "missing"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("kind", ["synthetic_crm", "synthetic_ats"])
def test_requisition_operations(connector_env, kind):
    env = connector_env
    connection = create(env, kind)
    data = {
        "title": "Synthetic Specialist",
        "department": "Technology",
        "location": "Toronto",
        "status": "OPEN",
    }
    response = execute(
        env, connection, "requisition.upsert", {"external_id": "job-1", "data": data}
    )
    assert response.status_code == 200
    identifier = response.json()["data"]["id"]
    assert (
        execute(env, connection, "requisition.get", {"id": identifier}).json()["data"]["data"]
        == data
    )
    assert execute(env, connection, "requisition.list").json()["data"]["total"] == 1
    response = execute(
        env,
        connection,
        "requisition.upsert",
        {"external_id": "job-1", "data": data | {"status": "CLOSED"}},
    )
    assert (
        response.json()["data"]["id"] == identifier
        and response.json()["data"]["data"]["status"] == "CLOSED"
    )
    assert (
        execute(
            env, connection, "record.search", {"kind": "requisition", "external_id": "job-1"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "kind,payload,model",
    [
        (
            "synthetic_slack",
            {
                "channel": "recruiting",
                "text": "Synthetic notification",
                "idempotency_key": "safe-message-key",
            },
            SyntheticNotification,
        ),
        (
            "synthetic_email",
            {
                "recipient": "recruiter@example.test",
                "subject": "Synthetic subject",
                "body": "Synthetic body",
                "idempotency_key": "safe-message-key",
            },
            SyntheticEmail,
        ),
    ],
)
def test_local_delivery_persistence_idempotency_and_conflict(
    connector_env, db, kind, payload, model
):
    env = connector_env
    connection = create(env, kind)
    first = execute(env, connection, "message.send", payload)
    assert first.status_code == 200 and first.json()["data"]["status"] == "SIMULATED"
    second = execute(env, connection, "message.send", payload)
    assert second.status_code == 200 and second.json()["data"]["duplicate"]
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    changed = payload | (
        {"text": "Different"} if kind == "synthetic_slack" else {"subject": "Different"}
    )
    response = execute(env, connection, "message.send", changed)
    assert response.status_code == 409 and response.json()["error"]["category"] == "CONFLICT"
    assert db.scalar(select(func.count()).select_from(model)) == 1
    inspected = env["client"].get(f"/api/v1/connections/{connection}/records").json()
    assert inspected["total"] == 1 and inspected["items"][0]["status"] == "SIMULATED"
    assert "request_hash" not in str(inspected) and "idempotency_key" not in str(inspected)


@pytest.mark.parametrize(
    "kind,payload",
    [
        (
            "synthetic_slack",
            {"channel": "alerts", "text": "Synthetic", "idempotency_key": "message-key-v1"},
        ),
        (
            "synthetic_slack",
            {
                "channel": "https://evil.example.test",
                "text": "Synthetic",
                "idempotency_key": "message-key-v1",
            },
        ),
        (
            "synthetic_email",
            {
                "recipient": "other@example.test",
                "subject": "Synthetic",
                "body": "Synthetic",
                "idempotency_key": "message-key-v1",
            },
        ),
        (
            "synthetic_email",
            {
                "recipient": "real@gmail.com",
                "subject": "Synthetic",
                "body": "Synthetic",
                "idempotency_key": "message-key-v1",
            },
        ),
        (
            "synthetic_email",
            {
                "recipient": "recruiter@example.test",
                "subject": "Header\r\nInjection",
                "body": "Synthetic",
                "idempotency_key": "message-key-v1",
            },
        ),
    ],
)
def test_unsupported_destinations_and_invalid_subjects(connector_env, kind, payload):
    connection = create(connector_env, kind)
    response = execute(connector_env, connection, "message.send", payload)
    assert response.status_code in {403, 422}
    assert "gmail" not in response.text and "evil" not in response.text


def booking(key="calendar-key-1", start="2030-01-15T15:00:00Z", end="2030-01-15T16:00:00Z"):
    return {
        "calendar": "interviews",
        "title": "Synthetic interview",
        "starts_at": start,
        "ends_at": end,
        "idempotency_key": key,
    }


def test_calendar_create_update_overlap_cancel_and_idempotency(connector_env, db):
    env = connector_env
    connection = create(env, "synthetic_calendar")
    first = execute(env, connection, "event.create", booking())
    assert first.status_code == 200
    identifier = first.json()["data"]["id"]
    assert execute(env, connection, "event.create", booking()).json()["data"]["duplicate"]
    assert execute(env, connection, "event.create", booking("calendar-key-2")).status_code == 409
    adjacent = execute(
        env,
        connection,
        "event.create",
        booking("calendar-adjacent", "2030-01-15T16:00:00Z", "2030-01-15T17:00:00Z"),
    )
    assert adjacent.status_code == 200
    update = {key: value for key, value in booking().items() if key != "idempotency_key"} | {
        "id": identifier,
        "ends_at": "2030-01-15T16:30:00Z",
    }
    assert execute(env, connection, "event.update", update).status_code == 409
    update["ends_at"] = "2030-01-15T15:30:00Z"
    assert execute(env, connection, "event.update", update).status_code == 200
    assert (
        execute(env, connection, "event.cancel", {"id": identifier}).json()["data"]["status"]
        == "CANCELLED"
    )
    assert execute(env, connection, "event.cancel", {"id": identifier}).status_code == 200
    assert execute(env, connection, "event.update", update).status_code == 409
    assert (
        execute(env, connection, "event.create", booking("calendar-after-cancel")).status_code
        == 200
    )
    assert execute(env, connection, "event.list").json()["data"]["total"] == 3
    assert db.scalar(select(SyntheticCalendarEvent.starts_at)).tzinfo is not None


@pytest.mark.parametrize(
    "payload",
    [
        booking(start="2030-01-15T15:00:00", end="2030-01-15T16:00:00"),
        booking(start="2030-01-15T16:00:00Z", end="2030-01-15T15:00:00Z"),
        booking() | {"calendar": "personal"},
    ],
)
def test_calendar_requires_valid_timezone_and_interval(connector_env, payload):
    connection = create(connector_env, "synthetic_calendar")
    assert execute(connector_env, connection, "event.create", payload).status_code == 422


def test_calendar_disallowed_configured_calendar(connector_env):
    connection = create(connector_env, "synthetic_calendar")
    assert (
        execute(
            connector_env, connection, "event.create", booking() | {"calendar": "recruiting"}
        ).status_code
        == 403
    )


def test_connection_catalog_health_enable_disable_and_operations(connector_env):
    env = connector_env
    connection = create(env)
    catalog = env["client"].get("/api/v1/connector-definitions").json()
    assert len(catalog) == 6
    assert (
        next(item for item in catalog if item["connector_type"] == "generic_rest")["operations"]
        == []
    )
    assert env["client"].get(f"/api/v1/connections/{connection}/operations").status_code == 200
    assert (
        mutation(env["client"], "POST", f"connections/{connection}/health").json()["status"]
        == "HEALTHY"
    )
    details = env["client"].get(f"/api/v1/connections/{connection}").json()
    assert details["health_status"] == "HEALTHY" and details["last_checked_at"]
    assert (
        mutation(
            env["client"], "PATCH", f"connections/{connection}", {"enabled": False}
        ).status_code
        == 200
    )
    assert execute(env, connection, "contact.list").status_code == 403
    assert (
        mutation(env["client"], "POST", f"connections/{connection}/health").json()["status"]
        == "DISABLED"
    )
    assert (
        mutation(env["client"], "PATCH", f"connections/{connection}", {"enabled": True}).status_code
        == 200
    )
    assert execute(env, connection, "contact.list").status_code == 200
    assert (
        mutation(
            env["client"],
            "PATCH",
            f"connections/{connection}",
            {"config": {"workspace_label": "Updated synthetic workspace"}},
        ).json()["health_status"]
        == "UNKNOWN"
    )


@pytest.mark.parametrize(
    "config",
    [
        {"password": "private-secret"},
        {"access_token": "private-secret"},
        {"url": "http://127.0.0.1"},
        {"schema_version": 2},
        {"allowed_channels": ["arbitrary"]},
    ],
)
def test_config_allowlist_rejects_credentials_and_unknown_fields(connector_env, config):
    response = mutation(
        connector_env["client"],
        "POST",
        "connections",
        {"connector_type": "synthetic_crm", "name": "Invalid config", "config": config},
    )
    assert response.status_code == 422
    assert "private-secret" not in response.text and "127.0.0.1" not in response.text


def test_unsupported_operations_and_schema_validation(connector_env):
    connection = create(connector_env)
    assert (
        execute(connector_env, connection, "candidate.list").json()["error"]["category"]
        == "UNSUPPORTED_OPERATION"
    )
    assert (
        execute(
            connector_env, connection, "contact.list", {"tenant_id": str(connector_env["b"].id)}
        ).status_code
        == 422
    )
    assert execute(connector_env, connection, "contact.list", {"limit": 101}).status_code == 422
    assert (
        execute(
            connector_env,
            connection,
            "contact.upsert",
            {
                "external_id": "valid",
                "data": {"name": "Synthetic", "email": "real@gmail.com", "company": "Fictional"},
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize("role", ["TENANT_ADMIN", "INTEGRATION_ENGINEER", "OPERATIONS_USER"])
def test_connection_rbac(identity_env, role):
    env = identity_env
    login(env)
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}
        ).status_code
        == 200
    )
    connection = create(env)
    mutation(env["client"], "POST", "auth/logout")
    login(env, role)
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}
        ).status_code
        == 200
    )
    assert env["client"].get("/api/v1/connections").status_code == 200
    assert env["client"].get(f"/api/v1/connections/{connection}").status_code == 200
    assert env["client"].get(f"/api/v1/connections/{connection}/operations").status_code == 200
    expected = 403 if role == "OPERATIONS_USER" else 200
    assert (
        mutation(env["client"], "POST", f"connections/{connection}/health").status_code == expected
    )
    assert execute(env, connection, "contact.list").status_code == expected
    assert (
        mutation(
            env["client"], "PATCH", f"connections/{connection}", {"enabled": False}
        ).status_code
        == expected
    )
    assert env["client"].get(f"/api/v1/connections/{connection}/records").status_code == expected
    response = mutation(
        env["client"],
        "POST",
        "connections",
        {"connector_type": "synthetic_crm", "name": "Second connection"},
    )
    assert response.status_code == (403 if role == "OPERATIONS_USER" else 201)


def test_active_tenant_boundary_for_connection_ids_and_records(connector_env):
    env = connector_env
    connection = create(env)
    person = execute(
        env,
        connection,
        "contact.upsert",
        {
            "external_id": "shared-id",
            "data": {
                "name": "Tenant Alpha Synthetic",
                "email": "alpha@example.test",
                "company": "Fictional",
            },
        },
    ).json()["data"]["id"]
    mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    for path in [
        f"connections/{connection}",
        f"connections/{connection}/records",
        f"connections/{connection}/operations",
    ]:
        response = env["client"].get("/api/v1/" + path)
        assert response.status_code == 404 and "Tenant Alpha" not in response.text
    assert execute(env, connection, "contact.list").status_code == 404
    assert (
        mutation(
            env["client"], "PATCH", f"connections/{connection}", {"enabled": False}
        ).status_code
        == 404
    )
    other = create(env, name="Synthetic test connection")
    assert execute(env, other, "contact.get", {"id": person}).status_code == 404
    assert execute(env, other, "contact.list").json()["data"]["total"] == 0
    assert (
        execute(
            env,
            other,
            "contact.upsert",
            {
                "external_id": "shared-id",
                "data": {
                    "name": "Tenant Beta Synthetic",
                    "email": "beta@example.test",
                    "company": "Fictional",
                },
            },
        ).status_code
        == 200
    )
    listing = env["client"].get("/api/v1/connections").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == other
    assert (
        mutation(
            env["client"],
            "POST",
            "connections",
            {
                "connector_type": "synthetic_crm",
                "name": "Unsafe tenant selection",
                "tenant_id": str(env["a"].id),
            },
        ).status_code
        == 422
    )


def test_connection_name_uniqueness(connector_env):
    create(connector_env)
    response = mutation(
        connector_env["client"],
        "POST",
        "connections",
        {"connector_type": "synthetic_crm", "name": "Synthetic test connection"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "model",
    [
        SyntheticCRMRecord,
        SyntheticATSRecord,
        SyntheticNotification,
        SyntheticEmail,
        SyntheticCalendarEvent,
    ],
)
def test_composite_tenant_connection_foreign_keys(connector_env, db, model):
    connection = create(connector_env)
    fields = {"tenant_id": connector_env["b"].id, "connection_id": uuid.UUID(connection)}
    if model in {SyntheticCRMRecord, SyntheticATSRecord}:
        fields |= {"kind": "requisition", "external_id": "test-id", "payload": {}}
    elif model == SyntheticCalendarEvent:
        fields |= {
            "calendar": "interviews",
            "title": "Synthetic",
            "starts_at": utcnow(),
            "ends_at": utcnow() + timedelta(hours=1),
            "idempotency_key": "safe-key-v1",
            "request_hash": "f" * 64,
        }
    elif model == SyntheticEmail:
        fields |= {
            "recipient": "recipient@example.test",
            "subject": "Synthetic",
            "body": "Synthetic",
            "idempotency_key": "safe-key-v1",
            "request_hash": "f" * 64,
        }
    else:
        fields |= {
            "channel": "recruiting",
            "text": "Synthetic",
            "idempotency_key": "safe-key-v1",
            "request_hash": "f" * 64,
        }
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(model(**fields))
        db.flush()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://127.0.0.1",
        "http://169.254.169.254",
        "https://10.0.0.1",
        "https://[::1]",
        "https://rebinding.example.test",
        "https://public.example.test",
    ],
)
def test_disabled_rest_cannot_resolve_or_send_to_any_destination(url):
    adapter = registry.resolve(ConnectorType.REST)
    with (
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")) as dns,
        patch.object(
            httpx.Client, "send", side_effect=AssertionError("Network forbidden")
        ) as network,
    ):
        with pytest.raises(ConnectorError):
            adapter.validate_config({"url": url})
        with pytest.raises(ConnectorError) as error:
            adapter.execute(
                None,
                uuid.uuid4(),
                uuid.uuid4(),
                RestConfig(),
                ConnectorOperation(name="contact.list", payload={"url": url}),
            )
        assert error.value.category == ErrorCategory.UNSUPPORTED_OPERATION
        dns.assert_not_called()
        network.assert_not_called()


def test_rest_connection_cannot_enable_or_execute(connector_env, db):
    env = connector_env
    connection = create(env, "generic_rest", enabled=False)
    assert (
        mutation(env["client"], "PATCH", f"connections/{connection}", {"enabled": True}).status_code
        == 400
    )
    assert execute(env, connection, "contact.list").status_code == 403
    assert env["client"].get(f"/api/v1/connections/{connection}/operations").json() == []
    with pytest.raises(IntegrityError), db.begin_nested():
        db.get(IntegrationConnection, uuid.UUID(connection)).status = "ENABLED"
        db.flush()


def test_mock_connectors_make_no_network_calls(connector_env, db):
    env = connector_env
    slack = create(env, "synthetic_slack")
    row = db.get(IntegrationConnection, uuid.UUID(slack))
    session = db.scalar(select(AuthSession))
    identity = Identity(db.get(User, session.user_id), session)
    context = authorized_context(db, identity, env["a"].id)
    with (
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")) as dns,
        patch.object(httpx.Client, "send", side_effect=AssertionError("HTTP forbidden")) as http,
    ):
        integrations.execute(
            db,
            context,
            row.id,
            ConnectorOperation(
                name="message.send",
                payload={
                    "channel": "recruiting",
                    "text": "Synthetic",
                    "idempotency_key": "no-network-key",
                },
            ),
        )
        dns.assert_not_called()
        http.assert_not_called()


def test_seed_repeatable_and_preserves_existing_tenants_and_users(identity_env, db):
    env = identity_env
    original_users = list(db.scalars(select(User.id)))
    before = set(db.scalars(select(Tenant.id)))
    arguments = (db, env["users"]["TENANT_ADMIN"].email, env["password"], "Synthetic administrator")
    with (
        patch.object(httpx.Client, "send", side_effect=AssertionError("HTTP forbidden")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")),
    ):
        assert seed_demo(*arguments) == seed_demo(*arguments)
    assert before <= set(db.scalars(select(Tenant.id)))
    assert set(db.scalars(select(User.id))) == set(original_users)
    for model, expected in [
        (IntegrationConnection, 10),
        (SyntheticCRMRecord, 10),
        (SyntheticATSRecord, 10),
        (SyntheticNotification, 2),
        (SyntheticEmail, 2),
        (SyntheticCalendarEvent, 4),
    ]:
        assert db.scalar(select(func.count()).select_from(model)) == expected
    assert db.scalar(select(func.count()).select_from(ConnectorDefinition)) == 6


@pytest.mark.parametrize("kind", [ConnectorType.EMAIL, ConnectorType.CALENDAR])
def test_concurrent_idempotency(pg_engine, kind):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    with Session(pg_engine) as db, db.begin():
        db.add(
            Tenant(
                id=tenant_id,
                name="Synthetic connector race",
                slug=f"connector-race-{uuid.uuid4().hex}",
            )
        )
        db.flush()
        db.add(
            User(
                id=user_id,
                email=f"race-{uuid.uuid4().hex}@example.test",
                display_name="Synthetic",
                password_hash="unused",
            )
        )
        db.flush()
        db.add(Membership(tenant_id=tenant_id, user_id=user_id, role="TENANT_ADMIN"))
        adapter = registry.resolve(kind)
        db.add(
            IntegrationConnection(
                id=connection_id,
                tenant_id=tenant_id,
                connector_type=kind.value,
                name="Concurrent simulator",
                public_config=adapter.validate_config({}).model_dump(),
            )
        )
    barrier = Barrier(2)
    operation = ConnectorOperation(
        name="message.send" if kind == ConnectorType.EMAIL else "event.create",
        payload={
            "recipient": "recruiter@example.test",
            "subject": "Synthetic",
            "body": "Synthetic",
            "idempotency_key": "concurrent-key-1",
        }
        if kind == ConnectorType.EMAIL
        else booking("concurrent-key-1"),
    )

    def run(_):
        barrier.wait()
        with Session(pg_engine) as db, db.begin():
            result = registry.resolve(kind).execute(
                db, tenant_id, connection_id, registry.resolve(kind).validate_config({}), operation
            )
            return result.data.id, result.data.duplicate

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0][0] == results[1][0] and sorted(result[1] for result in results) == [
        False,
        True,
    ]


def test_unexpected_adapter_failure_is_sanitized(connector_env):
    connection = create(connector_env)
    adapter = registry.resolve("synthetic_crm")
    with patch.object(adapter, "execute", side_effect=RuntimeError("private-provider-credential")):
        response = execute(connector_env, connection, "contact.list")
    assert response.status_code == 500
    assert response.json()["error"]["category"] == "INTERNAL_ERROR"
    assert response.json()["error"]["retryable"] is False
    assert "private-provider-credential" not in response.text


def test_connection_mutations_preserve_csrf_and_origin(connector_env):
    client = connector_env["client"]
    data = {"connector_type": "synthetic_crm", "name": "Denied without verification"}
    assert client.post("/api/v1/connections", json=data).status_code == 403
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    assert (
        client.post(
            "/api/v1/connections",
            json=data,
            headers={"Origin": "https://attacker.example.test", "X-CSRF-Token": token},
        ).status_code
        == 403
    )
