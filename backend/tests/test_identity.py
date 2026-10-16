import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import PERMISSIONS, Identity, authorized_context
from app.bootstrap import bootstrap
from app.core.config import Settings
from app.core.security import token_hash, utcnow
from app.models.identity import AuditEvent, AuthSession, Invitation, Membership, User
from app.models.tenant import Tenant
from app.schemas.identity import AcceptInvitation, MemberChange, Role
from app.services import tenancy
from app.services.audit import record

ORIGIN = "http://localhost:3000"


def mutation(client, method, path, data=None, **headers):
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    return client.request(
        method,
        "/api/v1/" + path,
        json=data or {},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, **headers},
    )


def login(env, role="TENANT_ADMIN"):
    return mutation(
        env["client"],
        "POST",
        "auth/login",
        {"email": env["users"][role].email, "password": env["password"]},
    )


def invitation(env, email="new@example.test", role="OPERATIONS_USER", tenant=None):
    response = mutation(
        env["client"],
        "POST",
        f"tenants/{tenant or env['a'].id}/invitations",
        {"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], env["tokens"][response.json()["id"]]


def test_valid_login_and_safe_cookie_and_session_hash(identity_env, db):
    env = identity_env
    with patch("app.main.logger.info") as logs:
        response = login(env)
    assert response.status_code == 200
    assert response.json()["user"]["email"] == env["users"]["TENANT_ADMIN"].email
    raw = env["client"].cookies["platform_session"]
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw)))
    assert session and session.token_hash != raw
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "SameSite=strict" in response.headers["set-cookie"]
    )
    for secret in [
        raw,
        env["password"],
        session.token_hash,
        env["users"]["TENANT_ADMIN"].password_hash,
    ]:
        assert secret not in response.text
        assert secret not in str(logs.call_args_list)
    assert env["client"].get("/api/v1/auth/me").status_code == 200


@pytest.mark.parametrize("email", ["tenant_admin@example.test", "unknown@example.test"])
def test_invalid_credentials_are_generic(identity_env, email, db):
    response = mutation(
        identity_env["client"], "POST", "auth/login", {"email": email, "password": "invalid"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "login.failed"))


def test_inactive_user(identity_env, db):
    db.get(User, identity_env["users"]["TENANT_ADMIN"].id).is_active = False
    db.commit()
    assert login(identity_env).status_code == 401


@pytest.mark.parametrize("state", ["expired", "revoked", "inactive_user"])
def test_unusable_session(identity_env, db, state):
    assert login(identity_env).status_code == 200
    session = db.scalar(select(AuthSession))
    if state == "expired":
        session.expires_at = utcnow() - timedelta(seconds=1)
    elif state == "revoked":
        session.revoked_at = utcnow()
    else:
        db.get(User, session.user_id).is_active = False
    db.commit()
    assert identity_env["client"].get("/api/v1/auth/me").status_code == 401


def test_logout_and_revocation(identity_env, db):
    login(identity_env)
    raw = identity_env["client"].cookies["platform_session"]
    assert mutation(identity_env["client"], "POST", "auth/logout").status_code == 200
    assert "platform_session" not in identity_env["client"].cookies
    assert identity_env["client"].get("/api/v1/auth/me").status_code == 401
    db.expire_all()
    assert (
        db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw))).revoked_at
        is not None
    )
    actions = set(db.scalars(select(AuditEvent.action)))
    assert {"login.succeeded", "logout", "session.revoked"} <= actions


@pytest.mark.parametrize(
    "failure",
    [
        "missing_header",
        "missing_cookie",
        "invalid",
        "tampered",
        "wrong_origin",
        "missing_origin",
        "cross_site",
    ],
)
def test_csrf_and_origin_denials(identity_env, failure):
    client = identity_env["client"]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    if failure == "missing_header":
        headers.pop("X-CSRF-Token")
    elif failure == "missing_cookie":
        client.cookies.clear()
    elif failure == "invalid":
        headers["X-CSRF-Token"] = "invalid"
    elif failure == "tampered":
        forged = csrf[:-1] + ("0" if csrf[-1] != "0" else "1")
        client.cookies.clear()
        client.cookies.set("platform_csrf", forged)
        headers["X-CSRF-Token"] = forged
    elif failure == "wrong_origin":
        headers["Origin"] = "https://attacker.example.test"
    elif failure == "missing_origin":
        headers.pop("Origin")
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "tenant_admin@example.test", "password": identity_env["password"]},
            headers=headers,
        ).status_code
        == 403
    )


