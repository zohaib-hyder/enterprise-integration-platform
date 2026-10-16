import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import (
    Identity,
    TenantContext,
    authorized_context,
    get_db,
    optional_identity,
    require_authenticated_user,
    require_tenant_membership,
    require_tenant_permission,
)
from app.core.security import cookie_names, issue_csrf, require_csrf, set_cookie, utcnow
from app.models.identity import AuditEvent, AuthSession, Invitation, Membership, User
from app.schemas.identity import (
    AcceptInvitation,
    AuditOutput,
    AuditPage,
    InvitationInput,
    InvitationOutput,
    InvitationPage,
    LoginInput,
    MemberChange,
    MemberOutput,
    MemberPage,
    MeOutput,
    MessageOutput,
    TenantOutput,
    TenantSelection,
    TenantSettingsInput,
)
from app.services import authentication, tenancy
from app.services.audit import record

router = APIRouter(prefix="/api/v1")
mutations = [Depends(require_csrf)]


def invitation_output(invitation: Invitation) -> InvitationOutput:
    status = (
        "ACCEPTED"
        if invitation.accepted_at
        else "REVOKED"
        if invitation.revoked_at
        else "EXPIRED"
        if invitation.expires_at <= utcnow()
        else "PENDING"
    )
    return InvitationOutput(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        status=status,
    )


def member_output(member: Membership, user: User) -> MemberOutput:
    return MemberOutput(
        id=member.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=member.role,
        status=member.status,
        created_at=member.created_at,
    )


@router.get("/auth/csrf")
def csrf(request: Request, response: Response) -> dict[str, str]:
    # This is a request-verification token, never an authentication/session token.
    return {"csrf_token": issue_csrf(request, response)}


