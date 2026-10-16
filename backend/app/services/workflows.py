"""Tenant-authorized immutable plans and explicit operator recovery."""

import hashlib
import json
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import Identity, TenantContext, assert_permission, authorized_context
from app.connectors.contracts import OperationName
from app.connectors.registry import registry
from app.core.security import utcnow
from app.models.canonical import TransformationMapping, TransformationMappingVersion
from app.models.identity import User
from app.models.integration import IntegrationConnection
from app.models.tenant import Tenant
from app.models.workflow import (
    RetrySchedule,
    WorkflowActionReceipt,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionEvent,
    WorkflowStepExecution,
    WorkflowTrigger,
    WorkflowVersion,
)
from app.schemas.workflow import ActionConfig, NormalizeConfig, WorkflowCreate, WorkflowJSON
from app.services.audit import record
from app.services.webhooks import manage_lock

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"}
TRANSITIONS = {
    "PENDING": {"RUNNING", "CANCELLED"},
    "RUNNING": {
        "PENDING",
        "WAITING",
        "RETRY_SCHEDULED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "DEAD_LETTERED",
    },
    "WAITING": {"RUNNING", "CANCELLED"},
    "RETRY_SCHEDULED": {"RUNNING", "CANCELLED"},
}
STEP_TRANSITIONS = {
    "PENDING": {"RUNNING", "CANCELLED", "SKIPPED"},
    "RUNNING": {"SUCCEEDED", "FAILED", "RETRY_SCHEDULED", "CANCELLED", "PENDING"},
    "RETRY_SCHEDULED": {"RUNNING", "CANCELLED"},
}


def transition(row, state: str) -> None:
    allowed = STEP_TRANSITIONS if isinstance(row, WorkflowStepExecution) else TRANSITIONS
    if state not in allowed.get(row.state, set()):
        raise ValueError("Illegal workflow state transition")
    row.state = state
    if state in TERMINAL or isinstance(row, WorkflowStepExecution) and state in {"SKIPPED"}:
        row.finished_at = utcnow()


def history(
    db: Session, execution: WorkflowExecution, event: str, step_id=None, category=None
) -> WorkflowExecutionEvent:
    row = WorkflowExecutionEvent(
        tenant_id=execution.tenant_id,
        execution_id=execution.id,
        event_type=event,
        step_id=step_id,
        category=category,
        details={},
    )
    db.add(row)
    return row


def clear_lease(row: WorkflowExecution) -> None:
    row.lease_worker_id = row.lease_token = row.lease_expires_at = None


def principal_context(db: Session, execution: WorkflowExecution) -> TenantContext:
    user = db.get(User, execution.principal_id)
    if user is None or not user.is_active:
        raise HTTPException(403, "Workflow principal unavailable")
    context = authorized_context(db, Identity(user, None), execution.tenant_id)
    assert_permission(context, "integrations:manage")
    return context


def recover_lock(db: Session, context: TenantContext) -> None:
    db.scalar(select(Tenant).where(Tenant.id == context.tenant.id).with_for_update())
    db.refresh(context.membership)
    assert_permission(
        authorized_context(db, context.identity, context.tenant.id), "executions:recover"
    )


def get_workflow(db: Session, context: TenantContext, workflow_id: uuid.UUID) -> WorkflowDefinition:
    row = db.scalar(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == workflow_id, WorkflowDefinition.tenant_id == context.tenant.id
        )
    )
    if row is None:
        raise HTTPException(404, "Workflow unavailable")
    return row


def get_execution(
    db: Session, context: TenantContext, execution_id: uuid.UUID, lock=False
) -> WorkflowExecution:
    q = select(WorkflowExecution).where(
        WorkflowExecution.id == execution_id, WorkflowExecution.tenant_id == context.tenant.id
    )
    row = db.scalar(q.with_for_update() if lock else q)
    if row is None:
        raise HTTPException(404, "Execution unavailable")
    return row


