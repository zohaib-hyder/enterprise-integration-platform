"""Authenticated endpoint management and signature-verified inbox transactions."""

import hashlib
import json
import re
import secrets
import time
import uuid
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.core.config import Settings
from app.core.security import utcnow
from app.core.webhook_security import (
    IngestionCategory,
    IngestionError,
    encryption,
    seal,
    signature_matches,
    unseal,
)
from app.models.integration import IntegrationConnection
from app.models.tenant import Tenant
from app.models.webhook import EventReceipt, IncomingEvent, WebhookEndpoint, WebhookSecretVersion
from app.schemas.webhook import (
    BusinessEvent,
    EndpointCreate,
    EndpointDisclosure,
    EndpointOutput,
    EventOutput,
    ReceiptOutput,
)
from app.services.audit import record
from app.services.integrations import authorize_management, get_connection


def get_endpoint(
    db: Session, context: TenantContext, endpoint_id: uuid.UUID, lock: bool = False
) -> WebhookEndpoint:
    query = select(WebhookEndpoint).where(
        WebhookEndpoint.id == endpoint_id, WebhookEndpoint.tenant_id == context.tenant.id
    )
    endpoint = db.scalar(query.with_for_update() if lock else query)
    if endpoint is None:
        raise IngestionError(IngestionCategory.AUTHENTICATION_FAILED, 404)
    return endpoint


def versions(db: Session, endpoint: WebhookEndpoint) -> list[WebhookSecretVersion]:
    return list(
        db.scalars(
            select(WebhookSecretVersion)
            .where(
                WebhookSecretVersion.endpoint_id == endpoint.id,
                WebhookSecretVersion.tenant_id == endpoint.tenant_id,
            )
            .order_by(WebhookSecretVersion.version.desc())
            .limit(2)
        )
    )