@router.post("/auth/login", response_model=MeOutput, dependencies=mutations)
def login(
    data: LoginInput,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MeOutput:
    identity, raw = authentication.login(db, request, data.email, data.password.get_secret_value())
    name, csrf_name = cookie_names(request)
    set_cookie(request, response, name, raw, request.app.state.settings.session_hours * 3600)
    response.delete_cookie(
        csrf_name,
        path="/",
        secure=request.app.state.settings.app_env == "production",
        httponly=True,
        samesite="strict",
    )
    return authentication.me(db, identity)


@router.get("/auth/me", response_model=MeOutput)
def me(
    identity: Annotated[Identity, Depends(require_authenticated_user)],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MeOutput:
    return authentication.me(db, identity)


@router.post("/auth/logout", response_model=MessageOutput, dependencies=mutations)
def logout(
    request: Request,
    response: Response,
    identity: Annotated[Identity, Depends(require_authenticated_user)],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MessageOutput:
    identity.session.revoked_at = utcnow()
    record(db, "logout", "session", identity.session.id, actor_id=identity.user.id)
    record(db, "session.revoked", "session", identity.session.id, actor_id=identity.user.id)
    for name in cookie_names(request):
        response.delete_cookie(
            name,
            path="/",
            secure=request.app.state.settings.app_env == "production",
            httponly=True,
            samesite="strict",
        )
    return MessageOutput(message="Signed out")


@router.post(
    "/auth/sessions/{session_id}/revoke", response_model=MessageOutput, dependencies=mutations
)
def revoke_session(
    session_id: uuid.UUID,
    identity: Annotated[Identity, Depends(require_authenticated_user)],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MessageOutput:
    session = db.scalar(
        select(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.user_id == identity.user.id)
        .with_for_update()
    )
    if not session:
        raise HTTPException(404, "Session unavailable")
    if not session.revoked_at:
        session.revoked_at = utcnow()
        record(db, "session.revoked", "session", session.id, actor_id=identity.user.id)
    return MessageOutput(message="Session revoked")


@router.get("/tenants", response_model=list[TenantOutput])
def tenants(
    identity: Annotated[Identity, Depends(require_authenticated_user)],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> list[TenantOutput]:
    return authentication.me(db, identity).tenants


@router.get("/tenant", response_model=TenantOutput)
def current_tenant(
    context: Annotated[TenantContext, Depends(require_tenant_membership)],
) -> TenantOutput:
    return TenantOutput(
        id=context.tenant.id,
        name=context.tenant.name,
        slug=context.tenant.slug,
        role=context.membership.role,
    )


@router.post("/tenant/switch", response_model=MeOutput, dependencies=mutations)
def switch(
    data: TenantSelection,
    identity: Annotated[Identity, Depends(require_authenticated_user)],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MeOutput:
    context = authorized_context(db, identity, data.tenant_id)
    identity.session.active_tenant_id = context.tenant.id
    record(
        db,
        "tenant.context_changed",
        "tenant",
        context.tenant.id,
        context.tenant.id,
        identity.user.id,
    )
    return authentication.me(db, identity)


@router.get("/tenants/{tenant_id}/members", response_model=MemberPage)
def members(
    *,
    tenant_id: uuid.UUID,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    context: Annotated[TenantContext, Depends(require_tenant_permission("members:manage"))],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MemberPage:
    where = Membership.tenant_id == context.tenant.id
    total = db.scalar(select(func.count()).select_from(Membership).where(where))
    rows = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(where)
        .order_by(Membership.created_at, Membership.id)
        .offset(offset)
        .limit(limit)
    )
    return MemberPage(
        items=[member_output(member, user) for member, user in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.patch(
    "/tenants/{tenant_id}/members/{member_id}", response_model=MemberOutput, dependencies=mutations
)
def update_member(
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    data: MemberChange,
    context: Annotated[TenantContext, Depends(require_tenant_permission("members:manage"))],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MemberOutput:
    member = tenancy.change_member(db, context, member_id, data)
    return member_output(member, db.get(User, member.user_id))


@router.get("/tenants/{tenant_id}/invitations", response_model=InvitationPage)
def invitations(
    *,
    tenant_id: uuid.UUID,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    context: Annotated[TenantContext, Depends(require_tenant_permission("members:manage"))],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> InvitationPage:
    where = Invitation.tenant_id == context.tenant.id
    total = db.scalar(select(func.count()).select_from(Invitation).where(where))
    rows = db.scalars(
        select(Invitation)
        .where(where)
        .order_by(Invitation.created_at.desc(), Invitation.id)
        .offset(offset)
        .limit(limit)
    )
    return InvitationPage(
        items=[invitation_output(row) for row in rows], total=total, offset=offset, limit=limit
    )


@router.post(
    "/tenants/{tenant_id}/invitations",
    response_model=InvitationOutput,
    status_code=201,
    dependencies=mutations,
)
def invite(
    tenant_id: uuid.UUID,
    data: InvitationInput,
    request: Request,
    context: Annotated[TenantContext, Depends(require_tenant_permission("members:manage"))],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> InvitationOutput:
    return invitation_output(tenancy.create_invitation(db, request, context, data))


@router.delete(
    "/tenants/{tenant_id}/invitations/{invitation_id}",
    response_model=MessageOutput,
    dependencies=mutations,
)
def revoke_invitation(
    tenant_id: uuid.UUID,
    invitation_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(require_tenant_permission("members:manage"))],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MessageOutput:
    tenancy.revoke_invitation(db, context, invitation_id)
    return MessageOutput(message="Invitation revoked")


@router.post("/invitations/accept", response_model=MessageOutput, dependencies=mutations)
def accept(
    data: AcceptInvitation,
    request: Request,
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> MessageOutput:
    identity = optional_identity(request, db)
    # Do not silently treat an expired logged-in browser as a new account.
    if request.cookies.get(cookie_names(request)[0]) and identity is None:
        raise HTTPException(401, "Authentication required")
    tenancy.accept_invitation(db, data, identity)
    return MessageOutput(message="Invitation accepted. Sign in or select your organization.")


@router.get("/tenants/{tenant_id}/audit", response_model=AuditPage)
def audit(
    *,
    tenant_id: uuid.UUID,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    context: Annotated[TenantContext, Depends(require_tenant_permission("audit:read"))],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> AuditPage:
    where = AuditEvent.tenant_id == context.tenant.id
    total = db.scalar(select(func.count()).select_from(AuditEvent).where(where))
    rows = db.scalars(
        select(AuditEvent)
        .where(where)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .offset(offset)
        .limit(limit)
    )
    return AuditPage(
        items=[
            AuditOutput(
                id=row.id,
                actor_user_id=row.actor_user_id,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                metadata=row.details,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.patch("/tenant", response_model=TenantOutput, dependencies=mutations)
def tenant_settings(
    data: TenantSettingsInput,
    context: Annotated[TenantContext, Depends(require_tenant_permission("tenant:settings"))],
    db: Annotated[Session, Depends(get_db, scope="function")],
) -> TenantOutput:
    context = tenancy.locked_admin_context(db, context)
    context.tenant.name = data.name
    record(
        db,
        "tenant.settings_changed",
        "tenant",
        context.tenant.id,
        context.tenant.id,
        context.identity.user.id,
    )
    return TenantOutput(
        id=context.tenant.id,
        name=context.tenant.name,
        slug=context.tenant.slug,
        role=context.membership.role,
    )