def test_login_csrf_cannot_be_reused_after_authentication(identity_env):
    client = identity_env["client"]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "tenant_admin@example.test", "password": identity_env["password"]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        ).status_code
        == 200
    )
    client.cookies.set("platform_csrf", csrf)
    assert (
        client.post(
            "/api/v1/auth/logout", json={}, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
        ).status_code
        == 403
    )


def test_persistent_login_rate_limit(identity_env, db):
    identity_env["app"].state.settings.login_account_limit = 2
    for _ in range(2):
        assert (
            mutation(
                identity_env["client"],
                "POST",
                "auth/login",
                {"email": "unknown@example.test", "password": "invalid"},
            ).status_code
            == 401
        )
    response = mutation(
        identity_env["client"],
        "POST",
        "auth/login",
        {"email": "unknown@example.test", "password": "invalid"},
    )
    assert response.status_code == 429 and "retry-after" in response.headers
    assert db.scalar(
        select(AuditEvent).where(AuditEvent.details["reason"].as_string() == "rate_limited")
    )


@pytest.mark.parametrize("role", list(Role))
def test_backend_role_matrix(identity_env, role):
    env = identity_env
    assert login(env, role.value).status_code == 200
    client = env["client"]
    assert client.get("/api/v1/tenant").status_code == 200
    expected = 200 if role == Role.TENANT_ADMIN else 403
    assert client.get(f"/api/v1/tenants/{env['a'].id}/members").status_code == expected
    assert client.get(f"/api/v1/tenants/{env['a'].id}/audit").status_code == expected
    expected_invite = 201 if role == Role.TENANT_ADMIN else 403
    assert (
        mutation(
            client,
            "POST",
            f"tenants/{env['a'].id}/invitations",
            {"email": "invite@example.test", "role": "OPERATIONS_USER"},
        ).status_code
        == expected_invite
    )
    assert ("members:manage" in PERMISSIONS[role]) == (role == Role.TENANT_ADMIN)
    assert ("integrations:manage" in PERMISSIONS[role]) == (role != Role.OPERATIONS_USER)


@pytest.mark.parametrize("route", ["members", "invitations", "audit"])
def test_cross_tenant_denial(identity_env, route):
    env = identity_env
    login(env, "OPERATIONS_USER")
    response = env["client"].get(f"/api/v1/tenants/{env['b'].id}/{route}")
    assert response.status_code == 403
    assert env["b"].name not in response.text
    assert str(env["b"].id) not in response.text


def test_tenant_switch_verified_memberships(identity_env):
    env = identity_env
    login(env)
    response = mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)})
    assert response.status_code == 200 and response.json()["active_tenant_id"] == str(env["b"].id)
    assert env["client"].get("/api/v1/tenant").json()["name"] == env["b"].name
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(uuid.uuid4())}
        ).status_code
        == 403
    )
    mutation(env["client"], "POST", "auth/logout")
    login(env, "OPERATIONS_USER")
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["b"].id)}
        ).status_code
        == 403
    )
    assert len(env["client"].get("/api/v1/tenants").json()) == 1


@pytest.mark.parametrize("change", [{"role": "OPERATIONS_USER"}, {"status": "INACTIVE"}])
def test_last_admin_protected(identity_env, change):
    env = identity_env
    login(env)
    response = mutation(
        env["client"],
        "PATCH",
        f"tenants/{env['a'].id}/members/{env['members']['TENANT_ADMIN'].id}",
        change,
    )
    assert response.status_code == 409