def endpoint_output(db: Session, row: WebhookEndpoint) -> EndpointOutput:
    keys = versions(db, row)
    return EndpointOutput(
        id=row.id,
        connection_id=row.connection_id,
        source=row.source,
        status=row.status,
        public_id=row.public_id,
        signature_version=row.signature_version,
        timestamp_tolerance=row.timestamp_tolerance,
        current_secret_version=keys[0].version if keys else 0,
        previous_secret_valid_until=keys[1].valid_until if len(keys) > 1 else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def manage_lock(db: Session, context: TenantContext) -> None:
    db.scalar(select(Tenant).where(Tenant.id == context.tenant.id).with_for_update())
    authorize_management(db, context)


def create_endpoint(
    db: Session, context: TenantContext, settings: Settings, data: EndpointCreate
) -> EndpointDisclosure:
    encryption(settings)
    manage_lock(db, context)
    connection = get_connection(db, context, data.connection_id)
    if connection.connector_type != data.source or connection.status != "ENABLED":
        raise IngestionError(IngestionCategory.INVALID_SOURCE, 422)
    endpoint = WebhookEndpoint(
        tenant_id=context.tenant.id,
        connection_id=connection.id,
        source=data.source,
        public_id=secrets.token_urlsafe(32),
        timestamp_tolerance=data.timestamp_tolerance,
    )
    db.add(endpoint)
    db.flush()
    secret, nonce, ciphertext = seal(settings, endpoint.tenant_id, endpoint.id, 1)
    db.add(
        WebhookSecretVersion(
            tenant_id=endpoint.tenant_id,
            endpoint_id=endpoint.id,
            connection_id=endpoint.connection_id,
            version=1,
            nonce=nonce,
            ciphertext=ciphertext,
        )
    )
    record(
        db, "webhook.created", "webhook", endpoint.id, endpoint.tenant_id, context.identity.user.id
    )
    db.flush()
    return EndpointDisclosure(
        endpoint=endpoint_output(db, endpoint), signing_secret=secret, secret_version=1
    )


def update_endpoint(
    db: Session, context: TenantContext, endpoint_id: uuid.UUID, enabled: bool
) -> EndpointOutput:
    manage_lock(db, context)
    row = get_endpoint(db, context, endpoint_id, lock=True)
    if enabled and get_connection(db, context, row.connection_id).status != "ENABLED":
        raise IngestionError(IngestionCategory.INVALID_SOURCE, 422)
    row.status = "ACTIVE" if enabled else "DISABLED"
    record(db, "webhook.status_changed", "webhook", row.id, row.tenant_id, context.identity.user.id)
    db.flush()
    return endpoint_output(db, row)


def rotate_secret(
    db: Session, context: TenantContext, settings: Settings, endpoint_id: uuid.UUID, grace: int
) -> EndpointDisclosure:
    encryption(settings)
    manage_lock(db, context)
    row = get_endpoint(db, context, endpoint_id, lock=True)
    keys = versions(db, row)
    if not keys:
        raise IngestionError(IngestionCategory.SECRET_UNAVAILABLE, 503)
    # Verify the master key before modifying credentials; wrong keys cannot silently reset them.
    unseal(settings, keys[0])
    now = utcnow()
    keys[0].valid_until = now + timedelta(seconds=grace)
    for previous in keys[1:]:
        previous.valid_until = now
    version = keys[0].version + 1
    secret, nonce, ciphertext = seal(settings, row.tenant_id, row.id, version)
    db.add(
        WebhookSecretVersion(
            tenant_id=row.tenant_id,
            endpoint_id=row.id,
            connection_id=row.connection_id,
            version=version,
            nonce=nonce,
            ciphertext=ciphertext,
        )
    )
    record(db, "webhook.secret_rotated", "webhook", row.id, row.tenant_id, context.identity.user.id)
    db.flush()
    return EndpointDisclosure(
        endpoint=endpoint_output(db, row), signing_secret=secret, secret_version=version
    )


def authenticate(
    db: Session, settings: Settings, public_id: str, timestamp: str, supplied: str, body: bytes
) -> WebhookEndpoint:
    encryption(settings)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", public_id):
        raise IngestionError(IngestionCategory.AUTHENTICATION_FAILED, 401)
    endpoint = db.scalar(
        select(WebhookEndpoint).where(WebhookEndpoint.public_id == public_id).with_for_update()
    )
    if endpoint is None or endpoint.status != "ACTIVE":
        raise IngestionError(IngestionCategory.AUTHENTICATION_FAILED, 401)
    connection = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.id == endpoint.connection_id,
            IntegrationConnection.tenant_id == endpoint.tenant_id,
            IntegrationConnection.connector_type == endpoint.source,
            IntegrationConnection.status == "ENABLED",
        )
    )
    if connection is None:
        raise IngestionError(IngestionCategory.AUTHENTICATION_FAILED, 401)
    if not re.fullmatch(r"[0-9]{1,11}", timestamp):
        raise IngestionError(IngestionCategory.INVALID_TIMESTAMP, 401)
    if abs(utcnow().timestamp() - int(timestamp)) > endpoint.timestamp_tolerance:
        raise IngestionError(IngestionCategory.STALE_TIMESTAMP, 401)
    matched = False
    now = utcnow()
    for key in versions(db, endpoint):
        if key.valid_until is None or key.valid_until > now:
            # Evaluate all eligible versions, without selecting one from untrusted input.
            matched = signature_matches(unseal(settings, key), timestamp, body, supplied) or matched
    if not matched:
        raise IngestionError(IngestionCategory.AUTHENTICATION_FAILED, 401)
    return endpoint


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def parse_event(body: bytes, source: str) -> BusinessEvent:
    try:
        parsed = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise IngestionError(IngestionCategory.MALFORMED_JSON, 422) from None
    if isinstance(parsed, dict) and parsed.get("source") != source:
        raise IngestionError(IngestionCategory.INVALID_SOURCE, 422)
    try:
        return BusinessEvent.model_validate(parsed)
    except ValidationError:
        raise IngestionError(IngestionCategory.INVALID_SCHEMA, 422) from None


def receipt(
    db: Session,
    endpoint: WebhookEndpoint,
    event_id: uuid.UUID | None,
    outcome: str,
    category: str | None,
    digest: str,
    correlation: uuid.UUID,
    started: float,
) -> None:
    db.add(
        EventReceipt(
            tenant_id=endpoint.tenant_id,
            endpoint_id=endpoint.id,
            connection_id=endpoint.connection_id,
            event_id=event_id,
            outcome=outcome,
            error_category=category,
            payload_digest=digest,
            correlation_id=correlation,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )
    )


