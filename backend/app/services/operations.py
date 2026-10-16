"""Tenant-scoped evidence, incident correlation and immutable operator activity."""

import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.core.security import utcnow
from app.models.canonical import TransformationAttempt
from app.models.identity import Membership, User
from app.models.integration import IntegrationConnection
from app.models.notification import (
    CalendarCoordinationAttempt,
    CalendarCoordinationRequest,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationIntent,
    NotificationReceipt,
)
from app.models.operations import (
    Incident,
    IncidentEvidence,
    OperationalFinding,
    OperationsActivity,
    RecoveryRequest,
)
from app.models.reliability import AlertEvent, OperationalAlert
from app.models.synchronization import SyncConflict, SyncExecution, SyncOperation, SyncPlan
from app.models.webhook import EventReceipt, IncomingEvent
from app.models.workflow import (
    DeadLetterRecord,
    WorkerLease,
    WorkflowDispatchRecord,
    WorkflowExecution,
    WorkflowExecutionEvent,
    WorkflowStepExecution,
)
from app.schemas.operations import IncidentChange
from app.services.audit import record
from app.services.notifications import digest, lock
from app.services.synchronization import scoped

TARGETS = {
    "event": IncomingEvent,
    "receipt": EventReceipt,
    "workflow": WorkflowExecution,
    "connection": IntegrationConnection,
    "sync": SyncExecution,
    "conflict": SyncConflict,
    "delivery": NotificationDelivery,
    "calendar": CalendarCoordinationRequest,
    "alert": OperationalAlert,
    "incident": Incident,
    "finding": OperationalFinding,
}
LINKS = {
    "transformation": "/mappings",
    "dispatch": "/events",
    "step": "/executions",
    "workflow_history": "/executions",
    "plan": "/synchronization",
    "ats_action": "/synchronization",
    "intent": "/notifications",
    "delivery_attempt": "/notifications",
    "delivery_receipt": "/notifications",
    "calendar_attempt": "/calendar",
    "alert_history": "/alerts",
    "activity": "/operations",
    "recovery": "/operations",
    "event": "/events",
    "receipt": "/events",
    "workflow": "/executions",
    "connection": "/connections",
    "sync": "/synchronization",
    "conflict": "/synchronization",
    "delivery": "/notifications",
    "calendar": "/calendar",
    "alert": "/alerts",
    "incident": "/operations",
    "finding": "/operations",
}
ALERT_CATEGORIES = {
    "CREDENTIAL_EXPIRED": "CREDENTIAL_FAILURE",
    "REFRESH_FAILED": "CREDENTIAL_FAILURE",
    "WORKFLOW_DEAD_LETTERED": "WORKFLOW_DEAD_LETTERED",
    "WORKER_RECOVERY": "WORKER_RECOVERY_REQUIRED",
    "TERMINAL_NOTIFICATION_FAILURE": "NOTIFICATION_DELIVERY_FAILURE",
    "REPEATED_DELIVERY_FAILURE": "NOTIFICATION_DELIVERY_FAILURE",
    "UNKNOWN_DELIVERY_OUTCOME": "UNKNOWN_DELIVERY_OUTCOME",
    "CALENDAR_CONFLICT": "CALENDAR_CONFLICT",
}
RUNBOOKS = {
    "WEBHOOK_INGESTION_FAILURE": "webhook-rejected",
    "WORKFLOW_STALLED": "workflow-stuck",
    "WORKFLOW_DEAD_LETTERED": "workflow-stuck",
    "CONNECTOR_UNHEALTHY": "connector-unavailable",
    "CREDENTIAL_FAILURE": "credential-expired",
    "SYNC_CONFLICT": "sync-conflict",
    "SYNC_INCONSISTENCY": "sync-conflict",
    "NOTIFICATION_DELIVERY_FAILURE": "notification-failed",
    "UNKNOWN_DELIVERY_OUTCOME": "unknown-outcome",
    "CALENDAR_CONFLICT": "calendar-conflict",
    "WORKER_RECOVERY_REQUIRED": "worker-restart",
}


def target(db: Session, ctx: TenantContext, kind: str, identifier: uuid.UUID):
    model = TARGETS.get(kind)
    if not model:
        raise HTTPException(422, "Unsupported operational target")
    return scoped(db, ctx, model, identifier)


