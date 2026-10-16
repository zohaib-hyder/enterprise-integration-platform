"""Bounded, versioned notification and calendar APIs."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime
from sqlalchemy import func, select

from app.api.canonical import DB, Manage, Read, mutations, page
from app.api.dependencies import TenantContext, require_tenant_permission
from app.api.webhooks import Limit, Offset
from app.models.integration import SyntheticCalendarEvent
from app.models.notification import (
    CalendarCoordinationAttempt,
    CalendarCoordinationRequest,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationIntent,
    NotificationReceipt,
    NotificationRule,
    NotificationRuleVersion,
    NotificationTemplate,
    NotificationTemplateVersion,
)
from app.schemas.notification import (
    ActivateTemplate,
    CalendarCancel,
    CalendarChange,
    CalendarCreate,
    ManualNotification,
    PreviewTemplate,
    RecoveryInput,
    RuleCreate,
    RuleUpdate,
    TemplateCreate,
    TemplateVersionInput,
)
from app.services import notifications as service
from app.services.synchronization import public, scoped

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])
calendar = APIRouter(prefix="/api/v1/calendar", tags=["Calendar coordination"])
Recover = Annotated[TenantContext, Depends(require_tenant_permission("executions:recover"))]


def paged(db, ctx, model, offset, limit, predicates=None):
    rows, total = page(db, model, ctx.tenant.id, offset, limit, predicates)
    return {"items": [public(r) for r in rows], "total": total, "offset": offset, "limit": limit}


@router.get("/templates")
def templates(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25):
    return paged(db, ctx, NotificationTemplate, offset, limit)


@router.post("/templates", dependencies=mutations, status_code=201)
def template_create(data: TemplateCreate, ctx: Manage, db: DB):
    return public(service.new_template(db, ctx, data))


@router.get("/templates/{template_id}/versions")
def template_versions(
    template_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25
):
    scoped(db, ctx, NotificationTemplate, template_id)
    return paged(
        db,
        ctx,
        NotificationTemplateVersion,
        offset,
        limit,
        [NotificationTemplateVersion.template_id == template_id],
    )


@router.post("/templates/{template_id}/versions", dependencies=mutations, status_code=201)
def template_version(template_id: uuid.UUID, data: TemplateVersionInput, ctx: Manage, db: DB):
    return public(
        service.new_template_version(
            db, ctx, scoped(db, ctx, NotificationTemplate, template_id), data
        )
    )


@router.post("/templates/{template_id}/activate", dependencies=mutations)
def activate(template_id: uuid.UUID, data: ActivateTemplate, ctx: Manage, db: DB):
    return public(service.activate_template(db, ctx, template_id, data.version_id))


@router.post("/templates/preview", dependencies=mutations)
def preview(data: PreviewTemplate, ctx: Manage, db: DB):
    return service.render(db, ctx, data.version_id, data.inputs)


@router.get("/rules")
def rules(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25):
    return paged(db, ctx, NotificationRule, offset, limit)


@router.post("/rules", dependencies=mutations, status_code=201)
def rule_create(data: RuleCreate, ctx: Manage, db: DB):
    return public(service.new_rule(db, ctx, data))


@router.get("/rules/{rule_id}/versions")
def rule_versions(rule_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25):
    scoped(db, ctx, NotificationRule, rule_id)
    return paged(
        db,
        ctx,
        NotificationRuleVersion,
        offset,
        limit,
        [NotificationRuleVersion.rule_id == rule_id],
    )


@router.patch("/rules/{rule_id}", dependencies=mutations)
def rule_update(rule_id: uuid.UUID, data: RuleUpdate, ctx: Manage, db: DB):
    return public(service.update_rule(db, ctx, scoped(db, ctx, NotificationRule, rule_id), data))


@router.post("/manual", dependencies=mutations, status_code=201)
def manual(data: ManualNotification, request: Request, ctx: Manage, db: DB):
    return public(service.manual(db, ctx, data, uuid.UUID(request.state.correlation_id)))


@router.get("/intents")
def intents(
    ctx: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    channel: Literal["SLACK", "EMAIL"] | None = None,
):
    return paged(
        db,
        ctx,
        NotificationIntent,
        offset,
        limit,
        [NotificationIntent.channel == channel] if channel else [],
    )


@router.get("/intents/{intent_id}")
def intent(intent_id: uuid.UUID, ctx: Read, db: DB):
    row = scoped(db, ctx, NotificationIntent, intent_id)
    delivery = db.scalar(
        select(NotificationDelivery).where(
            NotificationDelivery.intent_id == row.id,
            NotificationDelivery.tenant_id == ctx.tenant.id,
        )
    )
    return {"intent": public(row), "delivery": public(delivery)}


@router.get("/deliveries")
def deliveries(
    ctx: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    channel: Literal["SLACK", "EMAIL", "CALENDAR"] | None = None,
    status: Literal[
        "PENDING", "RUNNING", "RETRY_SCHEDULED", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"
    ]
    | None = None,
):
    return paged(
        db,
        ctx,
        NotificationDelivery,
        offset,
        limit,
        ([NotificationDelivery.channel == channel] if channel else [])
        + ([NotificationDelivery.status == status] if status else []),
    )


@router.get("/deliveries/{delivery_id}")
def delivery(delivery_id: uuid.UUID, ctx: Read, db: DB):
    return public(scoped(db, ctx, NotificationDelivery, delivery_id))


@router.get("/deliveries/{delivery_id}/attempts")
def attempts(delivery_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25):
    scoped(db, ctx, NotificationDelivery, delivery_id)
    return paged(
        db,
        ctx,
        NotificationDeliveryAttempt,
        offset,
        limit,
        [NotificationDeliveryAttempt.delivery_id == delivery_id],
    )


@router.get("/deliveries/{delivery_id}/receipts")
def receipts(delivery_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25):
    scoped(db, ctx, NotificationDelivery, delivery_id)
    return paged(
        db,
        ctx,
        NotificationReceipt,
        offset,
        limit,
        [NotificationReceipt.delivery_id == delivery_id],
    )


@router.post("/deliveries/{delivery_id}/{action}", dependencies=mutations)
def recovery(
    delivery_id: uuid.UUID,
    action: Literal["retry", "cancel", "review"],
    data: RecoveryInput,
    ctx: Recover,
    db: DB,
):
    return service.recover(db, ctx, scoped(db, ctx, NotificationDelivery, delivery_id), action)


@router.get("/metrics")
def metrics(ctx: Read, db: DB):
    counts = db.execute(
        select(NotificationDelivery.channel, NotificationDelivery.status, func.count())
        .where(NotificationDelivery.tenant_id == ctx.tenant.id)
        .group_by(NotificationDelivery.channel, NotificationDelivery.status)
    ).all()
    return {
        "counts": [{"channel": c, "status": s, "count": n} for c, s, n in counts],
        "basis": "Persisted tenant deliveries; no external transport",
    }


@calendar.get("/requests")
def calendar_requests(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25):
    return paged(db, ctx, CalendarCoordinationRequest, offset, limit)


@calendar.get("/requests/{request_id}")
def calendar_request(request_id: uuid.UUID, ctx: Read, db: DB):
    row = scoped(db, ctx, CalendarCoordinationRequest, request_id)
    return {
        "request": public(row),
        "delivery": public(scoped(db, ctx, NotificationDelivery, row.delivery_id)),
    }


@calendar.get("/requests/{request_id}/history")
def calendar_history(
    request_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25
):
    scoped(db, ctx, CalendarCoordinationRequest, request_id)
    return paged(
        db,
        ctx,
        CalendarCoordinationAttempt,
        offset,
        limit,
        [CalendarCoordinationAttempt.request_id == request_id],
    )


@calendar.get("/events")
def events(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25):
    return paged(db, ctx, SyntheticCalendarEvent, offset, limit)


@calendar.get("/events/{event_id}")
def event(event_id: uuid.UUID, ctx: Read, db: DB):
    return public(service.calendar_event(db, ctx, event_id))


@calendar.post("/events", dependencies=mutations, status_code=201)
def event_create(data: CalendarCreate, request: Request, ctx: Manage, db: DB):
    return public(service.calendar_queue(db, ctx, data, uuid.UUID(request.state.correlation_id)))


@calendar.post("/events/{event_id}/reschedule", dependencies=mutations, status_code=201)
def reschedule(event_id: uuid.UUID, data: CalendarChange, request: Request, ctx: Manage, db: DB):
    return public(
        service.calendar_queue(db, ctx, data, uuid.UUID(request.state.correlation_id), event_id)
    )


@calendar.post("/events/{event_id}/cancel", dependencies=mutations, status_code=201)
def cancel(event_id: uuid.UUID, data: CalendarCancel, request: Request, ctx: Manage, db: DB):
    return public(
        service.calendar_queue(db, ctx, data, uuid.UUID(request.state.correlation_id), event_id)
    )


@calendar.get("/availability")
def availability(
    ctx: Read,
    db: DB,
    connection_id: uuid.UUID,
    calendar: Literal["interviews", "recruiting"],
    starts_at: AwareDatetime,
    ends_at: AwareDatetime,
):
    return service.availability(db, ctx, connection_id, calendar, starts_at, ends_at)
