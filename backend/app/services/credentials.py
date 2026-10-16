"""Context-bound AES-GCM inventory and deterministic local OAuth-style rotation."""

import hashlib
import hmac
import json
import os
import uuid
from datetime import timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.connectors.contracts import ErrorCategory
from app.connectors.reliability import ReliabilityError
from app.core.config import Settings
from app.core.security import utcnow
from app.core.webhook_security import decode_key
from app.models.integration import IntegrationConnection
from app.models.reliability import ConnectionCredential, CredentialRefreshAttempt
from app.schemas.reliability import CredentialMetadata, CredentialSubmission
from app.services.audit import record
from app.services.observability import emit, resolve_connection_alerts
from app.services.webhooks import manage_lock


def key(settings: Settings, reference: str | None = None) -> bytes:
    reference = reference or settings.credential_key_reference
    value = (
        settings.credential_encryption_key
        if reference == settings.credential_key_reference
        else settings.credential_previous_keys.get(reference)
    )
    if value is None:
        raise ReliabilityError(
            ErrorCategory.TEMPORARY_UNAVAILABLE, "ENCRYPTION_UNAVAILABLE", retryable=False
        )
    return decode_key(value.get_secret_value())


def aad(row: ConnectionCredential) -> bytes:
    return f"northstar:credential:v1:{row.tenant_id}:{row.connection_id}:{row.id}:{row.credential_type}:{row.generation}:{row.key_reference}".encode()


def decrypt(settings: Settings, row: ConnectionCredential) -> str:
    try:
        payload = AESGCM(key(settings, row.key_reference)).decrypt(
            row.nonce, row.encrypted_payload, aad(row)
        )
        data = json.loads(payload)
        if (
            data.get("issuer") != "northstar-local"
            or not isinstance(data.get("value"), str)
            or not data["value"].startswith("sim_")
        ):
            raise ValueError
        return data["value"]
    except (InvalidTag, ValueError, TypeError, UnicodeDecodeError):
        raise ReliabilityError(
            ErrorCategory.TEMPORARY_UNAVAILABLE, "ENCRYPTION_UNAVAILABLE", retryable=False
        ) from None


def latest(
    db: Session, connection: IntegrationConnection, credential_type: str
) -> ConnectionCredential | None:
    return db.scalar(
        select(ConnectionCredential)
        .where(
            ConnectionCredential.tenant_id == connection.tenant_id,
            ConnectionCredential.connection_id == connection.id,
            ConnectionCredential.credential_type == credential_type,
        )
        .order_by(ConnectionCredential.generation.desc())
        .limit(1)
    )


def metadata(row: ConnectionCredential) -> dict:
    status = (
        "EXPIRED"
        if row.status == "ACTIVE" and row.expires_at is not None and row.expires_at <= utcnow()
        else row.status
    )
    data = {
        k: getattr(row, k)
        for k in CredentialMetadata.model_fields
        if k not in {"masked_value", "status"}
    }
    return CredentialMetadata(**data, status=status).model_dump(mode="json")


def insert(
    db: Session,
    settings: Settings,
    connection: IntegrationConnection,
    data: CredentialSubmission,
    previous: ConnectionCredential | None = None,
) -> ConnectionCredential:
    cipher = AESGCM(key(settings))  # fail before modifying the old inventory
    if previous and previous.status in {"ACTIVE", "EXPIRED"}:
        previous.status, previous.rotated_at = "ROTATED", utcnow()
        db.flush()
    row = ConnectionCredential(
        id=uuid.uuid4(),
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        credential_type=data.credential_type,
        generation=previous.generation + 1 if previous else 1,
        key_reference=settings.credential_key_reference,
        nonce=os.urandom(12),
        encrypted_payload=b"",
        expires_at=data.expires_at,
        replaces_id=previous.id if previous else None,
        status="EXPIRED"
        if data.expires_at is not None and data.expires_at <= utcnow()
        else "ACTIVE",
    )
    row.encrypted_payload = cipher.encrypt(
        row.nonce,
        json.dumps({"issuer": "northstar-local", "value": data.value.get_secret_value()}).encode(),
        aad(row),
    )
    db.add(row)
    db.flush()
    return row


