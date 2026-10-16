"""Allowlisted tenant workflow management, observation and recovery API."""

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.api.canonical import DB, Manage, Read, mutations, page
from app.api.dependencies import TenantContext, require_tenant_permission
from app.api.webhooks import Limit, Offset
from app.core.security import utcnow
from app.models.workflow import (
    DeadLetterRecord,
    RetrySchedule,
    WorkerLease,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionEvent,
    WorkflowStepExecution,
    WorkflowVersion,
)
from app.schemas.workflow import (
    DispatchRequest,
    ExecutionState,
    ReplayRequest,
    WorkflowActivation,
    WorkflowCreate,
    WorkflowJSON,
    WorkflowVersionCreate,
)
from app.services import workflows as service
from app.workflows.engine import dispatch

router = APIRouter(prefix="/api/v1")
Recover = Annotated[TenantContext, Depends(require_tenant_permission("executions:recover"))]


def execution_output(row: WorkflowExecution) -> dict:
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "workflow_id",
            "version_id",
            "event_id",
            "root_id",
            "replay_of_id",
            "generation",
            "state",
            "cursor",
            "cancel_requested",
            "correlation_id",
            "next_run_at",
            "started_at",
            "finished_at",
            "error_category",
            "heartbeat_at",
            "created_at",
        )
    }


def step_output(row: WorkflowStepExecution) -> dict:
    # Never expose action text, candidate PII or raw input/output. Checkpoints stay server-side.
    return {
        **{
            k: getattr(row, k)
            for k in (
                "id",
                "step_id",
                "position",
                "step_type",
                "state",
                "attempt_count",
                "error_category",
                "started_at",
                "finished_at",
            )
        },
        "checkpoint_saved": row.output is not None,
    }


def retry_output(row: RetrySchedule) -> dict:
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "execution_id",
            "step_execution_id",
            "attempt",
            "available_at",
            "state",
            "category",
            "created_at",
        )
    }


def history_output(row: WorkflowExecutionEvent) -> dict:
    return {k: getattr(row, k) for k in ("id", "step_id", "event_type", "category", "created_at")}


def result_page(rows, total, offset, limit, serialize) -> dict:
    return {"items": [serialize(r) for r in rows], "total": total, "offset": offset, "limit": limit}


@router.get("/workflow-schema")
def schema(context: Read) -> dict:
    return WorkflowJSON.model_json_schema()


@router.post("/workflows/validate", dependencies=mutations)
def validate(data: WorkflowJSON, context: Manage, db: DB) -> dict:
    service.manage_lock(db, context)
    service.validate_definition(db, context, data)
    return {"valid": True, "schema_version": 1, "step_count": len(data.steps)}


