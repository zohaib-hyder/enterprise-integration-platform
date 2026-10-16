"""Transactional outbox and bounded actions processed by the Phase 5 worker.

Synthetic effect, receipt and completion share a commit. No external transport.
"""

import hashlib
import html
import json
import re
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.api.dependencies import Identity, TenantContext, assert_permission, authorized_context
from app.connectors.contracts import ConnectorOperation, OperationName
from app.connectors.reliability import ReliabilityError
from app.core.security import utcnow
from app.models.identity import User
from app.models.integration import (
    IntegrationConnection,
    SyntheticCalendarEvent,
    SyntheticEmail,
    SyntheticNotification,
)
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
from app.models.synchronization import SyncConfiguration, SyncExecution, SyncPlan
from app.models.tenant import Tenant
from app.schemas.notification import (
    CalendarCancel,
    CalendarChange,
    CalendarCreate,
    ManualNotification,
    RenderInputs,
    RuleCreate,
    RuleDefinition,
    RuleUpdate,
    TemplateCreate,
    TemplateVersionInput,
)
from app.schemas.workflow import RetryPolicy
from app.services import integrations
from app.services.audit import record
from app.services.synchronization import public, scoped


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def lock(db: Session, ctx: TenantContext, permission: str = "integrations:manage") -> None:
    db.scalar(select(Tenant).where(Tenant.id == ctx.tenant.id).with_for_update())
    db.refresh(ctx.membership)
    assert_permission(authorized_context(db, ctx.identity, ctx.tenant.id), permission)


def audit(db: Session, ctx: TenantContext, action: str, row) -> None:
    record(
        db,
        action,
        "delivery" if isinstance(row, NotificationDelivery) else "notification",
        row.id,
        ctx.tenant.id,
        ctx.identity.user.id,
    )


def new_template(db: Session, ctx: TenantContext, data: TemplateCreate) -> NotificationTemplate:
    lock(db, ctx)
    row = NotificationTemplate(tenant_id=ctx.tenant.id, name=data.name, channel=data.channel)
    db.add(row)
    db.flush()
    new_template_version(db, ctx, row, TemplateVersionInput(subject=data.subject, body=data.body))
    audit(db, ctx, "notification.template_created", row)
    return row


def new_template_version(
    db: Session, ctx: TenantContext, template: NotificationTemplate, data: TemplateVersionInput
) -> NotificationTemplateVersion:
    lock(db, ctx)
    if template.channel == "EMAIL" and not data.subject.strip():
        raise HTTPException(422, "Email requires a subject")
    number = (
        db.scalar(
            select(func.max(NotificationTemplateVersion.version)).where(
                NotificationTemplateVersion.template_id == template.id
            )
        )
        or 0
    ) + 1
    row = NotificationTemplateVersion(
        tenant_id=ctx.tenant.id,
        template_id=template.id,
        version=number,
        subject=data.subject,
        body=data.body,
    )
    db.add(row)
    db.flush()
    audit(db, ctx, "notification.template_version_created", row)
    return row


def activate_template(
    db: Session, ctx: TenantContext, template_id: uuid.UUID, version_id: uuid.UUID
):
    lock(db, ctx)
    template = scoped(db, ctx, NotificationTemplate, template_id)
    version = scoped(db, ctx, NotificationTemplateVersion, version_id)
    if version.template_id != template.id:
        raise HTTPException(422, "Version belongs to another template")
    template.active_version = version.version
    audit(db, ctx, "notification.template_activated", template)
    return template


def render(db: Session, ctx: TenantContext, version_id: uuid.UUID, inputs: RenderInputs) -> dict:
    version = scoped(db, ctx, NotificationTemplateVersion, version_id)
    template = scoped(db, ctx, NotificationTemplate, version.template_id)
    values = {
        key: html.escape(value, quote=True).replace("@", "＠")
        for key, value in inputs.model_dump().items()
    }

    def replace(content: str, limit: int) -> str:
        return re.sub(
            r"\{([a-z_]+)\}",
            lambda m: values[m[1]],
            html.escape(content, quote=True).replace("@", "＠"),
        )[:limit]

    return {
        "subject": replace(version.subject, 200),
        "body": replace(version.body, 2000 if template.channel == "SLACK" else 4000),
        "channel": template.channel,
        "synthetic": True,
    }


