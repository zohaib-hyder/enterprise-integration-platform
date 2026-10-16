import time
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, get_db, require_tenant_permission
from app.connectors.webhook_sender import event_bytes, signed_request
from app.core.security import require_csrf
from app.core.webhook_security import (
    MAX_BODY,
    IngestionCategory,
    IngestionError,
    encryption,
    unseal,
)
from app.models.integration import IntegrationConnection
from app.models.webhook import EventReceipt, IncomingEvent, WebhookEndpoint
from app.schemas.webhook import (
    EndpointCreate,
    EndpointDisclosure,
    EndpointOutput,
    EndpointPage,
    EndpointUpdate,
    EventOutput,
    EventPage,
    EventType,
    ReceiptPage,
    RotateSecret,
    SimulationInput,
    Source,
)
from app.services import webhooks as service
from app.services.audit import record

router = APIRouter(prefix="/api/v1")
DB = Annotated[Session, Depends(get_db, scope="function")]
Read = Annotated[TenantContext, Depends(require_tenant_permission("operations:read"))]
Manage = Annotated[TenantContext, Depends(require_tenant_permission("integrations:manage"))]
Offset = Annotated[int, Query(ge=0, le=100000)]
Limit = Annotated[int, Query(ge=1, le=100)]
mutations = [Depends(require_csrf)]


@router.post(
    "/webhook-endpoints", response_model=EndpointDisclosure, status_code=201, dependencies=mutations
)
def create_endpoint(
    data: EndpointCreate, context: Manage, db: DB, request: Request
) -> EndpointDisclosure:
    return service.create_endpoint(db, context, request.app.state.settings, data)