@router.get("/workflows")
def workflows(context: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    rows, total = page(db, WorkflowDefinition, context.tenant.id, offset, limit)
    return result_page(rows, total, offset, limit, service.workflow_output)


@router.post("/workflows", dependencies=mutations, status_code=201)
def create(data: WorkflowCreate, context: Manage, db: DB) -> dict:
    return service.workflow_output(service.create(db, context, data))


@router.get("/workflows/{workflow_id}")
def workflow(workflow_id: uuid.UUID, context: Read, db: DB) -> dict:
    return service.workflow_output(service.get_workflow(db, context, workflow_id))


@router.get("/workflows/{workflow_id}/versions")
def versions(
    workflow_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    service.get_workflow(db, context, workflow_id)
    rows, total = page(
        db,
        WorkflowVersion,
        context.tenant.id,
        offset,
        limit,
        [WorkflowVersion.workflow_id == workflow_id],
    )
    return result_page(rows, total, offset, limit, service.version_output)


@router.post("/workflows/{workflow_id}/versions", dependencies=mutations, status_code=201)
def version(workflow_id: uuid.UUID, data: WorkflowVersionCreate, context: Manage, db: DB) -> dict:
    service.manage_lock(db, context)
    row = service.get_workflow(db, context, workflow_id)
    return service.version_output(service.add_version(db, context, row, data.definition))


@router.post("/workflows/{workflow_id}/activate", dependencies=mutations)
def activate(workflow_id: uuid.UUID, data: WorkflowActivation, context: Manage, db: DB) -> dict:
    row = service.get_workflow(db, context, workflow_id)
    return service.workflow_output(
        service.activate(db, context, row, data.enabled, data.version_id)
    )


@router.post("/workflow-dispatch", dependencies=mutations)
def manual_dispatch(data: DispatchRequest, context: Manage, db: DB, request: Request) -> dict:
    service.manage_lock(db, context)
    count = dispatch(db, context.tenant.id, rescan=data.rescan_no_match)
    service.record(
        db,
        "workflow.dispatched",
        "workflow",
        tenant_id=context.tenant.id,
        actor_id=context.identity.user.id,
    )
    return {"executions_created": count, "batch_limit": 25}


@router.get("/executions")
def executions(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    state: ExecutionState | None = None,
    workflow_id: uuid.UUID | None = None,
) -> dict:
    predicates = []
    if state:
        predicates.append(WorkflowExecution.state == state)
    if workflow_id:
        predicates.append(WorkflowExecution.workflow_id == workflow_id)
    rows, total = page(db, WorkflowExecution, context.tenant.id, offset, limit, predicates)
    return result_page(rows, total, offset, limit, execution_output)


@router.get("/executions/{execution_id}")
def execution(execution_id: uuid.UUID, context: Read, db: DB) -> dict:
    row = service.get_execution(db, context, execution_id)
    return {**execution_output(row), "steps": [step_output(s) for s in service.steps(db, row)]}


@router.get("/executions/{execution_id}/history")
def execution_history(
    execution_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    service.get_execution(db, context, execution_id)
    rows, total = page(
        db,
        WorkflowExecutionEvent,
        context.tenant.id,
        offset,
        limit,
        [WorkflowExecutionEvent.execution_id == execution_id],
    )
    return result_page(rows, total, offset, limit, history_output)


@router.get("/executions/{execution_id}/retries")
def retries(
    execution_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    service.get_execution(db, context, execution_id)
    rows, total = page(
        db,
        RetrySchedule,
        context.tenant.id,
        offset,
        limit,
        [RetrySchedule.execution_id == execution_id],
    )
    return result_page(rows, total, offset, limit, retry_output)


@router.get("/dead-letters")
def dead_letters(context: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    rows, total = page(db, DeadLetterRecord, context.tenant.id, offset, limit)
    return result_page(
        rows,
        total,
        offset,
        limit,
        lambda r: {
            k: getattr(r, k)
            for k in (
                "id",
                "execution_id",
                "event_id",
                "step_id",
                "category",
                "attempt_count",
                "created_at",
            )
        },
    )


@router.post("/executions/{execution_id}/cancel", dependencies=mutations)
def cancel(execution_id: uuid.UUID, context: Recover, db: DB) -> dict:
    return execution_output(service.cancel(db, context, execution_id))


@router.post("/executions/{execution_id}/replay", dependencies=mutations, status_code=201)
def replay(execution_id: uuid.UUID, data: ReplayRequest, context: Recover, db: DB) -> dict:
    return execution_output(service.replay(db, context, execution_id))


@router.get("/worker-status")
def worker_status(context: Read, db: DB) -> dict:
    now = utcnow()
    workers = list(
        db.scalars(
            select(WorkerLease)
            .where(
                WorkerLease.stopped_at.is_(None),
                WorkerLease.heartbeat_at >= now - timedelta(seconds=15),
            )
            .order_by(WorkerLease.heartbeat_at.desc())
            .limit(20)
        )
    )
    counts = db.execute(
        select(WorkflowExecution.state, func.count())
        .where(WorkflowExecution.tenant_id == context.tenant.id)
        .group_by(WorkflowExecution.state)
    ).all()
    return {
        "active_workers": [{"id": w.id, "heartbeat_at": w.heartbeat_at} for w in workers],
        "backlog": {state: count for state, count in counts},
        "checked_at": now,
        "scope": "tenant backlog; global infrastructure heartbeats",
    }