def validate_rule(db: Session, ctx: TenantContext, data: RuleDefinition) -> str:
    connection = scoped(db, ctx, IntegrationConnection, data.connection_id)
    version = scoped(db, ctx, NotificationTemplateVersion, data.template_version_id)
    template = scoped(db, ctx, NotificationTemplate, version.template_id)
    if template.active_version != version.version:
        raise HTTPException(422, "Pin an activated template version")
    expected = "synthetic_slack" if template.channel == "SLACK" else "synthetic_email"
    allowed = connection.public_config.get(
        "allowed_channels" if template.channel == "SLACK" else "allowed_recipients", []
    )
    if connection.connector_type != expected or data.destination not in allowed:
        raise HTTPException(422, "Destination is not allowlisted for this connection/channel")
    if data.sync_configuration_id:
        if not data.trigger.startswith("SYNC_"):
            raise HTTPException(422, "Only sync triggers accept a sync configuration scope")
        scoped(db, ctx, SyncConfiguration, data.sync_configuration_id)
    return template.channel


def rule_version(
    db: Session, ctx: TenantContext, rule: NotificationRule, definition: RuleDefinition
):
    validate_rule(db, ctx, definition)
    version = (
        db.scalar(
            select(func.max(NotificationRuleVersion.version)).where(
                NotificationRuleVersion.rule_id == rule.id
            )
        )
        or 0
    ) + 1
    row = NotificationRuleVersion(
        tenant_id=ctx.tenant.id, rule_id=rule.id, version=version, **definition.model_dump()
    )
    db.add(row)
    db.flush()
    rule.active_version = version
    return row


def new_rule(db: Session, ctx: TenantContext, data: RuleCreate):
    lock(db, ctx)
    row = NotificationRule(
        tenant_id=ctx.tenant.id,
        name=data.name,
        enabled=data.enabled,
        created_by=ctx.identity.user.id,
    )
    db.add(row)
    db.flush()
    rule_version(db, ctx, row, RuleDefinition(**data.model_dump(exclude={"name", "enabled"})))
    audit(db, ctx, "notification.rule_created", row)
    return row


def update_rule(db: Session, ctx: TenantContext, row: NotificationRule, data: RuleUpdate):
    lock(db, ctx)
    db.refresh(row)
    if row.revision != data.expected_revision:
        raise HTTPException(409, "Rule changed; reload before updating")
    if data.definition:
        rule_version(db, ctx, row, data.definition)
    row.enabled = data.enabled
    row.revision += 1
    audit(db, ctx, "notification.rule_updated", row)
    return row