def activity(
    db: Session,
    tenant_id: uuid.UUID,
    action: str,
    kind: str,
    identifier: uuid.UUID,
    correlation_id=None,
    actor_id=None,
    reason="Recorded operational evidence",
    outcome="RECORDED",
    incident_id=None,
) -> OperationsActivity:
    row = OperationsActivity(
        tenant_id=tenant_id,
        incident_id=incident_id,
        actor_id=actor_id,
        target_kind=kind,
        target_id=identifier,
        action=action,
        correlation_id=correlation_id,
        reason=reason,
        outcome=outcome,
    )
    db.add(row)
    record(db, "operations." + action.lower(), kind, identifier, tenant_id, actor_id)
    return row


def observe(
    db: Session,
    tenant_id: uuid.UUID,
    category: str,
    kind: str,
    identifier: uuid.UUID,
    source_kind: str,
    source_id: uuid.UUID,
    occurrence: str,
    classification: str,
    correlation_id=None,
    severity="WARNING",
    observed_at=None,
) -> Incident:
    from app.models.tenant import Tenant

    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    family = (
        "DELIVERY_FAILURE"
        if kind == "delivery"
        and category
        in {"NOTIFICATION_DELIVERY_FAILURE", "UNKNOWN_DELIVERY_OUTCOME", "CALENDAR_CONFLICT"}
        else "WORKER_LEASE"
        if category in {"WORKFLOW_STALLED", "WORKER_RECOVERY_REQUIRED"}
        else category
    )
    key = digest([family, kind, str(identifier)])
    incident = db.scalar(
        select(Incident)
        .where(Incident.tenant_id == tenant_id, Incident.problem_key == key)
        .with_for_update()
    )
    if incident is None:
        now = observed_at or utcnow()
        incident = Incident(
            tenant_id=tenant_id,
            category=category,
            severity=severity,
            problem_key=key,
            target_kind=kind,
            target_id=identifier,
            correlation_id=correlation_id,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(incident)
        db.flush()
        activity(
            db,
            tenant_id,
            "INCIDENT_CREATED",
            "incident",
            incident.id,
            correlation_id,
            incident_id=incident.id,
        )
    occurrence_key = digest([source_kind, str(source_id), occurrence])
    if db.scalar(
        select(IncidentEvidence.id).where(
            IncidentEvidence.tenant_id == tenant_id,
            IncidentEvidence.occurrence_key == occurrence_key,
        )
    ):
        return incident
    db.add(
        IncidentEvidence(
            tenant_id=tenant_id,
            incident_id=incident.id,
            source_kind=source_kind,
            source_id=source_id,
            occurrence_key=occurrence_key,
            classification=classification,
            observed_at=observed_at or utcnow(),
            correlation_id=correlation_id,
        )
    )
    previous_category = incident.category
    previous_severity = incident.severity
    priorities = {
        "NOTIFICATION_DELIVERY_FAILURE": 0,
        "CALENDAR_CONFLICT": 1,
        "UNKNOWN_DELIVERY_OUTCOME": 2,
        "WORKFLOW_STALLED": 0,
        "WORKER_RECOVERY_REQUIRED": 1,
    }
    if priorities.get(category, 0) > priorities.get(incident.category, 0):
        incident.category = category
    if {"INFO": 0, "WARNING": 1, "CRITICAL": 2}[severity] > {
        "INFO": 0,
        "WARNING": 1,
        "CRITICAL": 2,
    }[incident.severity]:
        incident.severity = severity
    if incident.category != previous_category:
        activity(
            db,
            tenant_id,
            "INCIDENT_CATEGORY_CHANGED",
            "incident",
            incident.id,
            correlation_id,
            reason="Recorded specific failure evidence",
            outcome=incident.category,
            incident_id=incident.id,
        )
    if incident.severity != previous_severity:
        activity(
            db,
            tenant_id,
            "INCIDENT_SEVERITY_CHANGED",
            "incident",
            incident.id,
            correlation_id,
            reason="New persisted failure severity",
            outcome=previous_severity + " -> " + incident.severity,
            incident_id=incident.id,
        )
    activity(
        db,
        tenant_id,
        "INCIDENT_EVIDENCE_ADDED",
        "incident",
        incident.id,
        correlation_id,
        reason="Persisted classification: " + classification,
        outcome=incident.status,
        incident_id=incident.id,
    )
    incident.occurrence_count += 1
    incident.revision += 1
    incident.last_seen_at = max(incident.last_seen_at, observed_at or utcnow())
    if incident.status in {"RESOLVED", "MITIGATED"}:
        incident.status = "REOPENED"
        incident.resolution_notes = ""
        activity(
            db,
            tenant_id,
            "INCIDENT_REOPENED",
            "incident",
            incident.id,
            correlation_id,
            incident_id=incident.id,
        )
    db.flush()
    return incident


def observe_alert(db: Session, row: OperationalAlert) -> Incident:
    category = ALERT_CATEGORIES.get(row.category, "CONNECTOR_UNHEALTHY")
    kind, identifier = "alert", row.id
    # Explicit resource/context references only, checked against this tenant.
    if row.category in {
        "TERMINAL_NOTIFICATION_FAILURE",
        "REPEATED_DELIVERY_FAILURE",
        "UNKNOWN_DELIVERY_OUTCOME",
        "CALENDAR_CONFLICT",
    }:
        candidate = db.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.tenant_id == row.tenant_id,
                NotificationDelivery.id == uuid.UUID(row.resource_key),
            )
        )
        if candidate:
            kind, identifier = "delivery", candidate.id
            if candidate.channel == "CALENDAR" and candidate.error_category == "CONFLICT":
                category = "CALENDAR_CONFLICT"
            elif candidate.status == "UNKNOWN":
                category = "UNKNOWN_DELIVERY_OUTCOME"
    elif row.context.get("execution_id"):
        candidate = db.scalar(
            select(WorkflowExecution).where(
                WorkflowExecution.tenant_id == row.tenant_id,
                WorkflowExecution.id == uuid.UUID(row.context["execution_id"]),
            )
        )
        if candidate:
            kind, identifier = "workflow", candidate.id
    elif row.context.get("connection_id"):
        candidate = db.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.tenant_id == row.tenant_id,
                IntegrationConnection.id == uuid.UUID(row.context["connection_id"]),
            )
        )
        if candidate:
            kind, identifier = "connection", candidate.id
    return observe(
        db,
        row.tenant_id,
        category,
        kind,
        identifier,
        "alert",
        row.id,
        str(row.occurrence_count),
        row.category,
        row.correlation_id,
        row.severity,
        row.last_seen_at,
    )


