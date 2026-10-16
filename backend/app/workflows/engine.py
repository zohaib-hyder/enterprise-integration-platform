"""PostgreSQL dispatch and fenced, bounded local step execution."""

import hashlib
import json
import random
import time
import uuid
from datetime import timedelta

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Engine, and_, exists, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.connectors.contracts import (
    ConnectorError,
    ConnectorOperation,
    OperationName,
)
from app.connectors.registry import registry
from app.connectors.reliability import ReliabilityError
from app.core.security import utcnow
from app.models.tenant import Tenant
from app.models.webhook import IncomingEvent
from app.models.workflow import (
    DeadLetterRecord,
    RetrySchedule,
    WorkerLease,
    WorkflowActionReceipt,
    WorkflowDefinition,
    WorkflowDispatchRecord,
    WorkflowExecution,
    WorkflowStepExecution,
    WorkflowTrigger,
    WorkflowVersion,
)
from app.schemas.workflow import CHECKPOINT_MODELS, RetryPolicy, WorkflowJSON, WorkflowStep
from app.services import canonical, integrations
from app.services.workflows import (
    cancel_now,
    clear_lease,
    history,
    principal_context,
    steps,
    transition,
)


class StepFailure(Exception):
    def __init__(self, category: str, retryable=False, retry_after=None):
        self.category, self.retryable, self.retry_after = category, retryable, retry_after
        super().__init__(category)


def queue_tenants(db: Session, model, query) -> list[uuid.UUID]:
    """Admit bounded tenants before queue locks; match execution/recovery lock order.

    Skip busy tenants instead of holding a queue row while waiting on its FK parent.
    Multiple admitted tenants always use UUID order to prevent inter-tenant cycles.
    """
    candidates = list(
        db.scalars(
            query.with_only_columns(model.tenant_id)
            .order_by(None)
            .distinct()
            .order_by(model.tenant_id)
            .limit(32)
        )
    )
    return [
        identifier
        for identifier in candidates
        if db.scalar(
            select(Tenant.id).where(Tenant.id == identifier).with_for_update(skip_locked=True)
        )
        is not None
    ]