def create_delivery(
    db: Session,
    ctx: TenantContext,
    connection_id: uuid.UUID,
    channel: str,
    operation: str,
    payload: dict,
    key: str,
    correlation: uuid.UUID,
    intent_id: uuid.UUID | None = None,
) -> NotificationDelivery:
    row = NotificationDelivery(
        tenant_id=ctx.tenant.id,
        connection_id=connection_id,
        principal_id=ctx.identity.user.id,
        channel=channel,
        operation=operation,
        payload=payload,
        idempotency_key=key,
        correlation_id=correlation,
        intent_id=intent_id,
        retry_policy=RetryPolicy().model_dump(),
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.flush()
    audit(db, ctx, "notification.action_queued", row)
    return row


def enqueue(
    db: Session,
    ctx: TenantContext,
    rule: NotificationRule,
    version: NotificationRuleVersion,
    inputs: RenderInputs,
    source_key: str,
    correlation: uuid.UUID,
    workflow_id: uuid.UUID | None = None,
    sync_id: uuid.UUID | None = None,
) -> NotificationIntent:
    key = digest([str(ctx.tenant.id), str(rule.id), source_key])
    previous = db.scalar(
        select(NotificationIntent).where(
            NotificationIntent.tenant_id == ctx.tenant.id, NotificationIntent.idempotency_key == key
        )
    )
    if previous:
        if version.trigger == "MANUAL" and (
            previous.rule_version_id != version.id
            or previous.rendering_inputs != inputs.model_dump()
        ):
            raise HTTPException(409, "Request key already used with different content")
        return previous
    rendered = render(db, ctx, version.template_version_id, inputs)
    row = NotificationIntent(
        tenant_id=ctx.tenant.id,
        rule_version_id=version.id,
        template_version_id=version.template_version_id,
        workflow_execution_id=workflow_id,
        sync_execution_id=sync_id,
        channel=rendered["channel"],
        destination=version.destination,
        rendering_inputs=inputs.model_dump(),
        correlation_id=correlation,
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    payload = (
        {"channel": version.destination, "text": rendered["body"], "idempotency_key": key}
        if rendered["channel"] == "SLACK"
        else {
            "recipient": version.destination,
            "subject": rendered["subject"],
            "body": rendered["body"],
            "idempotency_key": key,
        }
    )
    create_delivery(
        db,
        ctx,
        version.connection_id,
        rendered["channel"],
        "message.send",
        payload,
        key,
        correlation,
        row.id,
    )
    return row


def matching_rules(
    db: Session, tenant_id: uuid.UUID, trigger: str, configuration_id: uuid.UUID | None = None
):
    return db.execute(
        select(NotificationRule, NotificationRuleVersion)
        .join(
            NotificationRuleVersion,
            and_(
                NotificationRuleVersion.rule_id == NotificationRule.id,
                NotificationRuleVersion.version == NotificationRule.active_version,
            ),
        )
        .where(
            NotificationRule.tenant_id == tenant_id,
            NotificationRule.enabled,
            NotificationRuleVersion.trigger == trigger,
            or_(
                NotificationRuleVersion.sync_configuration_id.is_(None),
                NotificationRuleVersion.sync_configuration_id == configuration_id,
            ),
        )
    ).all()


def safe_value(value: object, length: int) -> str:
    # Only explicitly selected business fields. Redact tokens/URLs/control characters.
    value = str(value or "")[:length]
    cleaned = re.sub(
        r"(?:[a-z][a-z0-9+.-]*://|www\.|mailto:|javascript:|data:)\S+|(?:secret|password|token)\s*[:=]\S+|[\x00-\x1f]",
        "[redacted]",
        value,
        flags=re.IGNORECASE,
    )[:length]

    try:
        return RenderInputs.safe_inputs(cleaned)
    except ValueError:
        return "[redacted]"


def emit_sync(
    db: Session,
    ctx: TenantContext,
    plan: SyncPlan,
    execution: SyncExecution,
    conflict: bool = False,
) -> int:
    trigger = "SYNC_CONFLICT" if conflict else "SYNC_" + plan.decision
    if trigger not in {"SYNC_CREATE", "SYNC_UPDATE", "SYNC_CONFLICT"}:
        return 0
    inputs = RenderInputs(
        job_title=safe_value(plan.evidence.get("source_data", {}).get("title"), 160)
        or "Synthetic requisition",
        requisition_reference=safe_value(plan.evidence["source_identity"]["external_id"], 100),
        sync_outcome=plan.decision,
        correlation_id=str(plan.correlation_id),
        review_reference=str(execution.id),
    )
    for rule, version in matching_rules(db, ctx.tenant.id, trigger, execution.configuration_id):
        # Queue on the rule's configured principal, not an operations preview principal.
        rule_ctx = SimpleNamespace(
            tenant=ctx.tenant, identity=SimpleNamespace(user=SimpleNamespace(id=rule.created_by))
        )
        enqueue(
            db,
            rule_ctx,
            rule,
            version,
            inputs,
            "sync:" + str(execution.id) + ":" + trigger,
            plan.correlation_id,
            execution.workflow_execution_id,
            execution.id,
        )
    return 1


def emit_failure(db: Session, execution) -> None:
    ctx = SimpleNamespace(
        tenant=SimpleNamespace(id=execution.tenant_id),
        identity=SimpleNamespace(user=SimpleNamespace(id=execution.principal_id)),
    )
    for rule, version in matching_rules(db, execution.tenant_id, "WORKFLOW_FAILURE"):
        rule_ctx = SimpleNamespace(
            tenant=ctx.tenant, identity=SimpleNamespace(user=SimpleNamespace(id=rule.created_by))
        )
        enqueue(
            db,
            rule_ctx,
            rule,
            version,
            RenderInputs(
                sync_outcome=execution.state,
                correlation_id=str(execution.correlation_id),
                review_reference=str(execution.id),
            ),
            "workflow:" + str(execution.root_id) + ":failure",
            execution.correlation_id,
            execution.id,
        )


def manual(db: Session, ctx: TenantContext, data: ManualNotification, correlation: uuid.UUID):
    lock(db, ctx)
    rule = scoped(db, ctx, NotificationRule, data.rule_id)
    version = db.scalar(
        select(NotificationRuleVersion).where(
            NotificationRuleVersion.rule_id == rule.id,
            NotificationRuleVersion.version == rule.active_version,
        )
    )
    if not rule.enabled or version.trigger != "MANUAL":
        raise HTTPException(409, "Enable an explicit manual rule first")
    return enqueue(
        db, ctx, rule, version, data.inputs, "manual:" + str(data.request_key), correlation
    )


def set_status(db: Session, row: NotificationDelivery, status: str) -> None:
    row.status = status
    if row.intent_id:
        intent = db.scalar(
            select(NotificationIntent).where(
                NotificationIntent.id == row.intent_id,
                NotificationIntent.tenant_id == row.tenant_id,
            )
        )
        intent.status = status
    if status != "RUNNING":
        row.lease_token = row.lease_expires_at = row.worker_id = None


def claim(db: Session, worker_id: uuid.UUID, lease_seconds: int = 30):
    from app.workflows.engine import heartbeat, queue_tenants

    if not 15 <= lease_seconds <= 120:
        raise ValueError("Invalid lease duration")
    heartbeat(db, worker_id)
    now = utcnow()
    due = or_(
        and_(
            NotificationDelivery.status.in_(["PENDING", "RETRY_SCHEDULED"]),
            NotificationDelivery.next_attempt_at <= now,
        ),
        and_(
            NotificationDelivery.status == "RUNNING", NotificationDelivery.lease_expires_at <= now
        ),
    )
    q = select(NotificationDelivery).where(due)
    admitted = queue_tenants(db, NotificationDelivery, q)
    if not admitted:
        return None
    row = db.scalar(
        q.where(NotificationDelivery.tenant_id.in_(admitted))
        .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if not row:
        return None
    if row.status == "RUNNING":
        prior = db.scalar(
            select(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.delivery_id == row.id,
                NotificationDeliveryAttempt.number == row.attempt_count,
            )
        )
        if prior and prior.status == "RUNNING":
            prior.status = "LEASE_RECOVERED"
        # Local actions and receipts are atomic: expired claim has no committed effect.
        # Still inspect provider evidence before dispatching any recovered action.
    row.attempt_count += 1
    row.status = "RUNNING"
    row.worker_id = worker_id
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = now + timedelta(seconds=lease_seconds)
    attempt = NotificationDeliveryAttempt(
        tenant_id=row.tenant_id, delivery_id=row.id, number=row.attempt_count, status="RUNNING"
    )
    db.add(attempt)
    db.flush()
    calendar = db.scalar(
        select(CalendarCoordinationRequest).where(CalendarCoordinationRequest.delivery_id == row.id)
    )
    if calendar:
        db.add(
            CalendarCoordinationAttempt(
                tenant_id=row.tenant_id,
                request_id=calendar.id,
                delivery_attempt_id=attempt.id,
                outcome="RUNNING",
            )
        )
    return row.id, row.lease_token


def provider_receipt(db: Session, row: NotificationDelivery) -> dict | None:
    receipt = db.scalar(
        select(NotificationReceipt).where(
            NotificationReceipt.delivery_id == row.id,
            NotificationReceipt.tenant_id == row.tenant_id,
        )
    )
    if receipt:
        return receipt.result
    if row.channel in {"SLACK", "EMAIL"}:
        model = SyntheticNotification if row.channel == "SLACK" else SyntheticEmail
        provider = db.scalar(
            select(model).where(
                model.tenant_id == row.tenant_id,
                model.connection_id == row.connection_id,
                model.idempotency_key == row.idempotency_key,
            )
        )
        if provider:
            from app.connectors.contracts import EmailSend, SlackSend
            from app.connectors.simulators import fingerprint

            schema = SlackSend if row.channel == "SLACK" else EmailSend
            if provider.request_hash != fingerprint(schema.model_validate(row.payload)):
                return {"mismatch": True}
            return {
                "provider_reference": str(provider.id),
                "status": "SIMULATED",
                "synthetic": True,
            }
    elif row.operation == "event.create":
        provider = db.scalar(
            select(SyntheticCalendarEvent).where(
                SyntheticCalendarEvent.tenant_id == row.tenant_id,
                SyntheticCalendarEvent.connection_id == row.connection_id,
                SyntheticCalendarEvent.idempotency_key == row.idempotency_key,
            )
        )
        if provider:
            from app.connectors.contracts import EventCreate
            from app.connectors.simulators import fingerprint

            if provider.request_hash != fingerprint(EventCreate.model_validate(row.payload)):
                return {"mismatch": True}
            return {
                "provider_reference": str(provider.id),
                "status": provider.status,
                "synthetic": True,
            }
    return None


def complete(db: Session, row: NotificationDelivery, result: dict) -> None:
    if not db.scalar(
        select(NotificationReceipt.id).where(NotificationReceipt.delivery_id == row.id)
    ):
        db.add(
            NotificationReceipt(
                tenant_id=row.tenant_id,
                delivery_id=row.id,
                provider_reference=result["provider_reference"],
                result=result,
            )
        )
    set_status(db, row, "SUCCEEDED")
    row.error_category = None
    calendar = db.scalar(
        select(CalendarCoordinationRequest).where(CalendarCoordinationRequest.delivery_id == row.id)
    )
    if calendar:
        calendar.event_id = uuid.UUID(result["provider_reference"])


def alert(db: Session, row: NotificationDelivery, category: str, source: str):
    from app.services.observability import emit

    emit(
        db,
        row.tenant_id,
        category,
        row.id,
        row.correlation_id,
        source,
        {
            "connection_id": str(row.connection_id),
            "failure_count": row.attempt_count,
            "error_category": row.error_category,
        },
    )


def failed(db: Session, row: NotificationDelivery, error) -> None:
    from app.workflows.engine import backoff

    record(db, "notification.delivery_failed", "delivery", row.id, row.tenant_id, row.principal_id)
    category = getattr(error.category, "value", error.category)
    row.error_category = category
    policy = RetryPolicy.model_validate(row.retry_policy)
    if isinstance(error, ReliabilityError) and error.retry_policy:
        policy = policy.model_copy(
            update={"max_attempts": min(policy.max_attempts, error.retry_policy.max_attempts)}
        )
    if category == "TIMEOUT":
        # Conservatively ambiguous; never automatically resend an unconfirmed timeout.
        receipt = provider_receipt(db, row)
        if receipt and not receipt.get("mismatch"):
            complete(db, row, receipt)
        else:
            set_status(db, row, "UNKNOWN")
            alert(db, row, "UNKNOWN_DELIVERY_OUTCOME", str(row.id) + ":unknown")
    elif error.retryable and row.attempt_count < policy.max_attempts:
        row.next_attempt_at = utcnow() + timedelta(
            seconds=backoff(policy, row.attempt_count, getattr(error, "retry_after", None))
        )
        if getattr(error, "defer_until", None):
            row.next_attempt_at = max(row.next_attempt_at, error.defer_until)
        set_status(db, row, "RETRY_SCHEDULED")
        if row.attempt_count >= 2:
            alert(db, row, "REPEATED_DELIVERY_FAILURE", str(row.id) + ":" + str(row.attempt_count))
    else:
        set_status(db, row, "FAILED")
        alert(db, row, "TERMINAL_NOTIFICATION_FAILURE", str(row.id) + ":failed")
        if category == "CONFLICT" and row.channel == "CALENDAR":
            alert(db, row, "CALENDAR_CONFLICT", str(row.id) + ":conflict")


def conflict_event(db: Session, row: NotificationDelivery) -> bool:
    if row.operation == "event.cancel":
        return False
    p = row.payload
    predicates = [
        SyntheticCalendarEvent.tenant_id == row.tenant_id,
        SyntheticCalendarEvent.connection_id == row.connection_id,
        SyntheticCalendarEvent.calendar == p["calendar"],
        SyntheticCalendarEvent.status == "BOOKED",
        SyntheticCalendarEvent.starts_at < datetime.fromisoformat(p["ends_at"]),
        SyntheticCalendarEvent.ends_at > datetime.fromisoformat(p["starts_at"]),
    ]
    if "id" in p:
        predicates.append(SyntheticCalendarEvent.id != uuid.UUID(p["id"]))
    return db.scalar(select(SyntheticCalendarEvent.id).where(*predicates).limit(1)) is not None


def owned(db: Session, delivery_id: uuid.UUID, token: uuid.UUID):
    tenant_id = db.scalar(
        select(NotificationDelivery.tenant_id).where(NotificationDelivery.id == delivery_id)
    )
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    row = db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.id == delivery_id).with_for_update()
    )
    if (
        not row
        or row.status != "RUNNING"
        or row.lease_token != token
        or row.lease_expires_at <= utcnow()
    ):
        return None
    return row


def finish_attempt(db: Session, row: NotificationDelivery):
    attempt = db.scalar(
        select(NotificationDeliveryAttempt).where(
            NotificationDeliveryAttempt.delivery_id == row.id,
            NotificationDeliveryAttempt.number == row.attempt_count,
        )
    )
    attempt.status = row.status
    attempt.error_category = row.error_category
    attempt.next_attempt_at = row.next_attempt_at if row.status == "RETRY_SCHEDULED" else None
    calendar = db.scalar(
        select(CalendarCoordinationAttempt).where(
            CalendarCoordinationAttempt.delivery_attempt_id == attempt.id
        )
    )
    if calendar:
        calendar.outcome = row.status


def perform(db: Session, delivery_id: uuid.UUID, token: uuid.UUID) -> bool:
    from app.workflows.engine import StepFailure

    row = owned(db, delivery_id, token)
    if row is None:
        return False
    db.execute(text("SELECT set_config('statement_timeout','10000',true)"))
    db.execute(text("SELECT set_config('lock_timeout','1000',true)"))
    user = db.get(User, row.principal_id)
    try:
        if not user or not user.is_active:
            raise StepFailure("AUTHORIZATION_FAILED")
        ctx = authorized_context(db, Identity(user, None), row.tenant_id)
        assert_permission(ctx, "integrations:manage")
        connection = integrations.get_connection(db, ctx, row.connection_id, lock=True)
        if connection.status != "ENABLED":
            raise StepFailure("AUTHORIZATION_FAILED")
        receipt = provider_receipt(db, row)
        if row.attempt_count > 5 and not receipt:
            row.error_category = "LEASE_EXHAUSTED"
            set_status(db, row, "UNKNOWN")
            alert(db, row, "UNKNOWN_DELIVERY_OUTCOME", str(row.id) + ":lease-exhausted")
        elif receipt and receipt.get("mismatch"):
            set_status(db, row, "UNKNOWN")
            row.error_category = "CONFLICT"
            alert(db, row, "UNKNOWN_DELIVERY_OUTCOME", str(row.id) + ":receipt-mismatch")
        elif receipt:
            complete(db, row, receipt)
        else:
            if row.channel == "CALENDAR":
                request = db.scalar(
                    select(CalendarCoordinationRequest).where(
                        CalendarCoordinationRequest.delivery_id == row.id
                    )
                )
                if request.action != "CREATE":
                    event = db.scalar(
                        select(SyntheticCalendarEvent).where(
                            SyntheticCalendarEvent.id == request.event_id,
                            SyntheticCalendarEvent.tenant_id == row.tenant_id,
                            SyntheticCalendarEvent.connection_id == row.connection_id,
                        )
                    )
                    if (
                        not event
                        or event.updated_at != request.expected_event_updated_at
                        or event.status == "CANCELLED"
                    ):
                        raise StepFailure("CONFLICT")
                if conflict_event(db, row):
                    raise StepFailure("CONFLICT")
            result = integrations.execute(
                db,
                ctx,
                row.connection_id,
                ConnectorOperation(name=OperationName(row.operation), payload=row.payload),
                correlation_id=row.correlation_id,
                attempt=row.attempt_count,
                deadline_seconds=10,
            )
            data = result.data.model_dump(mode="json")
            complete(
                db,
                row,
                {
                    "provider_reference": str(data["id"]),
                    "status": data["status"],
                    "synthetic": True,
                },
            )
        audit(
            db,
            ctx,
            "notification.action_completed"
            if row.status == "SUCCEEDED"
            else "notification.action_held",
            row,
        )
    except ReliabilityError as error:
        failed(db, row, error)
        attempt = db.scalar(
            select(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.delivery_id == row.id,
                NotificationDeliveryAttempt.number == row.attempt_count,
            )
        )
        attempt.retry_after = error.retry_after
    except (HTTPException, StepFailure) as error:
        failed(
            db,
            row,
            error if isinstance(error, StepFailure) else StepFailure("AUTHORIZATION_FAILED"),
        )
    finish_attempt(db, row)
    db.flush()
    return True


def run_claim(engine, value: tuple[uuid.UUID, uuid.UUID]) -> None:
    from app.workflows.engine import classify

    try:
        with Session(engine) as db, db.begin():
            perform(db, *value)
    except Exception as error:  # noqa: BLE001 - rollback effects then sanitize persisted failure
        with Session(engine) as db, db.begin():
            row = owned(db, *value)
            if row:
                failed(db, row, classify(error))
                finish_attempt(db, row)


def recover(db: Session, ctx: TenantContext, row: NotificationDelivery, action: str):
    lock(db, ctx, "executions:recover")
    db.refresh(row)
    if action == "cancel":
        if row.status not in {"PENDING", "RETRY_SCHEDULED", "UNKNOWN"}:
            raise HTTPException(409, "Only pending, retrying or unknown actions can be cancelled")
        set_status(db, row, "CANCELLED")
    elif action == "review":
        if row.status != "UNKNOWN":
            raise HTTPException(409, "Only unknown actions require receipt review")
        receipt = provider_receipt(db, row)
        if not receipt or receipt.get("mismatch"):
            audit(db, ctx, "notification.unknown_reviewed", row)
            return {
                "delivery": public(row),
                "resolved": False,
                "message": "No verified provider receipt; resend remains blocked",
            }
        complete(db, row, receipt)
    elif action == "retry":
        if row.status != "RETRY_SCHEDULED":
            raise HTTPException(
                409, "Only scheduled retries are eligible; failed/unknown actions require review"
            )
        # Keep attempt budget and Retry-After. Operators cannot accelerate provider backoff.
        row.next_attempt_at = max(row.next_attempt_at, utcnow())
    audit(db, ctx, "notification." + action, row)
    return {"delivery": public(row), "resolved": row.status == "SUCCEEDED"}


def calendar_event(db: Session, ctx: TenantContext, event_id: uuid.UUID):
    row = db.scalar(
        select(SyntheticCalendarEvent).where(
            SyntheticCalendarEvent.id == event_id, SyntheticCalendarEvent.tenant_id == ctx.tenant.id
        )
    )
    if not row:
        raise HTTPException(404, "Calendar event unavailable")
    return row


def calendar_queue(
    db: Session,
    ctx: TenantContext,
    data: CalendarCreate | CalendarChange | CalendarCancel,
    correlation: uuid.UUID,
    event_id: uuid.UUID | None = None,
):
    lock(db, ctx)
    action = (
        "CREATE"
        if isinstance(data, CalendarCreate)
        else "RESCHEDULE"
        if isinstance(data, CalendarChange)
        else "CANCEL"
    )
    fingerprint = digest(
        {"action": action, "event_id": event_id, "data": data.model_dump(mode="json")}
    )
    prior = db.scalar(
        select(CalendarCoordinationRequest).where(
            CalendarCoordinationRequest.tenant_id == ctx.tenant.id,
            CalendarCoordinationRequest.request_key == data.request_key,
        )
    )
    if prior:
        if prior.request_digest != fingerprint:
            raise HTTPException(409, "Calendar request key reused with different input")
        return prior
    event = calendar_event(db, ctx, event_id) if event_id else None
    connection_id = data.connection_id if isinstance(data, CalendarCreate) else event.connection_id
    connection = scoped(db, ctx, IntegrationConnection, connection_id)
    if connection.connector_type != "synthetic_calendar":
        raise HTTPException(422, "Use a synthetic calendar connection")
    if event and (event.updated_at != data.expected_updated_at or event.status == "CANCELLED"):
        raise HTTPException(409, "Event changed; inspect its current revision")
    if action != "CANCEL" and data.calendar not in connection.public_config.get(
        "allowed_calendars", []
    ):
        raise HTTPException(422, "Calendar is not allowlisted")
    key = digest(["calendar", str(ctx.tenant.id), str(data.request_key)])
    payload = (
        {"id": str(event.id)}
        if action == "CANCEL"
        else data.model_dump(mode="json", include={"calendar", "title", "starts_at", "ends_at"})
    )
    if action == "CREATE":
        payload["idempotency_key"] = key
    elif action == "RESCHEDULE":
        payload["id"] = str(event.id)
    delivery = create_delivery(
        db,
        ctx,
        connection_id,
        "CALENDAR",
        {"CREATE": "event.create", "RESCHEDULE": "event.update", "CANCEL": "event.cancel"}[action],
        payload,
        key,
        correlation,
    )
    request = CalendarCoordinationRequest(
        tenant_id=ctx.tenant.id,
        delivery_id=delivery.id,
        connection_id=connection_id,
        event_id=event_id,
        action=action,
        participants=data.participants if action != "CANCEL" else [],
        expected_event_updated_at=event.updated_at if event else None,
        request_key=data.request_key,
        request_digest=fingerprint,
    )
    db.add(request)
    db.flush()
    audit(db, ctx, "calendar.action_queued", request)
    return request


def availability(
    db: Session,
    ctx: TenantContext,
    connection_id: uuid.UUID,
    calendar: str,
    starts_at: datetime,
    ends_at: datetime,
):
    connection = scoped(db, ctx, IntegrationConnection, connection_id)
    if (
        connection.connector_type != "synthetic_calendar"
        or calendar not in connection.public_config.get("allowed_calendars", [])
        or ends_at <= starts_at
    ):
        raise HTTPException(422, "Invalid calendar interval/configuration")
    matches = list(
        db.scalars(
            select(SyntheticCalendarEvent)
            .where(
                SyntheticCalendarEvent.tenant_id == ctx.tenant.id,
                SyntheticCalendarEvent.connection_id == connection_id,
                SyntheticCalendarEvent.calendar == calendar,
                SyntheticCalendarEvent.status == "BOOKED",
                SyntheticCalendarEvent.starts_at < ends_at,
                SyntheticCalendarEvent.ends_at > starts_at,
            )
            .order_by(SyntheticCalendarEvent.starts_at)
            .limit(100)
        )
    )
    return {
        "available": not matches,
        "conflicts": [public(r) for r in matches],
        "bounded": True,
        "synthetic": True,
    }