def ingest(
    db: Session,
    settings: Settings,
    public_id: str,
    timestamp: str,
    supplied: str,
    body: bytes,
    correlation: uuid.UUID,
    started: float,
) -> tuple[int, dict]:
    endpoint = authenticate(db, settings, public_id, timestamp, supplied, body)
    # Only signature-authenticated rejections receive tenant-attributed sanitized receipts.
    try:
        data = parse_event(body, endpoint.source)
    except IngestionError as error:
        receipt(
            db,
            endpoint,
            None,
            "REJECTED",
            error.category.value,
            hashlib.sha256(body).hexdigest(),
            correlation,
            started,
        )
        return error.status, {
            "detail": "Webhook request rejected",
            "category": error.category.value,
            "correlation_id": str(correlation),
        }
    normalized = data.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    existing = db.scalar(
        select(IncomingEvent).where(
            IncomingEvent.tenant_id == endpoint.tenant_id,
            IncomingEvent.endpoint_id == endpoint.id,
            IncomingEvent.source == endpoint.source,
            IncomingEvent.provider_event_id == data.event_id,
        )
    )
    if existing is not None:
        identical = existing.payload_digest == digest
        outcome = "DUPLICATE" if identical else "CONFLICT"
        category = None if identical else IngestionCategory.CONFLICTING_EVENT.value
        receipt(db, endpoint, existing.id, outcome, category, digest, correlation, started)
        return (200 if identical else 409), (
            {
                "status": "duplicate",
                "event_id": str(existing.id),
                "processing_status": "PENDING",
                "correlation_id": str(correlation),
            }
            if identical
            else {
                "detail": "Webhook request rejected",
                "category": category,
                "correlation_id": str(correlation),
            }
        )
    event = IncomingEvent(
        tenant_id=endpoint.tenant_id,
        endpoint_id=endpoint.id,
        connection_id=endpoint.connection_id,
        source=endpoint.source,
        provider_event_id=data.event_id,
        event_type=data.event_type,
        payload=normalized,
        payload_digest=digest,
        occurred_at=data.occurred_at,
        correlation_id=correlation,
    )
    db.add(event)
    db.flush()
    receipt(db, endpoint, event.id, "ACCEPTED", None, digest, correlation, started)
    return 202, {
        "status": "accepted",
        "event_id": str(event.id),
        "processing_status": "PENDING",
        "correlation_id": str(correlation),
    }


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(
                word in key.lower()
                for word in ("secret", "token", "password", "authorization", "email", "name")
            )
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def event_output(db: Session, row: IncomingEvent, detail: bool = False) -> EventOutput:
    duplicate_count = (
        db.scalar(
            select(func.count())
            .select_from(EventReceipt)
            .where(
                EventReceipt.tenant_id == row.tenant_id,
                EventReceipt.event_id == row.id,
                EventReceipt.outcome == "DUPLICATE",
            )
        )
        or 0
    )
    return EventOutput(
        id=row.id,
        endpoint_id=row.endpoint_id,
        connection_id=row.connection_id,
        source=row.source,
        provider_event_id=row.provider_event_id,
        event_type=row.event_type,
        schema_version=row.schema_version,
        payload_digest=row.payload_digest,
        received_at=row.received_at,
        occurred_at=row.occurred_at,
        ingestion_status=row.ingestion_status,
        processing_status=row.processing_status,
        correlation_id=row.correlation_id,
        duplicate_count=duplicate_count,
        payload=redact(row.payload) if detail else None,
    )


def receipt_output(row: EventReceipt) -> ReceiptOutput:
    return ReceiptOutput(
        id=row.id,
        endpoint_id=row.endpoint_id,
        event_id=row.event_id,
        outcome=row.outcome,
        error_category=row.error_category,
        correlation_id=row.correlation_id,
        duration_ms=row.duration_ms,
        received_at=row.received_at,
    )