def validate_definition(db: Session, context: TenantContext, plan: WorkflowJSON) -> None:
    conn = db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.id == plan.trigger.connection_id,
            IntegrationConnection.tenant_id == context.tenant.id,
        )
    )
    if conn is None or conn.connector_type != plan.trigger.source or conn.status != "ENABLED":
        raise HTTPException(422, "Trigger connection unavailable")
    if len({e.split(".")[0] for e in plan.trigger.event_types}) != 1:
        raise HTTPException(422, "Trigger must use one entity family")
    if plan.trigger.source == "synthetic_crm" and any(
        e.startswith("candidate.") for e in plan.trigger.event_types
    ):
        raise HTTPException(422, "CRM candidate triggers are unsupported")
    for step in plan.steps:
        config = step.typed_config()
        from app.schemas.synchronization import SyncStepConfig

        if isinstance(config, SyncStepConfig):
            from app.models.synchronization import SyncConfiguration, SyncConfigurationVersion
            from app.services import synchronization

            version = synchronization.scoped(
                db, context, SyncConfigurationVersion, config.configuration_version_id
            )
            cfg = synchronization.scoped(db, context, SyncConfiguration, version.configuration_id)
            if (
                cfg.source_connection_id != conn.id
                or plan.trigger.source != "synthetic_crm"
                or set(plan.trigger.event_types) != set(version.definition["trigger_event_types"])
            ):
                raise HTTPException(422, "Synchronization version does not match trigger")
        elif isinstance(config, NormalizeConfig):
            version = db.scalar(
                select(TransformationMappingVersion).where(
                    TransformationMappingVersion.id == config.mapping_version_id,
                    TransformationMappingVersion.tenant_id == context.tenant.id,
                )
            )
            mapping = db.get(TransformationMapping, version.mapping_id) if version else None
            expected = (
                "requisition"
                if all(e.startswith("job.") for e in plan.trigger.event_types)
                else "candidate"
            )
            if (
                mapping is None
                or mapping.connection_id != conn.id
                or mapping.source_kind != expected
            ):
                raise HTTPException(422, "Mapping does not match workflow trigger")
        elif isinstance(config, ActionConfig):
            target = db.scalar(
                select(IntegrationConnection).where(
                    IntegrationConnection.id == config.connection_id,
                    IntegrationConnection.tenant_id == context.tenant.id,
                )
            )
            if (
                target is None
                or target.status != "ENABLED"
                or target.connector_type == "generic_rest"
            ):
                raise HTTPException(422, "Action connection unavailable")
            model = (
                registry.resolve(target.connector_type)
                .supported_operations()
                .get(OperationName(config.operation))
            )
            if model is None or not set(config.payload | config.bindings) <= set(
                model.model_fields
            ):
                raise HTTPException(422, "Unsupported action input")
            if (
                set(model.model_fields)
                - set(config.payload | config.bindings)
                - {"idempotency_key"}
            ):
                required = {k for k, v in model.model_fields.items() if v.is_required()}
                if required - set(config.payload | config.bindings) - {"idempotency_key"}:
                    raise HTTPException(422, "Missing action input")
            # Validate literals with connector validators too. Missing bound fields
            # are intentional; their actual values are validated again at execution.
            literal_payload = {k: v for k, v in config.payload.items() if k not in config.bindings}
            if "idempotency_key" in model.model_fields:
                literal_payload["idempotency_key"] = "workflow:validation"
            try:
                model.model_validate(literal_payload)
            except ValidationError as error:
                if any(
                    item["type"] != "missing"
                    or not item["loc"]
                    or item["loc"][0] not in config.bindings
                    for item in error.errors(include_input=False, include_context=False)
                ):
                    raise HTTPException(422, "Invalid literal action input") from None
            if not config.bindings:
                payload = dict(config.payload)
                if "idempotency_key" in model.model_fields:
                    payload["idempotency_key"] = "workflow:validation"
                try:
                    model.model_validate(payload)
                except ValidationError:
                    raise HTTPException(422, "Invalid action input") from None


def version_output(row: WorkflowVersion) -> dict:
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "workflow_id",
            "version",
            "schema_version",
            "definition",
            "definition_digest",
            "created_at",
        )
    }


def workflow_output(row: WorkflowDefinition) -> dict:
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "name",
            "description",
            "status",
            "active_version",
            "trigger_configuration",
            "created_at",
            "updated_at",
        )
    }


def add_version(
    db: Session, context: TenantContext, row: WorkflowDefinition, plan: WorkflowJSON
) -> WorkflowVersion:
    validate_definition(db, context, plan)
    n = (
        db.scalar(
            select(func.max(WorkflowVersion.version)).where(WorkflowVersion.workflow_id == row.id)
        )
        or 0
    ) + 1
    definition = plan.model_dump(mode="json")
    version = WorkflowVersion(
        tenant_id=row.tenant_id,
        workflow_id=row.id,
        version=n,
        schema_version=1,
        definition=definition,
        definition_digest=hashlib.sha256(
            json.dumps(definition, sort_keys=True).encode()
        ).hexdigest(),
    )
    db.add(version)
    db.flush()
    db.add(
        WorkflowTrigger(
            tenant_id=row.tenant_id,
            workflow_id=row.id,
            version_id=version.id,
            **plan.trigger.model_dump(),
        )
    )
    db.flush()
    record(
        db, "workflow.version_created", "workflow", row.id, row.tenant_id, context.identity.user.id
    )
    return version


def create(db: Session, context: TenantContext, data: WorkflowCreate) -> WorkflowDefinition:
    manage_lock(db, context)
    if db.scalar(
        select(WorkflowDefinition.id).where(
            WorkflowDefinition.tenant_id == context.tenant.id, WorkflowDefinition.name == data.name
        )
    ):
        raise HTTPException(409, "Workflow name already exists")
    row = WorkflowDefinition(
        id=uuid.uuid4(),
        tenant_id=context.tenant.id,
        name=data.name,
        description=data.description,
        status="INACTIVE",
        active_version=1,
        trigger_configuration=data.definition.trigger.model_dump(mode="json"),
        created_by=context.identity.user.id,
    )
    db.add(row)
    db.flush()
    add_version(db, context, row, data.definition)
    return row