def submit(
    db: Session,
    context: TenantContext,
    settings: Settings,
    connection_id: uuid.UUID,
    data: CredentialSubmission,
    *,
    replace_id: uuid.UUID | None = None,
) -> ConnectionCredential:
    from app.services.integrations import get_connection

    manage_lock(db, context)
    connection = get_connection(db, context, connection_id, lock=True)
    if connection.connector_type == "generic_rest":
        raise HTTPException(422, "Credential use is limited to local simulators")
    previous = latest(db, connection, data.credential_type)
    if replace_id is None and previous and previous.status in {"ACTIVE", "EXPIRED"}:
        raise HTTPException(409, "Rotate the current credential explicitly")
    if replace_id is not None and (previous is None or previous.id != replace_id):
        raise HTTPException(409, "Only the latest credential can be replaced")
    row = insert(db, settings, connection, data, previous)
    connection.health_status, connection.last_checked_at = "UNKNOWN", None
    record(
        db,
        "credential.rotated" if previous else "credential.submitted",
        "credential",
        row.id,
        row.tenant_id,
        context.identity.user.id,
    )
    return row


def get(
    db: Session, context: TenantContext, connection_id: uuid.UUID, credential_id: uuid.UUID
) -> ConnectionCredential:
    row = db.scalar(
        select(ConnectionCredential).where(
            ConnectionCredential.id == credential_id,
            ConnectionCredential.connection_id == connection_id,
            ConnectionCredential.tenant_id == context.tenant.id,
        )
    )
    if row is None:
        raise HTTPException(404, "Credential unavailable")
    return row


def revoke(
    db: Session, context: TenantContext, connection_id: uuid.UUID, credential_id: uuid.UUID
) -> ConnectionCredential:
    from app.services.integrations import get_connection

    manage_lock(db, context)
    connection = get_connection(db, context, connection_id, lock=True)
    row = get(db, context, connection_id, credential_id)
    if row.status == "ROTATED":
        raise HTTPException(409, "Credential has been replaced")
    if row.status != "REVOKED":
        row.status, row.revoked_at = "REVOKED", utcnow()
        connection.health_status, connection.last_checked_at = "UNAVAILABLE", utcnow()
        record(
            db, "credential.revoked", "credential", row.id, row.tenant_id, context.identity.user.id
        )
    return row


def expiration(
    db: Session,
    connection: IntegrationConnection,
    row: ConnectionCredential,
    correlation_id: uuid.UUID,
    source_key: str,
) -> bool:
    expired = (
        row.status == "EXPIRED"
        or row.status == "ACTIVE"
        and row.expires_at is not None
        and row.expires_at <= utcnow()
    )
    if expired:
        row.status = "EXPIRED"
        emit(
            db,
            connection.tenant_id,
            "CREDENTIAL_EXPIRED",
            connection.id,
            correlation_id,
            source_key,
            {"connection_id": connection.id, "reason": "CREDENTIAL_EXPIRED"},
        )
    return expired