def dispatch(db: Session, tenant_id=None, limit=25, rescan=False) -> int:
    receipt = exists(
        select(WorkflowDispatchRecord.id).where(WorkflowDispatchRecord.event_id == IncomingEvent.id)
    )
    eligible = ~receipt
    if rescan:
        # Only reconsider NO_MATCH rows with a currently possible match; old unmatched
        # rows must not monopolize every bounded manual rescan batch.
        matching = exists(
            select(WorkflowTrigger.id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowTrigger.version_id)
            .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowVersion.workflow_id)
            .where(
                WorkflowDefinition.tenant_id == IncomingEvent.tenant_id,
                WorkflowDefinition.status == "ACTIVE",
                WorkflowDefinition.active_version == WorkflowVersion.version,
                WorkflowTrigger.connection_id == IncomingEvent.connection_id,
                WorkflowTrigger.source == IncomingEvent.source,
                WorkflowTrigger.event_types.op("?")(IncomingEvent.event_type),
                or_(
                    WorkflowTrigger.event_id_prefix.is_(None),
                    func.left(
                        IncomingEvent.provider_event_id,
                        func.length(WorkflowTrigger.event_id_prefix),
                    )
                    == WorkflowTrigger.event_id_prefix,
                ),
            )
        )
        eligible = or_(
            eligible,
            exists(
                select(WorkflowDispatchRecord.id).where(
                    WorkflowDispatchRecord.event_id == IncomingEvent.id,
                    WorkflowDispatchRecord.state == "NO_MATCH",
                    matching,
                )
            ),
        )
    q = select(IncomingEvent).where(IncomingEvent.ingestion_status == "ACCEPTED", eligible)
    if tenant_id:
        q = q.where(IncomingEvent.tenant_id == tenant_id)
    admitted = queue_tenants(db, IncomingEvent, q)
    if not admitted:
        return 0
    q = q.where(IncomingEvent.tenant_id.in_(admitted))
    events = list(
        db.scalars(
            q.order_by(IncomingEvent.received_at, IncomingEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=IncomingEvent)
        )
    )
    count = 0
    for event in events:
        # Recheck after locking; another dispatcher may have committed since our snapshot.
        previous = db.scalar(
            select(WorkflowDispatchRecord).where(WorkflowDispatchRecord.event_id == event.id)
        )
        if previous and (not rescan or previous.state != "NO_MATCH"):
            continue
        matches = db.execute(
            select(WorkflowDefinition, WorkflowVersion, WorkflowTrigger)
            .join(
                WorkflowVersion,
                and_(
                    WorkflowVersion.workflow_id == WorkflowDefinition.id,
                    WorkflowVersion.version == WorkflowDefinition.active_version,
                ),
            )
            .join(WorkflowTrigger, WorkflowTrigger.version_id == WorkflowVersion.id)
            .where(
                WorkflowDefinition.tenant_id == event.tenant_id,
                WorkflowDefinition.status == "ACTIVE",
                WorkflowTrigger.connection_id == event.connection_id,
                WorkflowTrigger.source == event.source,
            )
        ).all()
        created = 0
        for workflow, version, trigger in matches:
            if event.event_type not in trigger.event_types or (
                trigger.event_id_prefix
                and not event.provider_event_id.startswith(trigger.event_id_prefix)
            ):
                continue
            plan = WorkflowJSON.model_validate(version.definition)
            row = WorkflowExecution(
                id=uuid.uuid4(),
                tenant_id=event.tenant_id,
                workflow_id=workflow.id,
                version_id=version.id,
                event_id=event.id,
                connection_id=event.connection_id,
                principal_id=workflow.created_by,
                correlation_id=event.correlation_id,
                next_run_at=utcnow(),
            )
            row.root_id = row.id
            db.add(row)
            db.flush()
            for pos, step in enumerate(plan.steps):
                db.add(
                    WorkflowStepExecution(
                        tenant_id=event.tenant_id,
                        execution_id=row.id,
                        step_id=step.id,
                        position=pos,
                        step_type=step.type,
                    )
                )
            history(db, row, "DISPATCHED")
            created += 1
        state = "DISPATCHED" if created else "NO_MATCH"
        if previous:
            previous.state, previous.execution_count = state, created
        else:
            db.add(
                WorkflowDispatchRecord(
                    tenant_id=event.tenant_id,
                    event_id=event.id,
                    connection_id=event.connection_id,
                    state=state,
                    execution_count=created,
                )
            )
        count += created
    db.flush()
    return count


def register_worker(db: Session) -> uuid.UUID:
    row = WorkerLease(identity=f"local-worker:{uuid.uuid4()}", heartbeat_at=utcnow())
    db.add(row)
    db.flush()
    return row.id


def heartbeat(db: Session, worker_id: uuid.UUID) -> None:
    worker = db.get(WorkerLease, worker_id)
    if worker is None or worker.stopped_at is not None:
        raise ValueError("Worker unavailable")
    worker.heartbeat_at = utcnow()