def collect_incidents(db: Session, ctx: TenantContext, limit: int = 100, hours: int = 24) -> dict:
    lock(db, ctx, "executions:recover")
    since = utcnow() - timedelta(hours=hours)
    counts = {}
    for row in db.scalars(
        select(OperationalAlert)
        .where(OperationalAlert.tenant_id == ctx.tenant.id, OperationalAlert.last_seen_at >= since)
        .order_by(OperationalAlert.last_seen_at.desc(), OperationalAlert.id)
        .limit(limit)
    ):
        observe_alert(db, row)
        counts["alerts"] = counts.get("alerts", 0) + 1
    for model, kind, predicate, category in [
        (
            EventReceipt,
            "receipt",
            EventReceipt.outcome.in_(["REJECTED", "CONFLICT"]),
            "WEBHOOK_INGESTION_FAILURE",
        ),
        (SyncConflict, "conflict", SyncConflict.status == "OPEN", "SYNC_CONFLICT"),
        (
            WorkflowExecution,
            "workflow",
            WorkflowExecution.state == "DEAD_LETTERED",
            "WORKFLOW_DEAD_LETTERED",
        ),
        (
            WorkflowExecution,
            "workflow",
            (WorkflowExecution.state == "RUNNING")
            & (WorkflowExecution.lease_expires_at < utcnow()),
            "WORKFLOW_STALLED",
        ),
    ]:
        timecol = model.received_at if model is EventReceipt else model.created_at
        for row in db.scalars(
            select(model)
            .where(model.tenant_id == ctx.tenant.id, timecol >= since, predicate)
            .order_by(timecol.desc(), model.id)
            .limit(limit)
        ):
            identifier = row.endpoint_id if model is EventReceipt else row.id
            target_kind = "receipt" if model is EventReceipt else kind
            if model is EventReceipt:
                identifier = row.id
            classification = (
                getattr(row, "error_category", None) or getattr(row, "category", None) or category
            )
            correlation = getattr(row, "correlation_id", None)
            if model is SyncConflict:
                linked_plan = db.scalar(
                    select(SyncPlan).where(
                        SyncPlan.tenant_id == ctx.tenant.id, SyncPlan.id == row.plan_id
                    )
                )
                correlation = linked_plan.correlation_id if linked_plan else None
            observe(
                db,
                ctx.tenant.id,
                category,
                target_kind,
                identifier,
                kind,
                row.id,
                "initial",
                classification,
                correlation,
                observed_at=timecol_value(row, model),
            )
            counts[category] = counts.get(category, 0) + 1
    activity(
        db,
        ctx.tenant.id,
        "INCIDENT_REFRESH",
        "incident",
        ctx.tenant.id,
        actor_id=ctx.identity.user.id,
    )
    return {"scanned": counts, "bounded": True, "limit_per_source": limit, "hours": hours}


