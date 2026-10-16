import secrets
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.dependencies import PERMISSIONS, Identity
from app.core.security import (
    DUMMY_HASH,
    cookie_names,
    password_hasher,
    token_hash,
    utcnow,
    verify_password,
)
from app.models.identity import AuthSession, LoginRateBucket, Membership, User
from app.models.tenant import Tenant
from app.schemas.identity import MembershipOutput, MeOutput, Role, TenantOutput, UserOutput
from app.services.audit import record


def consume_login_limit(db: Session, request: Request, email: str) -> bool:
    settings = request.app.state.settings
    now = utcnow()
    window = int(now.timestamp()) // settings.login_window_seconds
    allowed = True
    # No trusted forwarded-IP support in Phase 1: the BFF shares a conservative IP bucket.
    address = request.client.host if request.client else "unknown"
    for scope, value, limit in [
        ("account", email, settings.login_account_limit),
        ("ip", address, settings.login_ip_limit),
    ]:
        key = token_hash(f"{scope}:{value}")
        statement = insert(LoginRateBucket).values(
            key=key,
            window=window,
            attempts=1,
            expires_at=now + timedelta(seconds=settings.login_window_seconds * 2),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[LoginRateBucket.key, LoginRateBucket.window],
            set_={"attempts": LoginRateBucket.attempts + 1},
        ).returning(LoginRateBucket.attempts)
        attempts = db.scalar(statement)
        allowed = allowed and attempts <= limit
    db.execute(delete(LoginRateBucket).where(LoginRateBucket.expires_at < now))
    return allowed


def login(db: Session, request: Request, email: str, password: str) -> tuple[Identity, str]:
    if not consume_login_limit(db, request, email):
        record(db, "login.failed", "authentication", metadata={"reason": "rate_limited"})
        db.commit()
        raise HTTPException(
            429,
            "Authentication temporarily unavailable",
            headers={"Retry-After": str(request.app.state.settings.login_window_seconds)},
        )
    user = db.scalar(select(User).where(User.email == email).with_for_update())
    valid = verify_password(user.password_hash if user else DUMMY_HASH, password)
    if not valid or user is None or not user.is_active:
        record(db, "login.failed", "authentication", metadata={"reason": "invalid_credentials"})
        db.commit()
        raise HTTPException(401, "Invalid email or password")
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = password_hasher.hash(password)
    tenant_id = db.scalar(
        select(Membership.tenant_id)
        .where(Membership.user_id == user.id, Membership.status == "ACTIVE")
        .order_by(Membership.created_at, Membership.id)
    )
    name, _ = cookie_names(request)
    previous_raw = request.cookies.get(name)
    if previous_raw:
        previous = db.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == token_hash(previous_raw))
            .with_for_update()
        )
        if previous and previous.user_id == user.id and previous.revoked_at is None:
            previous.revoked_at = utcnow()
            record(db, "session.revoked", "session", previous.id, actor_id=user.id)
    raw = secrets.token_urlsafe(32)
    session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(raw),
        active_tenant_id=tenant_id,
        expires_at=utcnow() + timedelta(hours=request.app.state.settings.session_hours),
    )
    db.add(session)
    db.flush()
    record(db, "login.succeeded", "session", session.id, actor_id=user.id)
    return Identity(user, session), raw


def me(db: Session, identity: Identity) -> MeOutput:
    tenants = [
        TenantOutput(id=tenant.id, name=tenant.name, slug=tenant.slug, role=membership.role)
        for tenant, membership in db.execute(
            select(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Membership.user_id == identity.user.id, Membership.status == "ACTIVE")
            .order_by(Tenant.name, Tenant.id)
        )
    ]
    active = next(
        (tenant for tenant in tenants if tenant.id == identity.session.active_tenant_id), None
    )
    memberships = [
        MembershipOutput(id=row.id, tenant_id=row.tenant_id, role=row.role, status=row.status)
        for row in db.scalars(
            select(Membership)
            .where(Membership.user_id == identity.user.id)
            .order_by(Membership.created_at, Membership.id)
        )
    ]
    return MeOutput(
        memberships=memberships,
        session_id=identity.session.id,
        user=UserOutput(
            id=identity.user.id, email=identity.user.email, display_name=identity.user.display_name
        ),
        tenants=tenants,
        active_tenant_id=active.id if active else None,
        permissions=sorted(PERMISSIONS[Role(active.role)]) if active else [],
    )
