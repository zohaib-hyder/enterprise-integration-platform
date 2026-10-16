import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import cookie_names, token_hash, utcnow
from app.models.identity import AuthSession, Membership, User
from app.models.tenant import Tenant
from app.schemas.identity import Role

PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.TENANT_ADMIN: frozenset(
        {
            "tenant:read",
            "tenant:settings",
            "members:manage",
            "audit:read",
            "integrations:manage",
            "alerts:resolve",
            "operations:read",
            "executions:recover",
            "alerts:acknowledge",
            "sync:resolve",
        }
    ),
    Role.INTEGRATION_ENGINEER: frozenset(
        {
            "tenant:read",
            "integrations:manage",
            "alerts:resolve",
            "operations:read",
            "executions:recover",
            "alerts:acknowledge",
            "sync:resolve",
        }
    ),
    Role.OPERATIONS_USER: frozenset(
        {
            "tenant:read",
            "operations:read",
            "executions:recover",
            "alerts:acknowledge",
            "sync:resolve",
        }
    ),
}


def get_db(request: Request) -> Iterator[Session]:
    if request.app.state.engine is None:
        raise HTTPException(503, "Database unavailable")
    with Session(request.app.state.engine, expire_on_commit=False) as db:
        try:
            db.info["settings"] = request.app.state.settings
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


@dataclass
class Identity:
    user: User
    session: AuthSession


@dataclass
class TenantContext:
    identity: Identity
    tenant: Tenant
    membership: Membership


def optional_identity(request: Request, db: Session) -> Identity | None:
    name, _ = cookie_names(request)
    raw = request.cookies.get(name, "")
    if not raw or len(raw) > 128:
        return None
    session = db.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash(raw)).with_for_update()
    )
    if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    session.last_seen_at = utcnow()
    return Identity(user, session)


def require_authenticated_user(
    request: Request, db: Annotated[Session, Depends(get_db, scope="function")]
) -> Identity:
    identity = optional_identity(request, db)
    if identity is None:
        raise HTTPException(401, "Authentication required")
    return identity


def authorized_context(
    db: Session, identity: Identity, tenant_id: uuid.UUID | None
) -> TenantContext:
    if tenant_id is None:
        raise HTTPException(403, "Tenant access denied")
    membership = db.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == identity.user.id,
            Membership.status == "ACTIVE",
        )
    )
    if membership is None:
        raise HTTPException(403, "Tenant access denied")
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(403, "Tenant access denied")
    return TenantContext(identity, tenant, membership)


def require_tenant_membership(
    request: Request,
    identity: Annotated[Identity, Depends(require_authenticated_user)],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> TenantContext:
    tenant_id = request.path_params.get("tenant_id", identity.session.active_tenant_id)
    try:
        tenant_id = uuid.UUID(str(tenant_id)) if tenant_id is not None else None
    except ValueError:
        raise HTTPException(403, "Tenant access denied") from None
    return authorized_context(db, identity, tenant_id)


def assert_permission(context: TenantContext, permission: str) -> None:
    if permission not in PERMISSIONS[Role(context.membership.role)]:
        raise HTTPException(403, "Permission denied")


def require_tenant_permission(permission: str) -> Callable:
    def dependency(
        context: Annotated[TenantContext, Depends(require_tenant_membership)],
    ) -> TenantContext:
        assert_permission(context, permission)
        return context

    return dependency


def require_tenant_role(*roles: Role) -> Callable:
    def dependency(
        context: Annotated[TenantContext, Depends(require_tenant_membership)],
    ) -> TenantContext:
        if Role(context.membership.role) not in roles:
            raise HTTPException(403, "Permission denied")
        return context

    return dependency
