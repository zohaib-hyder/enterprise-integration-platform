"""Bounded, explicitly requested consistency scans; no business-state repair."""

import uuid
from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.core.security import utcnow
from app.models.canonical import ExternalRecordMapping
from app.models.notification import (
    NotificationDelivery,
    NotificationIntent,
    NotificationRule,
    NotificationRuleVersion,
)
from app.models.operations import Incident, OperationalFinding, OperationalReconciliationRun
from app.models.reliability import AlertEvent, OperationalAlert
from app.models.synchronization import SyncExecution, SyncOperation, SyncPlan
from app.models.webhook import IncomingEvent
from app.models.workflow import (
    RetrySchedule,
    WorkflowDefinition,
    WorkflowDispatchRecord,
    WorkflowExecution,
    WorkflowStepExecution,
    WorkflowTrigger,
    WorkflowVersion,
)
from app.schemas.operations import ScanInput
from app.services import operations as ops
from app.services.notifications import digest, lock


def scan(
    db: Session, ctx: TenantContext, data: ScanInput, correlation_id: uuid.UUID
) -> OperationalReconciliationRun:
    lock(db, ctx, "executions:recover")
    now = utcnow()
    since = now - timedelta(hours=data.hours)
    past_due = now - timedelta(seconds=data.grace_seconds)
    run = OperationalReconciliationRun(
        tenant_id=ctx.tenant.id,
        actor_id=ctx.identity.user.id,
        correlation_id=correlation_id,
        window_hours=data.hours,
        scan_limit=data.limit,
        summary={},
    )
    db.add(run)
    db.flush()
    counts = {}
    scanned = {}
    truncated = []

    def rows(model, conditions):
        result = list(
            db.scalars(
                select(model)
                .where(model.tenant_id == ctx.tenant.id, model.created_at >= since, *conditions)
                .order_by(model.created_at, model.id)
                .limit(data.limit + 1)
            )
        )
        scanned[model.__tablename__] = scanned.get(model.__tablename__, 0) + min(
            len(result), data.limit
        )
        if len(result) > data.limit:
            truncated.append(model.__tablename__)
        return result[: data.limit]

    def finding(category, kind, row, classification):
        db.add(
            OperationalFinding(
                tenant_id=ctx.tenant.id,
                run_id=run.id,
                category=category,
                target_kind=kind,
                target_id=row.id,
                correlation_id=getattr(row, "correlation_id", None),
                classification=classification,
            )
        )
        counts[category] = counts.get(category, 0) + 1

    eligible = exists(
        select(WorkflowTrigger.id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowTrigger.version_id)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowVersion.workflow_id)
        .where(
            WorkflowDefinition.tenant_id == ctx.tenant.id,
            WorkflowDefinition.status == "ACTIVE",
            WorkflowDefinition.active_version == WorkflowVersion.version,
            WorkflowTrigger.connection_id == IncomingEvent.connection_id,
            WorkflowTrigger.source == IncomingEvent.source,
            WorkflowTrigger.event_types.op("?")(IncomingEvent.event_type),
            or_(
                WorkflowTrigger.event_id_prefix.is_(None),
                func.left(
                    IncomingEvent.provider_event_id, func.length(WorkflowTrigger.event_id_prefix)
                )
                == WorkflowTrigger.event_id_prefix,
            ),
        )
    )
    dispatch = exists(
        select(WorkflowDispatchRecord.id).where(
            WorkflowDispatchRecord.tenant_id == ctx.tenant.id,
            WorkflowDispatchRecord.event_id == IncomingEvent.id,
        )
    )
    for event in rows(
        IncomingEvent,
        [
            IncomingEvent.ingestion_status == "ACCEPTED",
            IncomingEvent.received_at < past_due,
            eligible,
            ~dispatch,
        ],
    ):
        finding(
            "ACCEPTED_EVENT_UNDISPATCHED",
            "event",
            event,
            "ELIGIBLE_TRIGGER_WITHOUT_DISPATCH_AFTER_GRACE",
        )
    for execution in rows(
        WorkflowExecution,
        [WorkflowExecution.state.in_(["PENDING", "WAITING", "RETRY_SCHEDULED", "RUNNING"])],
    ):
        if (
            execution.state == "RUNNING"
            and execution.lease_expires_at
            and execution.lease_expires_at < now
        ):
            finding("WORKER_LEASE_EXPIRED", "workflow", execution, "EXPIRED_WORKFLOW_LEASE")
        elif execution.state != "RUNNING" and execution.next_run_at < past_due:
            finding("WORKFLOW_STALLED", "workflow", execution, "WORK_PAST_DUE_GRACE")
    for schedule in rows(RetrySchedule, [RetrySchedule.state == "SCHEDULED"]):
        execution = db.scalar(
            select(WorkflowExecution).where(
                WorkflowExecution.tenant_id == ctx.tenant.id,
                WorkflowExecution.id == schedule.execution_id,
            )
        )
        step = db.scalar(
            select(WorkflowStepExecution).where(
                WorkflowStepExecution.tenant_id == ctx.tenant.id,
                WorkflowStepExecution.id == schedule.step_execution_id,
            )
        )
        if (
            not execution
            or not step
            or execution.state != "RETRY_SCHEDULED"
            or step.state != "RETRY_SCHEDULED"
        ):
            finding(
                "ORPHANED_RETRY_SCHEDULE",
                "workflow",
                execution or schedule,
                "SCHEDULE_WITHOUT_RETRYING_EXECUTION",
            )
    for delivery in rows(
        NotificationDelivery,
        [
            NotificationDelivery.status.in_(
                ["PENDING", "RETRY_SCHEDULED", "RUNNING", "UNKNOWN", "FAILED"]
            )
        ],
    ):
        if delivery.status == "UNKNOWN":
            finding("UNKNOWN_OUTCOME", "delivery", delivery, "NO_CONFIRMED_PROVIDER_RECEIPT")
        elif (
            delivery.status == "RUNNING"
            and delivery.lease_expires_at
            and delivery.lease_expires_at < now
        ):
            finding("WORKER_LEASE_EXPIRED", "delivery", delivery, "EXPIRED_DELIVERY_LEASE")
        elif (
            delivery.status in {"PENDING", "RETRY_SCHEDULED"}
            and delivery.next_attempt_at < past_due
        ):
            finding("DELIVERY_OVERDUE", "delivery", delivery, "ACTION_PAST_DUE_GRACE")
        if delivery.status == "FAILED" and not db.scalar(
            select(OperationalAlert.id).where(
                OperationalAlert.tenant_id == ctx.tenant.id,
                OperationalAlert.resource_key == str(delivery.id),
                OperationalAlert.category == "TERMINAL_NOTIFICATION_FAILURE",
            )
        ):
            finding(
                "TERMINAL_FAILURE_WITHOUT_ALERT",
                "delivery",
                delivery,
                "MISSING_EXPECTED_DELIVERY_ALERT",
            )
    for execution in rows(WorkflowExecution, [WorkflowExecution.state == "DEAD_LETTERED"]):
        alert = exists(
            select(OperationalAlert.id).where(
                OperationalAlert.tenant_id == ctx.tenant.id,
                OperationalAlert.category == "WORKFLOW_DEAD_LETTERED",
                OperationalAlert.context["execution_id"].astext == str(execution.id),
            )
        )
        historical = exists(
            select(AlertEvent.id)
            .join(OperationalAlert, OperationalAlert.id == AlertEvent.alert_id)
            .where(
                AlertEvent.tenant_id == ctx.tenant.id,
                AlertEvent.correlation_id == execution.correlation_id,
                OperationalAlert.category == "WORKFLOW_DEAD_LETTERED",
            )
        )
        if not db.scalar(select(or_(alert, historical))):
            finding(
                "TERMINAL_FAILURE_WITHOUT_ALERT",
                "workflow",
                execution,
                "MISSING_EXPECTED_WORKFLOW_ALERT",
            )
    temporal_skips = 0
    for operation in rows(SyncOperation, [SyncOperation.outcome.in_(["CREATE", "UPDATE"])]):
        plan = db.scalar(
            select(SyncPlan).where(
                SyncPlan.tenant_id == ctx.tenant.id, SyncPlan.id == operation.plan_id
            )
        )
        execution = db.scalar(
            select(SyncExecution).where(
                SyncExecution.tenant_id == ctx.tenant.id, SyncExecution.id == operation.execution_id
            )
        )
        identity = db.scalar(
            select(ExternalRecordMapping.id).where(
                ExternalRecordMapping.tenant_id == ctx.tenant.id,
                ExternalRecordMapping.canonical_id == operation.canonical_id,
                ExternalRecordMapping.connection_id == operation.destination_connection_id,
                ExternalRecordMapping.external_id
                == plan.evidence["destination_identity"]["external_id"],
                ExternalRecordMapping.source_kind == "requisition",
            )
        )
        if not identity:
            finding(
                "SYNC_IDENTITY_MISSING",
                "sync",
                execution,
                "ATS_OPERATION_WITHOUT_EXPECTED_IDENTITY",
            )
        rules = db.execute(
            select(NotificationRule, NotificationRuleVersion)
            .join(
                NotificationRuleVersion,
                and_(
                    NotificationRuleVersion.rule_id == NotificationRule.id,
                    NotificationRuleVersion.version == NotificationRule.active_version,
                ),
            )
            .where(
                NotificationRule.tenant_id == ctx.tenant.id,
                NotificationRule.enabled,
                NotificationRuleVersion.trigger == "SYNC_" + operation.outcome,
                or_(
                    NotificationRuleVersion.sync_configuration_id.is_(None),
                    NotificationRuleVersion.sync_configuration_id == execution.configuration_id,
                ),
            )
            .order_by(NotificationRule.id)
            .limit(data.limit + 1)
        ).all()
        if len(rules) > data.limit:
            truncated.append("notification_rules")
        for rule, version in rules[: data.limit]:
            # Historical enable transitions were not stored in Phase 8. Never guess.
            if rule.updated_at > operation.created_at or version.created_at > operation.created_at:
                temporal_skips += 1
                continue
            key = digest(
                [
                    str(ctx.tenant.id),
                    str(rule.id),
                    "sync:" + str(execution.id) + ":SYNC_" + operation.outcome,
                ]
            )
            if not db.scalar(
                select(NotificationIntent.id).where(
                    NotificationIntent.tenant_id == ctx.tenant.id,
                    NotificationIntent.idempotency_key == key,
                )
            ):
                finding(
                    "NOTIFICATION_INTENT_MISSING",
                    "sync",
                    execution,
                    "EXPECTED_PINNED_OUTBOX_INTENT_ABSENT",
                )
    for incident in rows(Incident, [Incident.status.in_(["RESOLVED", "MITIGATED"])]):
        model = ops.TARGETS.get(incident.target_kind)
        source = (
            db.scalar(
                select(model).where(
                    model.tenant_id == ctx.tenant.id, model.id == incident.target_id
                )
            )
            if model
            else None
        )
        if (
            source
            and getattr(source, "updated_at", incident.updated_at) > incident.updated_at
            and getattr(source, "status", getattr(source, "state", ""))
            in {"FAILED", "UNKNOWN", "DEAD_LETTERED", "OPEN"}
        ):
            finding(
                "STALE_INCIDENT_STATE",
                "incident",
                incident,
                "NEWER_UNRESOLVED_SOURCE_AFTER_INCIDENT_CLOSURE",
            )
    run.summary = {
        "counts": counts,
        "finding_count": sum(counts.values()),
        "scanned": scanned,
        "truncated_sources": sorted(set(truncated)),
        "historically_indeterminate_rules_skipped": temporal_skips,
        "grace_seconds": data.grace_seconds,
        "read_only_business_state": True,
        "bounded": True,
    }
    ops.activity(
        db,
        ctx.tenant.id,
        "RECONCILIATION_COMPLETED",
        "reconciliation",
        run.id,
        correlation_id,
        ctx.identity.user.id,
        outcome="READ_ONLY_SCAN",
    )
    db.flush()
    return run