def test_membership_deactivate_reactivate_and_role_change(identity_env, db):
    env = identity_env
    login(env)
    path = f"tenants/{env['a'].id}/members/{env['members']['OPERATIONS_USER'].id}"
    assert mutation(env["client"], "PATCH", path, {"status": "INACTIVE"}).status_code == 200
    assert (
        mutation(
            env["client"], "PATCH", path, {"status": "ACTIVE", "role": "INTEGRATION_ENGINEER"}
        ).status_code
        == 200
    )
    events = set(db.scalars(select(AuditEvent.action)))
    assert {"membership.deactivated", "membership.reactivated", "membership.role_changed"} <= events


def test_revoked_membership_denied_immediately(identity_env, db):
    env = identity_env
    login(env, "OPERATIONS_USER")
    db.get(Membership, env["members"]["OPERATIONS_USER"].id).status = "INACTIVE"
    db.commit()
    assert env["client"].get("/api/v1/tenant").status_code == 403
    assert env["client"].get("/api/v1/auth/me").json()["tenants"] == []


def test_cross_tenant_target_ids_do_not_escape_scope(identity_env, db):
    env = identity_env
    login(env)
    other = db.scalar(select(Membership).where(Membership.tenant_id == env["b"].id))
    assert (
        mutation(
            env["client"],
            "PATCH",
            f"tenants/{env['a'].id}/members/{other.id}",
            {"status": "INACTIVE"},
        ).status_code
        == 404
    )
    invite_id, _ = invitation(env, tenant=env["b"].id)
    assert (
        mutation(
            env["client"], "DELETE", f"tenants/{env['a'].id}/invitations/{invite_id}"
        ).status_code
        == 404
    )


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_invitation_unavailable(identity_env, db, state):
    env = identity_env
    login(env)
    invite_id, raw = invitation(env)
    if state == "expired":
        db.get(Invitation, uuid.UUID(invite_id)).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    else:
        assert (
            mutation(
                env["client"], "DELETE", f"tenants/{env['a'].id}/invitations/{invite_id}"
            ).status_code
            == 200
        )
    mutation(env["client"], "POST", "auth/logout")
    response = mutation(
        env["client"],
        "POST",
        "invitations/accept",
        {"token": raw, "display_name": "New Synthetic", "password": env["password"]},
    )
    assert response.status_code == 400
    assert db.scalar(select(User).where(User.email == "new@example.test")) is None


def test_new_invitation_acceptance_one_time_and_redaction(identity_env, db):
    env = identity_env
    login(env)
    invite_id, raw = invitation(env)
    assert db.get(Invitation, uuid.UUID(invite_id)).token_hash == token_hash(raw)
    assert raw not in env["client"].get(f"/api/v1/tenants/{env['a'].id}/invitations").text
    mutation(env["client"], "POST", "auth/logout")
    data = {"token": raw, "display_name": "New Synthetic", "password": env["password"]}
    assert mutation(env["client"], "POST", "invitations/accept", data).status_code == 200
    assert mutation(env["client"], "POST", "invitations/accept", data).status_code == 400
    new = db.scalar(select(User).where(User.email == "new@example.test"))
    assert new and new.password_hash.startswith("$argon2id$")
    assert (
        db.scalar(select(Membership).where(Membership.user_id == new.id)).tenant_id == env["a"].id
    )


def test_existing_user_must_authenticate_as_invitee(identity_env):
    env = identity_env
    login(env)
    _, raw = invitation(env, env["outsider"].email)
    assert mutation(env["client"], "POST", "invitations/accept", {"token": raw}).status_code == 403
    mutation(env["client"], "POST", "auth/logout")
    assert (
        mutation(
            env["client"],
            "POST",
            "invitations/accept",
            {"token": raw, "display_name": "Takeover", "password": env["password"]},
        ).status_code
        == 403
    )
    assert (
        mutation(
            env["client"],
            "POST",
            "auth/login",
            {"email": env["outsider"].email, "password": env["password"]},
        ).status_code
        == 200
    )
    assert mutation(env["client"], "POST", "invitations/accept", {"token": raw}).status_code == 200
    assert (
        mutation(
            env["client"], "POST", "tenant/switch", {"tenant_id": str(env["a"].id)}
        ).status_code
        == 200
    )


