import json
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import Identity, TenantContext, assert_permission, authorized_context
from app.core.security import password_hasher, token_hash, utcnow
from app.models.identity import Invitation, Membership, User
from app.models.tenant import Tenant
from app.schemas.identity import AcceptInvitation, InvitationInput, MemberChange
from app.services.audit import record


def locked_admin_context(db: Session, context: TenantContext) -> TenantContext:
    # Every membership mutation locks the parent tenant, serializing last-admin checks.
    db.scalar(select(Tenant).where(Tenant.id == context.tenant.id).with_for_update())
    db.expire(context.membership)
    fresh = authorized_context(db, context.identity, context.tenant.id)
    assert_permission(fresh, "members:manage")
    return fresh


def deliver_invitation(request: Request, invitation: Invitation, raw: str) -> None:
    # Local filesystem delivery is deliberately inaccessible over HTTP.
    backend = Path(__file__).resolve().parents[2]
    directory = (backend / request.app.state.settings.invitation_delivery_dir).resolve()
    if not directory.is_relative_to(backend):
        raise RuntimeError("Invitation delivery must remain inside the backend repository")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise RuntimeError("Invitation delivery directory requires private permissions")
    path = directory / f"{invitation.id}.json"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(
            {
                "invitation_id": str(invitation.id),
                "email": invitation.email,
                "token": raw,
                "expires_at": invitation.expires_at.isoformat(),
                "accept_path": "/accept-invitation",
            },
            file,
        )


def create_invitation(
    db: Session, request: Request, context: TenantContext, data: InvitationInput
) -> Invitation:
    context = locked_admin_context(db, context)
    existing_user = db.scalar(select(User).where(User.email == data.email))
    if existing_user and db.scalar(
        select(Membership).where(
            Membership.tenant_id == context.tenant.id, Membership.user_id == existing_user.id
        )
    ):
        raise HTTPException(409, "Invitation cannot be created")
    pending = db.scalar(
        select(Invitation).where(
            Invitation.tenant_id == context.tenant.id,
            Invitation.email == data.email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > utcnow(),
        )
    )
    if pending:
        raise HTTPException(409, "Invitation cannot be created")
    raw = secrets.token_urlsafe(32)
    invitation = Invitation(
        tenant_id=context.tenant.id,
        email=data.email,
        role=data.role.value,
        token_hash=token_hash(raw),
        expires_at=utcnow() + timedelta(hours=request.app.state.settings.invitation_hours),
    )
    db.add(invitation)
    db.flush()
    record(
        db,
        "invitation.created",
        "invitation",
        invitation.id,
        context.tenant.id,
        context.identity.user.id,
        {"role": data.role.value},
    )
    deliver_invitation(request, invitation, raw)
    return invitation


def revoke_invitation(db: Session, context: TenantContext, invitation_id: uuid.UUID) -> None:
    context = locked_admin_context(db, context)
    invitation = db.scalar(
        select(Invitation)
        .where(Invitation.id == invitation_id, Invitation.tenant_id == context.tenant.id)
        .with_for_update()
    )
    if (
        not invitation
        or invitation.accepted_at
        or invitation.revoked_at
        or invitation.expires_at <= utcnow()
    ):
        raise HTTPException(404, "Pending invitation unavailable")
    invitation.revoked_at = utcnow()
    record(
        db,
        "invitation.revoked",
        "invitation",
        invitation.id,
        context.tenant.id,
        context.identity.user.id,
    )


def accept_invitation(db: Session, data: AcceptInvitation, identity: Identity | None) -> None:
    digest = token_hash(data.token.get_secret_value())
    # Locate only the parent first; all admin writes use tenant -> invitation lock ordering.
    tenant_id = db.scalar(select(Invitation.tenant_id).where(Invitation.token_hash == digest))
    if tenant_id is None:
        raise HTTPException(400, "Invitation unavailable")
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    invitation = db.scalar(
        select(Invitation).where(Invitation.token_hash == digest).with_for_update()
    )
    if (
        invitation is None
        or invitation.accepted_at
        or invitation.revoked_at
        or invitation.expires_at <= utcnow()
    ):
        raise HTTPException(400, "Invitation unavailable")
    # Serialize account creation across different tenants inviting the same email.
    from sqlalchemy import text

    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int(token_hash(invitation.email)[:15], 16)},
    )
    user = db.scalar(select(User).where(User.email == invitation.email))
    if user is not None:
        if identity is None or identity.user.id != user.id or not user.is_active:
            raise HTTPException(403, "Sign in as the invited account before accepting")
    else:
        if identity is not None:
            raise HTTPException(403, "Invitation does not belong to the signed-in account")
        if not data.password or not data.display_name or not data.display_name.strip():
            raise HTTPException(
                422, "Display name and a password of at least 12 characters required"
            )
        user = User(
            email=invitation.email,
            display_name=data.display_name.strip(),
            password_hash=password_hasher.hash(data.password.get_secret_value()),
        )
        db.add(user)
        db.flush()
    if db.scalar(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user.id)
    ):
        raise HTTPException(409, "Membership already exists")
    membership = Membership(
        tenant_id=tenant_id, user_id=user.id, role=invitation.role, status="ACTIVE"
    )
    db.add(membership)
    db.flush()
    invitation.accepted_at = utcnow()
    record(
        db,
        "invitation.accepted",
        "invitation",
        invitation.id,
        tenant_id,
        user.id,
        {"role": invitation.role},
    )


def change_member(
    db: Session, context: TenantContext, member_id: uuid.UUID, data: MemberChange
) -> Membership:
    context = locked_admin_context(db, context)
    member = db.scalar(
        select(Membership)
        .where(Membership.id == member_id, Membership.tenant_id == context.tenant.id)
        .with_for_update()
    )
    if not member:
        raise HTTPException(404, "Member unavailable")
    role, status = data.role.value if data.role else member.role, data.status or member.status
    if (
        member.role == "TENANT_ADMIN"
        and member.status == "ACTIVE"
        and db.get(User, member.user_id).is_active
        and (role != "TENANT_ADMIN" or status != "ACTIVE")
    ):
        count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == context.tenant.id,
                Membership.role == "TENANT_ADMIN",
                Membership.status == "ACTIVE",
                User.is_active.is_(True),
            )
        )
        if count <= 1:
            raise HTTPException(409, "At least one active tenant administrator is required")
    if role != member.role:
        record(
            db,
            "membership.role_changed",
            "membership",
            member.id,
            context.tenant.id,
            context.identity.user.id,
            {"previous_role": member.role, "role": role},
        )
    if status != member.status:
        record(
            db,
            "membership.reactivated" if status == "ACTIVE" else "membership.deactivated",
            "membership",
            member.id,
            context.tenant.id,
            context.identity.user.id,
            {"previous_status": member.status, "status": status},
        )
    member.role, member.status = role, status
    return member