def refresh(
    db: Session,
    settings: Settings,
    connection: IntegrationConnection,
    correlation_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
    force_failure=False,
) -> dict:
    """Caller holds tenant + connection locks; a second refresh sees the rotated valid access."""
    key(settings)  # missing encryption fails closed, no plaintext fallback
    access = latest(db, connection, "OAUTH_ACCESS_TOKEN")
    refresh_token = latest(db, connection, "OAUTH_REFRESH_TOKEN")
    attempt_id = uuid.uuid4()
    if access and access.status == "ACTIVE" and access.expires_at and access.expires_at > utcnow():
        return {
            "outcome": "NOT_NEEDED",
            "access_credential_id": str(access.id),
            "refresh_credential_id": str(refresh_token.id) if refresh_token else None,
        }
    if access:
        expiration(db, connection, access, correlation_id, str(attempt_id))
    reason = (
        "ACCESS_REVOKED"
        if access and access.status == "REVOKED"
        else "NO_REFRESH_TOKEN"
        if refresh_token is None
        else "REFRESH_REVOKED"
        if refresh_token.status != "ACTIVE"
        else "REFRESH_EXPIRED"
        if refresh_token.expires_at is None or refresh_token.expires_at <= utcnow()
        else "SIMULATED_REFRESH_FAILED"
        if force_failure
        else None
    )
    if reason is None:
        refresh_value = decrypt(settings, refresh_token)
        digest = hmac.new(
            key(settings),
            f"{connection.tenant_id}:{connection.id}:{refresh_token.generation}:{refresh_value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        from pydantic import SecretStr

        new_refresh = insert(
            db,
            settings,
            connection,
            CredentialSubmission(
                credential_type="OAUTH_REFRESH_TOKEN",
                value=SecretStr("sim_refresh_" + digest),
                expires_at=utcnow() + timedelta(days=7),
            ),
            refresh_token,
        )
        new_access = insert(
            db,
            settings,
            connection,
            CredentialSubmission(
                credential_type="OAUTH_ACCESS_TOKEN",
                value=SecretStr("sim_access_" + digest),
                expires_at=utcnow() + timedelta(minutes=5),
            ),
            access,
        )
        db.add(
            CredentialRefreshAttempt(
                id=attempt_id,
                tenant_id=connection.tenant_id,
                connection_id=connection.id,
                outcome="SUCCEEDED",
                reason="LOCAL_ROTATION",
                access_credential_id=new_access.id,
                refresh_credential_id=new_refresh.id,
                correlation_id=correlation_id,
            )
        )
        record(
            db, "credential.refreshed", "connection", connection.id, connection.tenant_id, actor_id
        )
        resolve_connection_alerts(
            db,
            connection.tenant_id,
            connection.id,
            correlation_id,
            str(attempt_id),
            ["CREDENTIAL_EXPIRED", "REFRESH_FAILED"],
        )
        db.flush()
        return {
            "outcome": "SUCCEEDED",
            "access_credential_id": str(new_access.id),
            "refresh_credential_id": str(new_refresh.id),
        }
    if refresh_token and reason == "REFRESH_EXPIRED":
        refresh_token.status = "EXPIRED"
    connection.health_status, connection.last_checked_at = "UNAVAILABLE", utcnow()
    db.add(
        CredentialRefreshAttempt(
            id=attempt_id,
            tenant_id=connection.tenant_id,
            connection_id=connection.id,
            outcome="FAILED",
            reason=reason,
            access_credential_id=access.id if access else None,
            refresh_credential_id=refresh_token.id if refresh_token else None,
            correlation_id=correlation_id,
        )
    )
    emit(
        db,
        connection.tenant_id,
        "REFRESH_FAILED",
        connection.id,
        correlation_id,
        str(attempt_id),
        {"connection_id": connection.id, "reason": reason},
    )
    record(
        db, "credential.refresh_failed", "connection", connection.id, connection.tenant_id, actor_id
    )
    db.flush()
    raise ReliabilityError(ErrorCategory.AUTHENTICATION_FAILED, "REFRESH_FAILED", retryable=False)


def authorize(
    db: Session,
    settings: Settings,
    connection: IntegrationConnection,
    mode: str,
    correlation_id: uuid.UUID,
    operation_id: uuid.UUID,
    actor_id: uuid.UUID,
    force_refresh_failure=False,
) -> None:
    if mode == "OPTIONAL":
        return
    kind = "OAUTH_ACCESS_TOKEN" if mode == "OAUTH" else mode
    row = latest(db, connection, kind)
    if row is None or row.status in {"REVOKED", "ROTATED"}:
        raise ReliabilityError(
            ErrorCategory.AUTHENTICATION_FAILED, "CREDENTIAL_MISSING_OR_REVOKED", retryable=False
        )
    expired = expiration(db, connection, row, correlation_id, str(operation_id))
    if mode == "OAUTH" and expired:
        refresh(db, settings, connection, correlation_id, actor_id, force_refresh_failure)
        row = latest(db, connection, kind)
    elif expired:
        raise ReliabilityError(
            ErrorCategory.AUTHENTICATION_FAILED, "CREDENTIAL_EXPIRED", retryable=False
        )
    decrypt(settings, row)  # validates ciphertext/AAD/issuer without disclosing the value