def timecol_value(row, model):
    return row.received_at if model is EventReceipt else row.created_at


def change_incident(
    db: Session, ctx: TenantContext, row: Incident, data: IncidentChange
) -> Incident:
    lock(db, ctx, "executions:recover")
    db.refresh(row)
    if row.revision != data.expected_revision:
        raise HTTPException(409, "Incident changed; reload its evidence")
    from app.api.dependencies import assert_permission

    if data.severity or data.assigned_user_id or data.clear_assignment:
        assert_permission(ctx, "integrations:manage")
    previous_severity = row.severity
    previous_assignee = row.assigned_user_id
    previous_status = row.status
    if data.assigned_user_id:
        if not db.scalar(
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                User.is_active,
                Membership.tenant_id == ctx.tenant.id,
                Membership.user_id == data.assigned_user_id,
                Membership.status == "ACTIVE",
            )
        ):
            raise HTTPException(422, "Assignee must be an active member of this tenant")
        row.assigned_user_id = data.assigned_user_id
        activity(
            db,
            ctx.tenant.id,
            "INCIDENT_ASSIGNED",
            "incident",
            row.id,
            row.correlation_id,
            ctx.identity.user.id,
            data.reason,
            outcome=str(data.assigned_user_id),
            incident_id=row.id,
        )
    if data.clear_assignment:
        row.assigned_user_id = None
    if data.severity:
        row.severity = data.severity
    if data.status:
        allowed = {
            "OPEN": {"INVESTIGATING", "MITIGATED", "RESOLVED"},
            "REOPENED": {"INVESTIGATING", "MITIGATED", "RESOLVED"},
            "INVESTIGATING": {"MITIGATED", "RESOLVED"},
            "MITIGATED": {"INVESTIGATING", "RESOLVED", "REOPENED"},
            "RESOLVED": {"REOPENED"},
        }
        if data.status != row.status and data.status not in allowed[row.status]:
            raise HTTPException(409, "Invalid incident transition")
        row.status = data.status
        if row.status == "RESOLVED":
            row.resolution_notes = data.reason
    if data.acknowledge and row.status in {"OPEN", "REOPENED"}:
        row.status = "INVESTIGATING"
    if data.clear_assignment and previous_assignee:
        activity(
            db,
            ctx.tenant.id,
            "INCIDENT_UNASSIGNED",
            "incident",
            row.id,
            row.correlation_id,
            ctx.identity.user.id,
            data.reason,
            str(previous_assignee),
            row.id,
        )
    if row.severity != previous_severity:
        activity(
            db,
            ctx.tenant.id,
            "INCIDENT_SEVERITY_CHANGED",
            "incident",
            row.id,
            row.correlation_id,
            ctx.identity.user.id,
            data.reason,
            previous_severity + " -> " + row.severity,
            row.id,
        )
    if row.status != previous_status:
        activity(
            db,
            ctx.tenant.id,
            "INCIDENT_STATUS_CHANGED",
            "incident",
            row.id,
            row.correlation_id,
            ctx.identity.user.id,
            data.reason,
            previous_status + " -> " + row.status,
            row.id,
        )
    row.revision += 1
    activity(
        db,
        ctx.tenant.id,
        "INCIDENT_UPDATED",
        "incident",
        row.id,
        row.correlation_id,
        ctx.identity.user.id,
        data.reason,
        row.status,
        row.id,
    )
    db.flush()
    return row