def test_audit_metadata_is_allowlisted(identity_env, db):
    record(
        db,
        "test",
        "tenant",
        tenant_id=identity_env["a"].id,
        metadata={
            "password": "never-store",
            "token": "never-store",
            "role": "OPERATIONS_USER",
            "reason": "credential-string",
        },
    )
    db.commit()
    login(identity_env)
    response = identity_env["client"].get(f"/api/v1/tenants/{identity_env['a'].id}/audit")
    assert response.status_code == 200 and "never-store" not in response.text
    assert next(item for item in response.json()["items"] if item["action"] == "test")[
        "metadata"
    ] == {"role": "OPERATIONS_USER"}


@pytest.mark.parametrize(
    "constraint",
    [
        "role",
        "status",
        "membership_unique",
        "email_unique",
        "normalized_email",
        "foreign_key",
        "session_hash_unique",
    ],
)
def test_postgresql_database_constraints(identity_env, db, constraint):
    env = identity_env
    with pytest.raises(IntegrityError), db.begin_nested():
        if constraint in {"role", "status", "membership_unique", "foreign_key"}:
            user_id = (
                env["outsider"].id
                if constraint != "membership_unique"
                else env["users"]["TENANT_ADMIN"].id
            )
            db.add(
                Membership(
                    tenant_id=uuid.uuid4() if constraint == "foreign_key" else env["a"].id,
                    user_id=user_id,
                    role="INVALID" if constraint == "role" else "OPERATIONS_USER",
                    status="INVALID" if constraint == "status" else "ACTIVE",
                )
            )
        elif constraint in {"email_unique", "normalized_email"}:
            db.add(
                User(
                    email=env["outsider"].email
                    if constraint == "email_unique"
                    else "UPPER@example.test",
                    password_hash="not-a-real-hash",
                    display_name="Synthetic constraint",
                )
            )
        else:
            for _ in range(2):
                db.add(
                    AuthSession(
                        user_id=env["outsider"].id,
                        token_hash=token_hash("synthetic-test"),
                        expires_at=utcnow() + timedelta(hours=1),
                    )
                )
        db.flush()


def test_pagination_and_input_validation(identity_env):
    env = identity_env
    login(env)
    client = env["client"]
    response = client.get(f"/api/v1/tenants/{env['a'].id}/members?limit=1&offset=1")
    assert (
        response.status_code == 200
        and response.json()["total"] == 3
        and len(response.json()["items"]) == 1
    )
    assert client.get(f"/api/v1/tenants/{env['a'].id}/audit?limit=101").status_code == 422
    response = mutation(
        client,
        "POST",
        f"tenants/{env['a'].id}/invitations",
        {"email": "invalid", "role": "TENANT_ADMIN", "token": "private-secret"},
    )
    assert response.status_code == 422 and "private-secret" not in response.text


def test_bootstrap_is_idempotent_without_password_reset(identity_env, db):
    env = identity_env
    bootstrap(
        db,
        env["users"]["TENANT_ADMIN"].email,
        env["password"],
        "Admin",
        env["a"].name,
        env["a"].slug,
    )
    db.flush()
    with pytest.raises(ValueError):
        bootstrap(
            db,
            env["users"]["TENANT_ADMIN"].email,
            "wrong-credential-long",
            "Admin",
            env["a"].name,
            env["a"].slug,
        )


def test_concurrent_invitation_acceptance_is_one_time(pg_engine):
    raw = secrets.token_urlsafe(32)
    email = f"race-{uuid.uuid4().hex}@example.test"
    with Session(pg_engine) as db, db.begin():
        tenant = Tenant(name="Synthetic race", slug=f"race-{uuid.uuid4().hex}")
        db.add(tenant)
        db.flush()
        db.add(
            Invitation(
                tenant_id=tenant.id,
                email=email,
                role="OPERATIONS_USER",
                token_hash=token_hash(raw),
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
    barrier = Barrier(2)

    def accept_once():
        barrier.wait()
        try:
            with Session(pg_engine) as db, db.begin():
                tenancy.accept_invitation(
                    db,
                    AcceptInvitation(
                        token=raw, password=secrets.token_urlsafe(24), display_name="Synthetic race"
                    ),
                    None,
                )
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: accept_once(), range(2))) == [200, 400]