@router.get("/webhook-endpoints", response_model=EndpointPage)
def endpoints(context: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> EndpointPage:
    scope = WebhookEndpoint.tenant_id == context.tenant.id
    total = db.scalar(select(func.count()).select_from(WebhookEndpoint).where(scope)) or 0
    rows = db.scalars(
        select(WebhookEndpoint)
        .where(scope)
        .order_by(WebhookEndpoint.created_at, WebhookEndpoint.id)
        .offset(offset)
        .limit(limit)
    )
    return EndpointPage(
        items=[service.endpoint_output(db, row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/webhook-endpoints/{endpoint_id}", response_model=EndpointOutput)
def endpoint(endpoint_id: uuid.UUID, context: Read, db: DB) -> EndpointOutput:
    return service.endpoint_output(db, service.get_endpoint(db, context, endpoint_id))


@router.patch(
    "/webhook-endpoints/{endpoint_id}", response_model=EndpointOutput, dependencies=mutations
)
def update(endpoint_id: uuid.UUID, data: EndpointUpdate, context: Manage, db: DB) -> EndpointOutput:
    return service.update_endpoint(db, context, endpoint_id, data.enabled)


@router.post(
    "/webhook-endpoints/{endpoint_id}/rotate-secret",
    response_model=EndpointDisclosure,
    dependencies=mutations,
)
def rotate(
    endpoint_id: uuid.UUID, data: RotateSecret, context: Manage, db: DB, request: Request
) -> EndpointDisclosure:
    return service.rotate_secret(
        db, context, request.app.state.settings, endpoint_id, data.grace_seconds
    )


@router.get("/webhook-endpoints/{endpoint_id}/status")
def endpoint_status(endpoint_id: uuid.UUID, context: Read, db: DB, request: Request) -> dict:
    row = service.get_endpoint(db, context, endpoint_id)
    connection = db.get(IntegrationConnection, row.connection_id)
    counts = dict(
        db.execute(
            select(EventReceipt.outcome, func.count())
            .where(EventReceipt.tenant_id == context.tenant.id, EventReceipt.endpoint_id == row.id)
            .group_by(EventReceipt.outcome)
        ).all()
    )
    errors = dict(
        db.execute(
            select(EventReceipt.error_category, func.count())
            .where(
                EventReceipt.tenant_id == context.tenant.id,
                EventReceipt.endpoint_id == row.id,
                EventReceipt.error_category.is_not(None),
            )
            .group_by(EventReceipt.error_category)
        ).all()
    )
    last = db.scalar(
        select(func.max(EventReceipt.received_at)).where(
            EventReceipt.tenant_id == context.tenant.id, EventReceipt.endpoint_id == row.id
        )
    )
    key_available = request.app.state.settings.webhook_encryption_key is not None
    if key_available:
        try:
            keys = service.versions(db, row)
            key_available = bool(keys) and bool(unseal(request.app.state.settings, keys[0]))
        except IngestionError:
            key_available = False
    return {
        "endpoint_id": row.id,
        "status": row.status,
        "connection_status": connection.status,
        "secret_available": key_available,
        "receipt_counts": counts,
        "error_counts": errors,
        "last_received_at": last,
        "synthetic": True,
        "processing_status": "PENDING",
    }


def receipts(
    db: Session, context: TenantContext, predicates: list, offset: int, limit: int
) -> ReceiptPage:
    scope = [EventReceipt.tenant_id == context.tenant.id, *predicates]
    total = db.scalar(select(func.count()).select_from(EventReceipt).where(*scope)) or 0
    rows = db.scalars(
        select(EventReceipt)
        .where(*scope)
        .order_by(EventReceipt.received_at.desc(), EventReceipt.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return ReceiptPage(
        items=[service.receipt_output(row) for row in rows], total=total, offset=offset, limit=limit
    )


@router.get("/webhook-endpoints/{endpoint_id}/receipts", response_model=ReceiptPage)
def endpoint_receipts(
    endpoint_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> ReceiptPage:
    service.get_endpoint(db, context, endpoint_id)
    return receipts(db, context, [EventReceipt.endpoint_id == endpoint_id], offset, limit)


@router.get("/events", response_model=EventPage)
def events(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    source: Source | None = None,
    event_type: EventType | None = None,
    status: Literal["ACCEPTED", "PENDING"] | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> EventPage:
    if since and until and since > until:
        raise HTTPException(422, "Invalid time range")
    scope = [IncomingEvent.tenant_id == context.tenant.id]
    if source:
        scope.append(IncomingEvent.source == source)
    if event_type:
        scope.append(IncomingEvent.event_type == event_type)
    if status:
        scope.append(
            (
                IncomingEvent.ingestion_status
                if status == "ACCEPTED"
                else IncomingEvent.processing_status
            )
            == status
        )
    if since:
        scope.append(IncomingEvent.received_at >= since)
    if until:
        scope.append(IncomingEvent.received_at <= until)
    total = db.scalar(select(func.count()).select_from(IncomingEvent).where(*scope)) or 0
    rows = db.scalars(
        select(IncomingEvent)
        .where(*scope)
        .order_by(IncomingEvent.received_at.desc(), IncomingEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return EventPage(
        items=[service.event_output(db, row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


def get_event(db: Session, context: TenantContext, event_id: uuid.UUID) -> IncomingEvent:
    row = db.scalar(
        select(IncomingEvent).where(
            IncomingEvent.id == event_id, IncomingEvent.tenant_id == context.tenant.id
        )
    )
    if row is None:
        raise HTTPException(404, "Event unavailable")
    return row


@router.get("/events/{event_id}", response_model=EventOutput)
def event_details(event_id: uuid.UUID, context: Read, db: DB) -> EventOutput:
    return service.event_output(db, get_event(db, context, event_id), detail=True)


@router.get("/events/{event_id}/receipts", response_model=ReceiptPage)
def event_receipts(
    event_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> ReceiptPage:
    get_event(db, context, event_id)
    return receipts(db, context, [EventReceipt.event_id == event_id], offset, limit)


@router.post("/webhooks/ingest/{public_id}")
async def receive(public_id: str, request: Request, db: DB) -> JSONResponse:
    started = time.perf_counter()
    encryption(request.app.state.settings)
    content_type = request.headers.get("content-type", "").lower().replace(" ", "")
    if content_type not in {"application/json", "application/json;charset=utf-8"}:
        raise IngestionError(IngestionCategory.UNSUPPORTED_CONTENT_TYPE, 415)
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or len(length) > 10 or int(length) > MAX_BODY):
        raise IngestionError(IngestionCategory.PAYLOAD_TOO_LARGE, 413)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            raise IngestionError(IngestionCategory.PAYLOAD_TOO_LARGE, 413)
        raw.extend(chunk)
    # No session cookies, CSRF or payload/header tenant IDs are used at ingress.
    from starlette.concurrency import run_in_threadpool

    status, response = await run_in_threadpool(
        service.ingest,
        db,
        request.app.state.settings,
        public_id,
        request.headers.get("x-webhook-timestamp", ""),
        request.headers.get("x-webhook-signature", ""),
        bytes(raw),
        uuid.UUID(request.state.correlation_id),
        started,
    )
    request.state.webhook_outcome = response.get("status", response.get("category"))
    return JSONResponse(response, status_code=status)


@router.post("/webhook-endpoints/{endpoint_id}/simulate", dependencies=mutations)
def simulate(
    endpoint_id: uuid.UUID, data: SimulationInput, context: Manage, db: DB, request: Request
) -> dict:
    service.manage_lock(db, context)
    row = service.get_endpoint(db, context, endpoint_id, lock=True)
    settings = request.app.state.settings
    keys = service.versions(db, row)
    if not keys:
        raise IngestionError(IngestionCategory.SECRET_UNAVAILABLE, 503)
    try:
        body = event_bytes(row.source, data.event_type, data.event_id)
    except ValueError:
        raise IngestionError(IngestionCategory.INVALID_SCHEMA, 422) from None
    body, headers = signed_request(unseal(settings, keys[0]), body, data.scenario)
    results = []
    for _ in range(2 if data.scenario == "duplicate" else 1):
        try:
            status, result = service.ingest(
                db,
                settings,
                row.public_id,
                headers["X-Webhook-Timestamp"],
                headers["X-Webhook-Signature"],
                body,
                uuid.UUID(request.state.correlation_id),
                time.perf_counter(),
            )
        except IngestionError as error:
            status, result = error.status, {"category": error.category.value}
        db.flush()
        results.append({"http_status": status, **result})
    record(db, "webhook.simulated", "webhook", row.id, row.tenant_id, context.identity.user.id)
    return {"synthetic": True, "delivery_mode": "local_in_process", "results": results}