def grouped(db: Session, model, tenant_id, column, since=None, timecol=None) -> dict:
    predicates = [model.tenant_id == tenant_id]
    if since:
        predicates.append((timecol if timecol is not None else model.created_at) >= since)
    return dict(db.execute(select(column, func.count()).where(*predicates).group_by(column)).all())


def overview(db: Session, ctx: TenantContext, hours: int) -> dict:
    now = utcnow()
    since = now - timedelta(hours=hours)
    tid = ctx.tenant.id

    def count(model, *predicates):
        return (
            db.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == tid, *predicates)
            )
            or 0
        )

    workflow = grouped(db, WorkflowExecution, tid, WorkflowExecution.state, since)
    deliveries = grouped(db, NotificationDelivery, tid, NotificationDelivery.status, since)
    workers = db.execute(
        select(WorkerLease.id, WorkerLease.heartbeat_at, WorkerLease.stopped_at).where(
            or_(
                exists(
                    select(WorkflowExecution.id).where(
                        WorkflowExecution.tenant_id == tid,
                        WorkflowExecution.lease_worker_id == WorkerLease.id,
                    )
                ),
                exists(
                    select(NotificationDelivery.id).where(
                        NotificationDelivery.tenant_id == tid,
                        NotificationDelivery.worker_id == WorkerLease.id,
                    )
                ),
            )
        )
    ).all()
    fresh = sum(
        stopped is None and heartbeat >= now - timedelta(seconds=30)
        for _, heartbeat, stopped in workers
    )
    connector_rows = list(
        db.scalars(
            select(IntegrationConnection)
            .where(IntegrationConnection.tenant_id == tid)
            .order_by(IntegrationConnection.id)
            .limit(100)
        )
    )
    connector_counts = {}
    for c in connector_rows:
        state = (
            "DISABLED"
            if c.status == "DISABLED"
            else "STALE"
            if c.last_checked_at and c.last_checked_at < now - timedelta(minutes=5)
            else c.health_status
        )
        connector_counts[state] = connector_counts.get(state, 0) + 1
    unit = "hour" if hours <= 24 else "day"
    failure_counts = {}
    for model, timecol, condition in [
        (
            NotificationDeliveryAttempt,
            NotificationDeliveryAttempt.updated_at,
            NotificationDeliveryAttempt.error_category.is_not(None),
        ),
        (
            WorkflowExecutionEvent,
            WorkflowExecutionEvent.created_at,
            WorkflowExecutionEvent.event_type == "STEP_FAILED",
        ),
    ]:
        bucket = func.date_trunc(unit, timecol)
        for stamp, n in db.execute(
            select(bucket, func.count())
            .where(model.tenant_id == tid, timecol >= since, condition)
            .group_by(bucket)
            .order_by(bucket)
        ):
            failure_counts[stamp] = failure_counts.get(stamp, 0) + n
    trend = sorted(failure_counts.items())
    return {
        "checked_at": now,
        "window_hours": hours,
        "window_start": since,
        "webhook_receipts": grouped(
            db, EventReceipt, tid, EventReceipt.outcome, since, EventReceipt.received_at
        ),
        "accepted_events": count(IncomingEvent, IncomingEvent.received_at >= since),
        "workflows": workflow,
        "deliveries": deliveries,
        "sync": grouped(db, SyncExecution, tid, SyncExecution.state, since),
        "dead_letters": count(DeadLetterRecord, DeadLetterRecord.created_at >= since),
        "recovery_outcomes": grouped(db, RecoveryRequest, tid, RecoveryRequest.status, since),
        "snapshot": {
            "workflow_backlog": count(
                WorkflowExecution,
                WorkflowExecution.state.in_(["PENDING", "WAITING", "RETRY_SCHEDULED", "RUNNING"]),
            ),
            "expired_workflow_leases": count(
                WorkflowExecution,
                WorkflowExecution.state == "RUNNING",
                WorkflowExecution.lease_expires_at < now,
            ),
            "open_sync_conflicts": count(SyncConflict, SyncConflict.status == "OPEN"),
            "pending_notification_retries": count(
                NotificationDelivery, NotificationDelivery.status == "RETRY_SCHEDULED"
            ),
            "open_alerts": count(OperationalAlert, OperationalAlert.status != "RESOLVED"),
            "unresolved_calendar_conflicts": count(
                OperationalAlert,
                OperationalAlert.category == "CALENDAR_CONFLICT",
                OperationalAlert.status != "RESOLVED",
            ),
            "tenant_lease_workers_fresh": fresh,
            "tenant_lease_workers_stale": len(workers) - fresh,
            "worker_health": "HEALTHY"
            if fresh and fresh == len(workers)
            else "STALE"
            if workers
            else "UNKNOWN",
            "connector_health": connector_counts,
            "connector_sample_limit": 100,
        },
        "failure_trend": [{"timestamp": stamp, "count": n} for stamp, n in trend],
        "basis": "Window counts use retained records created/received in the selected window; backlog and health are current snapshots. Worker counts cover tenant-associated leases only.",
    }