def claim(
    db: Session, worker_id: uuid.UUID, lease_seconds: int = 30
) -> tuple[uuid.UUID, uuid.UUID] | None:
    if not 15 <= lease_seconds <= 120:
        raise ValueError("Lease must cover the bounded local step timeout")
    heartbeat(db, worker_id)
    now = utcnow()
    due = or_(
        and_(
            WorkflowExecution.state.in_(["PENDING", "WAITING", "RETRY_SCHEDULED"]),
            WorkflowExecution.next_run_at <= now,
        ),
        and_(WorkflowExecution.state == "RUNNING", WorkflowExecution.lease_expires_at <= now),
    )
    q = select(WorkflowExecution).where(due)
    admitted = queue_tenants(db, WorkflowExecution, q)
    if not admitted:
        return None
    row = db.scalar(
        q.where(WorkflowExecution.tenant_id.in_(admitted))
        .order_by(WorkflowExecution.next_run_at, WorkflowExecution.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    if row.cancel_requested:
        cancel_now(db, row)
        return None
    if row.state == "RUNNING":
        recovery = history(db, row, "LEASE_RECOVERED")
        from app.services.observability import emit

        db.flush()
        emit(
            db,
            row.tenant_id,
            "WORKER_RECOVERY",
            row.workflow_id,
            row.correlation_id,
            str(recovery.id),
            {"workflow_id": row.workflow_id, "execution_id": row.id},
        )
        current = steps(db, row)[row.cursor]
        if current.state == "RUNNING":
            transition(current, "PENDING")
    else:
        transition(row, "RUNNING")
    plan = WorkflowJSON.model_validate(db.get(WorkflowVersion, row.version_id).definition)
    current = steps(db, row)[row.cursor]
    if current.state not in {"PENDING", "RETRY_SCHEDULED"}:
        raise ValueError("Checkpoint cursor invalid")
    if current.attempt_count >= plan.steps[row.cursor].retry.max_attempts:
        transition(current, "RUNNING")
        fail(db, row, current, plan.steps[row.cursor], StepFailure("LEASE_EXHAUSTED"))
        return None
    for schedule in db.scalars(
        select(RetrySchedule).where(
            RetrySchedule.step_execution_id == current.id, RetrySchedule.state == "SCHEDULED"
        )
    ):
        schedule.state = "CONSUMED"
    transition(current, "RUNNING")
    current.attempt_count += 1
    current.started_at = current.started_at or now
    current.error_category = None
    row.started_at = row.started_at or now
    row.lease_worker_id, row.lease_token = worker_id, uuid.uuid4()
    row.heartbeat_at, row.lease_expires_at = now, now + timedelta(seconds=lease_seconds)
    history(db, row, "STEP_STARTED", current.step_id)
    db.flush()
    return row.id, row.lease_token


def owned(db: Session, execution_id: uuid.UUID, token: uuid.UUID) -> WorkflowExecution | None:
    # Lock tenant first, matching management/cancel/replay and canonical locking order.
    tenant_id = db.scalar(
        select(WorkflowExecution.tenant_id).where(WorkflowExecution.id == execution_id)
    )
    if tenant_id is None:
        return None
    db.execute(text("SELECT set_config('lock_timeout', '1000', true)"))
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    row = db.scalar(
        select(WorkflowExecution).where(WorkflowExecution.id == execution_id).with_for_update()
    )
    if row.state != "RUNNING" or row.lease_token != token or row.lease_expires_at <= utcnow():
        return None
    return row


def backoff(policy: RetryPolicy, attempt: int, retry_after=None, random_value=None) -> float:
    base = min(policy.max_delay_seconds, policy.base_delay_seconds * 2 ** max(0, attempt - 1))
    jitter = random.random() if random_value is None else random_value
    delay = base * (1 + policy.jitter_fraction * jitter)
    return min(policy.max_delay_seconds, max(delay, retry_after or 0))


def fail(
    db: Session, row: WorkflowExecution, current: WorkflowStepExecution, spec, error: StepFailure
) -> None:
    category = error.category.value if isinstance(error, ReliabilityError) else error.category
    current.error_category = row.error_category = category
    policy = spec.retry
    if isinstance(error, ReliabilityError) and error.retry_policy is not None:
        policy = error.retry_policy.model_copy(
            update={"max_attempts": min(spec.retry.max_attempts, error.retry_policy.max_attempts)}
        )
    history(db, row, "STEP_FAILED", current.step_id, category)
    if error.retryable and current.attempt_count < policy.max_attempts:
        due = utcnow() + timedelta(
            seconds=backoff(policy, current.attempt_count, error.retry_after)
        )
        if isinstance(error, ReliabilityError) and error.defer_until is not None:
            due = max(due, error.defer_until)
        transition(current, "RETRY_SCHEDULED")
        transition(row, "RETRY_SCHEDULED")
        row.next_run_at = due
        db.add(
            RetrySchedule(
                tenant_id=row.tenant_id,
                execution_id=row.id,
                step_execution_id=current.id,
                attempt=current.attempt_count,
                available_at=due,
                category=category,
            )
        )
        history(db, row, "RETRY_SCHEDULED", current.step_id, category)
    else:
        transition(current, "FAILED")
        transition(row, "DEAD_LETTERED" if spec.failure_behavior == "DEAD_LETTER" else "FAILED")
        if row.state == "DEAD_LETTERED":
            db.add(
                DeadLetterRecord(
                    tenant_id=row.tenant_id,
                    execution_id=row.id,
                    event_id=row.event_id,
                    connection_id=row.connection_id,
                    step_id=current.step_id,
                    category=category,
                    attempt_count=current.attempt_count,
                )
            )
        history(db, row, row.state, current.step_id, category)
        from app.services.notifications import emit_failure

        emit_failure(db, row)
        if row.state == "DEAD_LETTERED":
            from app.services.observability import emit

            emit(
                db,
                row.tenant_id,
                "WORKFLOW_DEAD_LETTERED",
                row.workflow_id,
                row.correlation_id,
                str(row.id),
                {
                    "workflow_id": row.workflow_id,
                    "execution_id": row.id,
                    "event_id": row.event_id,
                    "error_category": category,
                },
            )
    clear_lease(row)


def reference(db: Session, row: WorkflowExecution, event: IncomingEvent, ref):
    value = (
        event.payload
        if ref.source == "event"
        else next(
            (
                s.output
                for s in steps(db, row)
                if s.step_id == ref.step_id and s.state == "SUCCEEDED"
            ),
            None,
        )
    )
    for key in ref.path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def normalize_step(
    db: Session,
    row: WorkflowExecution,
    current: WorkflowStepExecution,
    spec: WorkflowStep,
    context: TenantContext,
    event: IncomingEvent,
) -> dict:
    config = spec.typed_config()
    mapping, source = canonical.event_input(db, context, event.id, config.mapping_version_id)
    if spec.type == "TRANSFORM":
        version = canonical.resolve_version(db, context, mapping, config.mapping_version_id)
        result = canonical.evaluate(db, context, mapping, version, source, row.correlation_id)
        output = result["output"]
        outcome = result["outcome"]
        attempt_id = None
    else:
        attempt = canonical.normalize(db, context, mapping.id, source, row.correlation_id, event.id)
        output, outcome, attempt_id = attempt.result_snapshot, attempt.outcome, str(attempt.id)
    if outcome not in {"SUCCEEDED", "NO_CHANGE"} or output is None:
        raise StepFailure("TRANSFORMATION_REVIEW")
    return {**output, "transformation_attempt_id": attempt_id}


def condition_step(
    db: Session,
    row: WorkflowExecution,
    current: WorkflowStepExecution,
    spec: WorkflowStep,
    context: TenantContext,
    event: IncomingEvent,
) -> dict:
    config = spec.typed_config()
    value = reference(db, row, event, config.reference)
    matched = (
        value is not None
        if config.operator == "EXISTS"
        else value == config.expected
        if config.operator == "EQ"
        else value != config.expected
    )
    if not matched and config.on_false == "SKIP_REMAINING":
        for other in steps(db, row)[row.cursor + 1 :]:
            transition(other, "SKIPPED")
    return {"matched": matched}


def delay_step(
    db: Session,
    row: WorkflowExecution,
    current: WorkflowStepExecution,
    spec: WorkflowStep,
    context: TenantContext,
    event: IncomingEvent,
) -> dict:
    row.next_run_at = utcnow() + timedelta(seconds=spec.typed_config().seconds)
    return {"due_at": row.next_run_at.isoformat()}


def action_step(
    db: Session,
    row: WorkflowExecution,
    current: WorkflowStepExecution,
    spec: WorkflowStep,
    context: TenantContext,
    event: IncomingEvent,
) -> dict:
    config = spec.typed_config()
    payload = dict(config.payload)
    for key, ref in config.bindings.items():
        payload[key] = reference(db, row, event, ref)
    conn = integrations.get_connection(db, context, config.connection_id)
    model = (
        registry.resolve(conn.connector_type)
        .supported_operations()
        .get(OperationName(config.operation))
    )
    if model is None:
        raise StepFailure("UNSUPPORTED_OPERATION")
    if "idempotency_key" in model.model_fields:
        payload["idempotency_key"] = f"workflow:{row.root_id.hex}:{spec.id}"
    payload = model.model_validate(payload).model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(
            {"connection": str(conn.id), "operation": config.operation, "payload": payload},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    receipt = db.scalar(
        select(WorkflowActionReceipt).where(
            WorkflowActionReceipt.tenant_id == row.tenant_id,
            WorkflowActionReceipt.root_execution_id == row.root_id,
            WorkflowActionReceipt.step_id == spec.id,
        )
    )
    if receipt:
        if receipt.state != "APPLIED" or receipt.request_digest != digest:
            raise StepFailure("UNCERTAIN_ACTION")
        return receipt.result
    count = (
        db.scalar(
            select(func.sum(WorkflowStepExecution.attempt_count))
            .join(WorkflowExecution, WorkflowExecution.id == WorkflowStepExecution.execution_id)
            .where(
                WorkflowExecution.root_id == row.root_id, WorkflowStepExecution.step_id == spec.id
            )
        )
        or 0
    )
    fault = config.fault
    if fault.mode == "PERMANENT" or event.provider_event_id in fault.permanent_event_ids:
        raise StepFailure("VALIDATION_FAILED")
    if count <= fault.fail_first_attempts and fault.mode in {"TRANSIENT", "TIMEOUT"}:
        raise StepFailure(
            "TIMEOUT" if fault.mode == "TIMEOUT" else "TEMPORARY_UNAVAILABLE",
            True,
            fault.retry_after_seconds,
        )
    result = integrations.execute(
        db,
        context,
        conn.id,
        ConnectorOperation(name=config.operation, payload=payload),
        propagate_database_errors=True,
        correlation_id=row.correlation_id,
        workflow_execution_id=row.id,
        attempt=current.attempt_count,
        deadline_seconds=spec.timeout_seconds,
    ).model_dump(mode="json")
    db.add(
        WorkflowActionReceipt(
            tenant_id=row.tenant_id,
            root_execution_id=row.root_id,
            connection_id=conn.id,
            step_id=spec.id,
            request_digest=digest,
            result=result,
        )
    )
    return result


def result_step(
    db: Session,
    row: WorkflowExecution,
    current: WorkflowStepExecution,
    spec: WorkflowStep,
    context: TenantContext,
    event: IncomingEvent,
) -> dict:
    config = spec.typed_config()
    return {
        "summary": config.summary,
        "outcome": config.outcome,
        "canonical_id": reference(db, row, event, config.canonical_id)
        if config.canonical_id
        else None,
    }


def sync_step(
    db: Session,
    row: WorkflowExecution,
    current: WorkflowStepExecution,
    spec: WorkflowStep,
    context: TenantContext,
    event: IncomingEvent,
) -> dict:
    from app.models.synchronization import (
        SyncConfiguration,
        SyncConfigurationVersion,
        SyncExecution,
        SyncPlan,
    )
    from app.schemas.synchronization import PlanPreview
    from app.services import synchronization as sync

    config = spec.typed_config()
    if spec.type == "PLAN_CRM_ATS_SYNC":
        version = sync.scoped(
            db, context, SyncConfigurationVersion, config.configuration_version_id
        )
        cfg = sync.scoped(db, context, SyncConfiguration, version.configuration_id)
        plan, execution = sync.plan(
            db,
            context,
            cfg,
            version,
            PlanPreview(event_id=event.id),
            row.correlation_id,
            normalize=True,
            workflow_execution_id=row.id,
        )
    else:
        preceding = next(s for s in steps(db, row) if s.step_id == config.plan_step_id)
        plan = sync.scoped(db, context, SyncPlan, uuid.UUID(preceding.output["plan_id"]))
        execution = sync.scoped(
            db, context, SyncExecution, uuid.UUID(preceding.output["execution_id"])
        )
        if spec.type == "APPLY_CRM_ATS_SYNC":
            sync.apply(
                db,
                context,
                plan,
                execution,
                attempt=current.attempt_count,
                deadline=spec.timeout_seconds,
            )
    return sync.checkpoint(plan, execution)


EXECUTORS = {
    "PLAN_CRM_ATS_SYNC": sync_step,
    "APPLY_CRM_ATS_SYNC": sync_step,
    "RECORD_SYNC_OUTCOME": sync_step,
    "NORMALIZE_EVENT": normalize_step,
    "TRANSFORM": normalize_step,
    "CONDITION": condition_step,
    "DELAY": delay_step,
    "MOCK_CONNECTOR_ACTION": action_step,
    "RECORD_RESULT": result_step,
}


def perform(db: Session, execution_id: uuid.UUID, token: uuid.UUID) -> bool:
    row = owned(db, execution_id, token)
    if row is None:
        return False
    if row.cancel_requested:
        cancel_now(db, row)
        return True
    plan = WorkflowJSON.model_validate(db.get(WorkflowVersion, row.version_id).definition)
    current = steps(db, row)[row.cursor]
    spec = plan.steps[row.cursor]
    db.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": str(spec.timeout_seconds * 1000)},
    )
    db.execute(text("SELECT set_config('lock_timeout', '1000', true)"))
    context = principal_context(db, row)
    event = db.scalar(
        select(IncomingEvent).where(
            IncomingEvent.id == row.event_id,
            IncomingEvent.tenant_id == row.tenant_id,
            IncomingEvent.connection_id == row.connection_id,
        )
    )
    if event is None:
        raise StepFailure("NOT_FOUND")
    started = time.monotonic()
    try:
        output = EXECUTORS[spec.type](db, row, current, spec, context, event)
    except ReliabilityError as error:
        # Local effects were rolled back in the connector savepoint. Commit telemetry,
        # quotas, breaker/refresh outcomes and durable retry together under the same fence.
        if row.lease_expires_at <= utcnow():
            raise StepFailure("TIMEOUT", True) from None
        fail(db, row, current, spec, error)
        return True
    if time.monotonic() - started > spec.timeout_seconds or row.lease_expires_at <= utcnow():
        raise StepFailure("TIMEOUT", True)
    current.output = CHECKPOINT_MODELS[spec.type].model_validate(output).model_dump(mode="json")
    transition(current, "SUCCEEDED")
    history(db, row, "STEP_SUCCEEDED", current.step_id)
    row.cursor += 1
    following = steps(db, row)
    while row.cursor < len(following) and following[row.cursor].state in {"SUCCEEDED", "SKIPPED"}:
        row.cursor += 1
    row.error_category = None
    if row.cursor == len(following):
        transition(row, "SUCCEEDED")
        history(db, row, "SUCCEEDED")
    elif spec.type == "DELAY":
        transition(row, "WAITING")
        history(db, row, "WAITING", current.step_id)
    else:
        transition(row, "PENDING")
        row.next_run_at = utcnow()
    heartbeat(db, row.lease_worker_id)
    clear_lease(row)
    db.flush()
    return True


def classify(error: Exception) -> StepFailure:
    if isinstance(error, StepFailure):
        return error
    if isinstance(error, ConnectorError):
        return StepFailure(
            error.category.value, error.retryable, getattr(error, "retry_after", None)
        )
    if isinstance(error, HTTPException):
        return StepFailure(
            "AUTHORIZATION_FAILED" if error.status_code in {401, 403} else "VALIDATION_FAILED"
        )
    if isinstance(error, ValidationError):
        return StepFailure("VALIDATION_FAILED")
    if isinstance(error, DBAPIError):
        code = getattr(error.orig, "sqlstate", None)
        return StepFailure(
            "TIMEOUT" if code in {"57014", "55P03"} else "DATABASE_UNAVAILABLE", True
        )
    return StepFailure("INTERNAL_ERROR")


def run_claim(engine: Engine, claim_value: tuple[uuid.UUID, uuid.UUID]) -> None:
    execution_id, token = claim_value
    try:
        with Session(engine) as db, db.begin():
            perform(db, execution_id, token)
    except Exception as error:  # noqa: BLE001 - rollback and classify without exposing raw errors
        failure = classify(error)
        with Session(engine) as db, db.begin():
            row = owned(db, execution_id, token)
            if row is None:
                return
            current = steps(db, row)[row.cursor]
            plan = WorkflowJSON.model_validate(db.get(WorkflowVersion, row.version_id).definition)
            fail(db, row, current, plan.steps[row.cursor], failure)