def test_concurrent_last_admin_changes_preserve_one(pg_engine):
    tenant_id = uuid.uuid4()
    with Session(pg_engine) as db, db.begin():
        db.add(
            Tenant(id=tenant_id, name="Synthetic admin race", slug=f"admin-race-{uuid.uuid4().hex}")
        )
        db.flush()
        member_ids, user_ids, session_ids = [], [], []
        for _ in range(2):
            user = User(
                email=f"admin-race-{uuid.uuid4().hex}@example.test",
                display_name="Synthetic admin",
                password_hash="unused-hash",
            )
            db.add(user)
            db.flush()
            member = Membership(tenant_id=tenant_id, user_id=user.id, role="TENANT_ADMIN")
            session = AuthSession(
                user_id=user.id,
                token_hash=token_hash(secrets.token_urlsafe(32)),
                expires_at=utcnow() + timedelta(hours=1),
            )
            db.add_all([member, session])
            db.flush()
            member_ids.append(member.id)
            user_ids.append(user.id)
            session_ids.append(session.id)
    barrier = Barrier(2)

    def demote(index):
        barrier.wait()
        try:
            with Session(pg_engine) as db, db.begin():
                identity = Identity(
                    db.get(User, user_ids[index]), db.get(AuthSession, session_ids[index])
                )
                context = authorized_context(db, identity, tenant_id)
                tenancy.change_member(
                    db, context, member_ids[index], MemberChange(role="OPERATIONS_USER")
                )
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(demote, range(2))) == [200, 409]
    with Session(pg_engine) as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(Membership).where(
                            Membership.tenant_id == tenant_id,
                            Membership.role == "TENANT_ADMIN",
                            Membership.status == "ACTIVE",
                        )
                    )
                )
            )
            == 1
        )


def test_admin_cannot_access_unjoined_tenant(identity_env, db):
    env = identity_env
    private = Tenant(name="Unjoined confidential synthetic tenant", slug="private-synthetic")
    db.add(private)
    db.flush()
    member = Membership(tenant_id=private.id, user_id=env["outsider"].id, role="TENANT_ADMIN")
    db.add(member)
    db.commit()
    login(env)
    for suffix in ["members", "invitations", "audit"]:
        response = env["client"].get(f"/api/v1/tenants/{private.id}/{suffix}")
        assert response.status_code == 403 and private.name not in response.text
    assert (
        mutation(
            env["client"],
            "POST",
            f"tenants/{private.id}/invitations",
            {"email": "new@example.test", "role": "OPERATIONS_USER"},
        ).status_code
        == 403
    )
    assert (
        mutation(
            env["client"],
            "PATCH",
            f"tenants/{private.id}/members/{member.id}",
            {"role": "OPERATIONS_USER"},
        ).status_code
        == 403
    )
    assert (
        mutation(env["client"], "POST", "tenant/switch", {"tenant_id": str(private.id)}).status_code
        == 403
    )


@pytest.mark.parametrize("role", list(Role))
def test_tenant_settings_permissions(identity_env, role):
    env = identity_env
    login(env, role.value)
    response = mutation(env["client"], "PATCH", "tenant", {"name": "Updated synthetic name"})
    assert response.status_code == (200 if role == Role.TENANT_ADMIN else 403)