def timeline(
    db: Session, ctx: TenantContext, correlation_id: uuid.UUID, offset: int = 0, limit: int = 100
) -> dict:
    tid = ctx.tenant.id
    items = []
    seen = set()
    ids = {}
    bounded = False

    def add(model, kind, predicates, status_attr="state", source="platform", linked=False):
        nonlocal bounded
        rows = list(
            db.scalars(
                select(model)
                .where(model.tenant_id == tid, or_(*predicates))
                .order_by(
                    (
                        model.received_at
                        if model in {EventReceipt, IncomingEvent}
                        else model.created_at
                    ),
                    model.id,
                )
                .limit(101)
            )
        )
        if len(rows) > 100:
            bounded = True
        rows = rows[:100]
        ids.setdefault(kind, set()).update(r.id for r in rows)
        for r in rows:
            key = (model.__tablename__, r.id)
            if key in seen:
                continue
            seen.add(key)
            status = str(getattr(r, status_attr, "RECORDED") or "RECORDED")
            explicit_corr = getattr(r, "correlation_id", None)
            items.append(
                {
                    "id": r.id,
                    "event_type": model.__tablename__,
                    "timestamp": r.received_at
                    if model in {EventReceipt, IncomingEvent}
                    else r.created_at,
                    "status": status,
                    "source_system": source,
                    "entity_reference": r.id,
                    "correlation_id": correlation_id,
                    "summary": kind.replace("_", " ").title() + ": " + status,
                    "detail_link": LINKS.get(kind, "/operations"),
                    "provenance": "CORRELATION_ID"
                    if explicit_corr == correlation_id
                    else "DURABLE_REFERENCE",
                }
            )
        return rows

    # Walk stored upstream references as well as correlation matches. No time-based joins.
    seed_event_ids = set(
        db.scalars(
            select(IncomingEvent.id)
            .where(IncomingEvent.tenant_id == tid, IncomingEvent.correlation_id == correlation_id)
            .limit(100)
        )
    )
    for model in (TransformationAttempt, SyncPlan, WorkflowExecution):
        seed_event_ids.update(
            db.scalars(
                select(model.event_id)
                .where(
                    model.tenant_id == tid,
                    model.correlation_id == correlation_id,
                    model.event_id.is_not(None),
                )
                .limit(100)
            )
        )
    seed_intents = list(
        db.scalars(
            select(NotificationIntent)
            .where(
                NotificationIntent.tenant_id == tid,
                NotificationIntent.correlation_id == correlation_id,
            )
            .limit(100)
        )
    )
    referenced_jobs = [r.workflow_execution_id for r in seed_intents if r.workflow_execution_id]
    seed_event_ids.update(
        db.scalars(
            select(WorkflowExecution.event_id)
            .where(WorkflowExecution.tenant_id == tid, WorkflowExecution.id.in_(referenced_jobs))
            .limit(100)
        )
    )
    referenced_syncs = [r.sync_execution_id for r in seed_intents if r.sync_execution_id]
    seed_event_ids.update(
        db.scalars(
            select(SyncPlan.event_id)
            .join(SyncExecution, SyncExecution.plan_id == SyncPlan.id)
            .where(
                SyncPlan.tenant_id == tid,
                SyncExecution.tenant_id == tid,
                SyncExecution.id.in_(referenced_syncs),
                SyncPlan.event_id.is_not(None),
            )
            .limit(100)
        )
    )
    receipts = add(
        EventReceipt,
        "receipt",
        [
            EventReceipt.correlation_id == correlation_id,
            EventReceipt.event_id.in_(list(seed_event_ids)),
        ],
        "outcome",
        "webhook",
    )
    events = add(
        IncomingEvent,
        "event",
        [
            IncomingEvent.correlation_id == correlation_id,
            IncomingEvent.id.in_(
                [r.event_id for r in receipts if r.event_id] + list(seed_event_ids)
            ),
        ],
        "ingestion_status",
        "synthetic_crm",
    )
    eventids = [r.id for r in events]
    add(
        TransformationAttempt,
        "transformation",
        [
            TransformationAttempt.correlation_id == correlation_id,
            TransformationAttempt.event_id.in_(eventids),
        ],
        "outcome",
        "canonical",
    )
    jobs = add(
        WorkflowExecution,
        "workflow",
        [
            WorkflowExecution.correlation_id == correlation_id,
            WorkflowExecution.event_id.in_(eventids),
        ],
    )
    jobids = [r.id for r in jobs]
    add(WorkflowDispatchRecord, "dispatch", [WorkflowDispatchRecord.event_id.in_(eventids)])
    add(WorkflowStepExecution, "step", [WorkflowStepExecution.execution_id.in_(jobids)])
    add(
        WorkflowExecutionEvent,
        "workflow_history",
        [WorkflowExecutionEvent.execution_id.in_(jobids)],
        "event_type",
    )
    plans = add(
        SyncPlan,
        "plan",
        [SyncPlan.correlation_id == correlation_id, SyncPlan.event_id.in_(eventids)],
        "decision",
        "sync",
    )
    planids = [r.id for r in plans]
    syncs = add(
        SyncExecution,
        "sync",
        [SyncExecution.plan_id.in_(planids), SyncExecution.workflow_execution_id.in_(jobids)],
        source="sync",
    )
    syncids = [r.id for r in syncs]
    add(
        SyncOperation,
        "ats_action",
        [SyncOperation.execution_id.in_(syncids)],
        "outcome",
        "synthetic_ats",
    )
    add(SyncConflict, "conflict", [SyncConflict.execution_id.in_(syncids)], "status", "sync")
    intents = add(
        NotificationIntent,
        "intent",
        [
            NotificationIntent.correlation_id == correlation_id,
            NotificationIntent.sync_execution_id.in_(syncids),
            NotificationIntent.workflow_execution_id.in_(jobids),
        ],
        "status",
        "notification",
    )
    deliveries = add(
        NotificationDelivery,
        "delivery",
        [
            NotificationDelivery.correlation_id == correlation_id,
            NotificationDelivery.intent_id.in_([r.id for r in intents]),
        ],
        "status",
        "notification",
    )
    deliveryids = [r.id for r in deliveries]
    add(
        NotificationDeliveryAttempt,
        "delivery_attempt",
        [NotificationDeliveryAttempt.delivery_id.in_(deliveryids)],
        "status",
        "notification",
    )
    add(
        NotificationReceipt,
        "delivery_receipt",
        [NotificationReceipt.delivery_id.in_(deliveryids)],
        source="notification",
    )
    requests = add(
        CalendarCoordinationRequest,
        "calendar",
        [CalendarCoordinationRequest.delivery_id.in_(deliveryids)],
        "action",
        "synthetic_calendar",
    )
    add(
        CalendarCoordinationAttempt,
        "calendar_attempt",
        [CalendarCoordinationAttempt.request_id.in_([r.id for r in requests])],
        "outcome",
        "synthetic_calendar",
    )
    alerts = add(
        OperationalAlert, "alert", [OperationalAlert.correlation_id == correlation_id], "status"
    )
    add(
        AlertEvent,
        "alert_history",
        [
            AlertEvent.correlation_id == correlation_id,
            AlertEvent.alert_id.in_([r.id for r in alerts]),
        ],
        "event_type",
    )
    add(
        OperationsActivity,
        "activity",
        [OperationsActivity.correlation_id == correlation_id],
        "outcome",
    )
    add(RecoveryRequest, "recovery", [RecoveryRequest.correlation_id == correlation_id], "status")
    if not items:
        raise HTTPException(404, "No linked evidence in this tenant")
    items.sort(key=lambda x: (x["timestamp"], x["event_type"], str(x["id"])))
    return {
        "correlation_id": correlation_id,
        "items": items[offset : offset + limit],
        "offset": offset,
        "limit": limit,
        "bounded": bounded or len(items) > offset + limit,
        "unlinked": "Records without this stored correlation ID or an explicit traversed reference are unlinked; timestamp similarity is not causality.",
    }