def activate(
    db: Session, context: TenantContext, row: WorkflowDefinition, enabled: bool, version_id=None
) -> WorkflowDefinition:
    manage_lock(db, context)
    version = db.scalar(
        select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == row.id,
            WorkflowVersion.tenant_id == context.tenant.id,
            WorkflowVersion.id == version_id
            if version_id
            else WorkflowVersion.version == row.active_version,
        )
    )
    if version is None:
        raise HTTPException(404, "Workflow version unavailable")
    if enabled:
        validate_definition(db, context, WorkflowJSON.model_validate(version.definition))
    row.status = "ACTIVE" if enabled else "INACTIVE"
    row.active_version = version.version
    row.trigger_configuration = version.definition["trigger"]
    record(
        db,
        "workflow.activated" if enabled else "workflow.deactivated",
        "workflow",
        row.id,
        row.tenant_id,
        context.identity.user.id,
    )
    db.flush()
    return row


def steps(db: Session, execution: WorkflowExecution) -> list[WorkflowStepExecution]:
    return list(
        db.scalars(
            select(WorkflowStepExecution)
            .where(
                WorkflowStepExecution.execution_id == execution.id,
                WorkflowStepExecution.tenant_id == execution.tenant_id,
            )
            .order_by(WorkflowStepExecution.position)
        )
    )


def cancel_now(db: Session, execution: WorkflowExecution) -> None:
    for step in steps(db, execution):
        if step.state not in {"SUCCEEDED", "SKIPPED", "FAILED", "CANCELLED"}:
            transition(step, "CANCELLED")
    for schedule in db.scalars(
        select(RetrySchedule).where(
            RetrySchedule.execution_id == execution.id, RetrySchedule.state == "SCHEDULED"
        )
    ):
        schedule.state = "CANCELLED"
    transition(execution, "CANCELLED")
    clear_lease(execution)
    history(db, execution, "CANCELLED")


def cancel(db: Session, context: TenantContext, execution_id: uuid.UUID) -> WorkflowExecution:
    recover_lock(db, context)
    execution = get_execution(db, context, execution_id, True)
    if execution.state in TERMINAL:
        raise HTTPException(409, "Execution is terminal")
    execution.cancel_requested = True
    if execution.state != "RUNNING":
        cancel_now(db, execution)
    else:
        history(db, execution, "CANCEL_REQUESTED")
    record(
        db,
        "execution.cancel_requested",
        "execution",
        execution.id,
        execution.tenant_id,
        context.identity.user.id,
    )
    return execution


def replay(db: Session, context: TenantContext, execution_id: uuid.UUID) -> WorkflowExecution:
    recover_lock(db, context)
    parent = get_execution(db, context, execution_id, True)
    if parent.state not in {"FAILED", "DEAD_LETTERED"}:
        raise HTTPException(409, "Execution is not eligible for replay")
    principal_context(db, parent)
    existing = db.scalar(
        select(WorkflowExecution).where(WorkflowExecution.replay_of_id == parent.id)
    )
    if existing:
        return existing
    if db.scalar(
        select(WorkflowActionReceipt.id).where(
            WorkflowActionReceipt.root_execution_id == parent.root_id,
            WorkflowActionReceipt.state == "UNKNOWN",
        )
    ):
        raise HTTPException(409, "Action outcome uncertain; replay denied")
    row = WorkflowExecution(
        id=uuid.uuid4(),
        tenant_id=parent.tenant_id,
        workflow_id=parent.workflow_id,
        version_id=parent.version_id,
        event_id=parent.event_id,
        connection_id=parent.connection_id,
        root_id=parent.root_id,
        replay_of_id=parent.id,
        generation=parent.generation + 1,
        principal_id=parent.principal_id,
        correlation_id=parent.correlation_id,
        next_run_at=utcnow(),
    )
    db.add(row)
    db.flush()
    old = steps(db, parent)
    for step in old:
        if step.step_type == "MOCK_CONNECTOR_ACTION" and step.state == "SUCCEEDED":
            receipt = db.scalar(
                select(WorkflowActionReceipt).where(
                    WorkflowActionReceipt.tenant_id == parent.tenant_id,
                    WorkflowActionReceipt.root_execution_id == parent.root_id,
                    WorkflowActionReceipt.step_id == step.step_id,
                )
            )
            if receipt is None or receipt.state != "APPLIED" or receipt.result != step.output:
                raise HTTPException(409, "Completed action receipt cannot be verified")
    for step in old:
        done = step.state in {"SUCCEEDED", "SKIPPED"}
        db.add(
            WorkflowStepExecution(
                tenant_id=row.tenant_id,
                execution_id=row.id,
                step_id=step.step_id,
                position=step.position,
                step_type=step.step_type,
                state=step.state if done else "PENDING",
                output=step.output if done else None,
                attempt_count=0,
                finished_at=step.finished_at if done else None,
            )
        )
    row.cursor = next(
        (s.position for s in old if s.state not in {"SUCCEEDED", "SKIPPED"}), len(old)
    )
    history(db, row, "REPLAY_CREATED")
    history(db, parent, "REPLAYED")
    record(db, "execution.replayed", "execution", row.id, row.tenant_id, context.identity.user.id)
    db.flush()
    return row