def test_session_revocation_is_owner_scoped(identity_env, db):
    env = identity_env
    login(env)
    foreign = AuthSession(
        user_id=env["outsider"].id,
        token_hash=token_hash(secrets.token_urlsafe(32)),
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(foreign)
    db.commit()
    assert mutation(env["client"], "POST", f"auth/sessions/{foreign.id}/revoke").status_code == 404
    own_id = env["client"].get("/api/v1/auth/me").json()["session_id"]
    assert mutation(env["client"], "POST", f"auth/sessions/{own_id}/revoke").status_code == 200
    assert env["client"].get("/api/v1/auth/me").status_code == 401


def test_logout_requires_csrf(identity_env):
    env = identity_env
    login(env)
    assert (
        env["client"].post("/api/v1/auth/logout", json={}, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    assert env["client"].get("/api/v1/auth/me").status_code == 200


def test_production_configuration_requires_secure_origins_and_secret():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+psycopg://user:private@localhost/db",
            csrf_secret="x" * 32,
        )
    config = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://user:private@localhost/db",
        csrf_secret="x" * 32,
        auth_origins=["https://workspace.example.test"],
        webhook_encryption_key="d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3c=",
        credential_encryption_key="Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
        allowed_hosts=["workspace.example.test"],
    )
    assert "private" not in repr(config)


def test_expired_csrf_is_rejected_without_sleep(identity_env):
    env = identity_env
    client = env["client"]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    with patch("app.core.security.utcnow", return_value=utcnow() + timedelta(hours=2)):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "tenant_admin@example.test", "password": env["password"]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
    assert response.status_code == 403


def test_ip_rate_limit_covers_rotating_account_names(identity_env):
    env = identity_env
    env["app"].state.settings.login_ip_limit = 2
    for index in range(2):
        assert (
            mutation(
                env["client"],
                "POST",
                "auth/login",
                {"email": f"unknown-{index}@example.test", "password": "invalid"},
            ).status_code
            == 401
        )
    assert (
        mutation(
            env["client"],
            "POST",
            "auth/login",
            {"email": "another@example.test", "password": "invalid"},
        ).status_code
        == 429
    )


def test_production_session_cookie_security(identity_env):
    from fastapi.testclient import TestClient

    env = identity_env
    production_settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://user:private@localhost/db",
        csrf_secret="x" * 32,
        auth_origins=["https://workspace.example.test"],
        webhook_encryption_key="d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3c=",
        credential_encryption_key="Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=",
        allowed_hosts=["workspace.example.test"],
    )
    from app.main import create_app

    production_app = create_app(production_settings)
    production_app.dependency_overrides.update(env["app"].dependency_overrides)
    with TestClient(production_app, base_url="https://workspace.example.test") as client:
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        response = client.post(
            "/api/v1/auth/login",
            json={"email": env["users"]["TENANT_ADMIN"].email, "password": env["password"]},
            headers={"Origin": "https://workspace.example.test", "X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(
            cookie for cookie in cookies if cookie.startswith("__Host-platform_session=")
        )
        assert all(
            flag in session_cookie for flag in ["HttpOnly", "Secure", "SameSite=strict", "Path=/"]
        )
        assert "Domain=" not in session_cookie
        assert client.get("/api/v1/auth/me").status_code == 200


def test_commit_failure_cannot_return_authenticated_cookie(identity_env):
    from app.api.dependencies import get_db

    env = identity_env

    def failing_transaction():
        with Session(env["connection"], join_transaction_mode="create_savepoint") as db:
            try:
                yield db
                raise RuntimeError("private-commit-details")
            finally:
                db.rollback()

    original = env["app"].dependency_overrides[get_db]
    env["app"].dependency_overrides[get_db] = failing_transaction
    try:
        response = login(env)
        assert response.status_code == 500
        assert "platform_session=" not in response.headers.get("set-cookie", "")
        assert "private-commit-details" not in response.text
    finally:
        env["app"].dependency_overrides[get_db] = original
    assert env["client"].get("/api/v1/auth/me").status_code == 401


def test_inactive_identity_is_not_an_effective_last_admin(identity_env, db):
    env = identity_env
    inactive = User(
        email="inactive-admin@example.test",
        display_name="Synthetic inactive administrator",
        password_hash="unused-hash",
        is_active=False,
    )
    db.add(inactive)
    db.flush()
    membership = Membership(tenant_id=env["a"].id, user_id=inactive.id, role="TENANT_ADMIN")
    db.add(membership)
    db.commit()
    login(env)
    assert (
        mutation(
            env["client"],
            "PATCH",
            f"tenants/{env['a'].id}/members/{membership.id}",
            {"status": "INACTIVE"},
        ).status_code
        == 200
    )
    assert (
        mutation(
            env["client"],
            "PATCH",
            f"tenants/{env['a'].id}/members/{env['members']['TENANT_ADMIN'].id}",
            {"status": "INACTIVE"},
        ).status_code
        == 409
    )
