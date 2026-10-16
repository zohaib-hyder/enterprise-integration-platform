"""Versioned tenant-scoped synchronization control and review API."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.api.canonical import DB, Manage, Read, mutations, page
from app.api.dependencies import TenantContext, require_tenant_permission
from app.api.webhooks import Limit, Offset
from app.connectors.contracts import ExternalId
from app.models.synchronization import (
    ReconciliationFinding,
    ReconciliationRun,
    SyncConfiguration,
    SyncConfigurationVersion,
    SyncConflict,
    SyncExecution,
    SyncOperation,
    SyncPlan,
    SyncResolution,
)
from app.models.workflow import WorkflowExecution
from app.schemas.synchronization import (
    PlanPreview,
    ReconcileRequest,
    Resolution,
    SyncActivation,
    SyncCreate,
    SyncDefinition,
)
from app.services import synchronization as service

router = APIRouter(prefix="/api/v1/sync", tags=["Synchronization"])
Review = Annotated[TenantContext, Depends(require_tenant_permission("sync:resolve"))]


def paged(db, ctx, model, offset, limit, predicates=None) -> dict:
    rows, total = page(db, model, ctx.tenant.id, offset, limit, predicates)
    items = [service.public(r) for r in rows]
    if model is SyncExecution:
        jobs = {
            j.id: j
            for j in db.scalars(
                select(WorkflowExecution).where(
                    WorkflowExecution.tenant_id == ctx.tenant.id,
                    WorkflowExecution.id.in_(
                        [r.workflow_execution_id for r in rows if r.workflow_execution_id]
                    ),
                )
            )
        }
        for item, row in zip(items, rows, strict=True):
            job = jobs.get(row.workflow_execution_id)
            item["workflow_state"] = job.state if job else None
            item["workflow_error_category"] = job.error_category if job else None
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/configurations")
def configurations(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    return paged(db, ctx, SyncConfiguration, offset, limit)


@router.post("/configurations", dependencies=mutations, status_code=201)
def create(data: SyncCreate, ctx: Manage, db: DB) -> dict:
    return service.public(service.create(db, ctx, data))


@router.get("/configurations/{configuration_id}")
def configuration(configuration_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    return service.public(service.scoped(db, ctx, SyncConfiguration, configuration_id))


@router.get("/configurations/{configuration_id}/versions")
def versions(
    configuration_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    service.scoped(db, ctx, SyncConfiguration, configuration_id)
    return paged(
        db,
        ctx,
        SyncConfigurationVersion,
        offset,
        limit,
        [SyncConfigurationVersion.configuration_id == configuration_id],
    )


@router.post("/configurations/{configuration_id}/versions", dependencies=mutations, status_code=201)
def version(configuration_id: uuid.UUID, data: SyncDefinition, ctx: Manage, db: DB) -> dict:
    return service.public(
        service.add_version(
            db, ctx, service.scoped(db, ctx, SyncConfiguration, configuration_id), data
        )
    )


@router.post("/configurations/{configuration_id}/activate", dependencies=mutations)
def activate(configuration_id: uuid.UUID, data: SyncActivation, ctx: Manage, db: DB) -> dict:
    return service.public(
        service.activate(
            db, ctx, service.scoped(db, ctx, SyncConfiguration, configuration_id), data
        )
    )


@router.post("/configurations/{configuration_id}/preview", dependencies=mutations)
def preview(
    configuration_id: uuid.UUID, data: PlanPreview, request: Request, ctx: Manage, db: DB
) -> dict:
    cfg = service.scoped(db, ctx, SyncConfiguration, configuration_id)
    row, execution = service.plan(
        db,
        ctx,
        cfg,
        service.scoped(db, ctx, SyncConfigurationVersion, cfg.active_version_id),
        data,
        uuid.UUID(request.state.correlation_id),
    )
    return {"plan": service.public(row), "execution": service.public(execution)}


@router.get("/plans/{plan_id}")
def plan(plan_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    return service.public(service.scoped(db, ctx, SyncPlan, plan_id))


@router.get("/executions")
def executions(
    ctx: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    configuration_id: uuid.UUID | None = None,
    state: Literal["PLANNED", "SUCCEEDED", "HELD", "SKIPPED"] | None = None,
) -> dict:
    filters = ([SyncExecution.configuration_id == configuration_id] if configuration_id else []) + (
        [SyncExecution.state == state] if state else []
    )
    return paged(db, ctx, SyncExecution, offset, limit, filters)


@router.get("/executions/{execution_id}")
def execution(execution_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    row = service.scoped(db, ctx, SyncExecution, execution_id)
    return {
        "execution": service.public(row),
        "plan": service.public(service.scoped(db, ctx, SyncPlan, row.plan_id)),
    }


@router.get("/operations")
def operations(
    ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25, execution_id: uuid.UUID | None = None
) -> dict:
    return paged(
        db,
        ctx,
        SyncOperation,
        offset,
        limit,
        [SyncOperation.execution_id == execution_id] if execution_id else [],
    )


@router.get("/operations/{operation_id}")
def operation(operation_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    return service.public(service.scoped(db, ctx, SyncOperation, operation_id))


@router.get("/conflicts")
def conflicts(
    ctx: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    status: Literal["OPEN", "RESOLVED"] | None = None,
) -> dict:
    return paged(
        db, ctx, SyncConflict, offset, limit, [SyncConflict.status == status] if status else []
    )


@router.get("/conflicts/{conflict_id}")
def conflict(
    conflict_id: uuid.UUID, ctx: Read, db: DB, destination_external_id: ExternalId | None = None
) -> dict:
    row = service.scoped(db, ctx, SyncConflict, conflict_id)
    plan = service.scoped(db, ctx, SyncPlan, row.plan_id)
    cfg = service.scoped(db, ctx, SyncConfiguration, plan.configuration_id)
    target = service.destination(
        db, ctx, cfg.destination_connection_id, plan.evidence["destination_identity"]["external_id"]
    )
    source = service.current_source(db, ctx, cfg, plan.evidence["source_identity"]["external_id"])
    candidate = None
    if destination_external_id:
        candidate = service.destination(
            db, ctx, cfg.destination_connection_id, destination_external_id
        )
    return {
        "current_source": source.payload if source else None,
        "current_source_digest": service.destination_digest(source),
        "link_candidate": candidate.payload if candidate else None,
        "link_candidate_digest": service.destination_digest(candidate),
        "conflict": service.public(row),
        "plan": service.public(plan),
        "current_destination_digest": service.destination_digest(target),
        "current_destination": target.payload if target else None,
    }


@router.post("/conflicts/{conflict_id}/resolve", dependencies=mutations)
def resolve(
    conflict_id: uuid.UUID, data: Resolution, request: Request, ctx: Review, db: DB
) -> dict:
    return service.public(
        service.resolve(
            db,
            ctx,
            service.scoped(db, ctx, SyncConflict, conflict_id),
            data,
            uuid.UUID(request.state.correlation_id),
        )
    )


@router.get("/conflicts/{conflict_id}/resolutions")
def resolutions(
    conflict_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    service.scoped(db, ctx, SyncConflict, conflict_id)
    return paged(
        db, ctx, SyncResolution, offset, limit, [SyncResolution.conflict_id == conflict_id]
    )


@router.post(
    "/configurations/{configuration_id}/reconcile", dependencies=mutations, status_code=201
)
def reconcile(configuration_id: uuid.UUID, data: ReconcileRequest, ctx: Manage, db: DB) -> dict:
    return service.public(
        service.reconcile(
            db, ctx, service.scoped(db, ctx, SyncConfiguration, configuration_id), data
        )
    )


@router.get("/reconciliation-runs")
def runs(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    return paged(db, ctx, ReconciliationRun, offset, limit)


@router.get("/reconciliation-runs/{run_id}")
def run(run_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    return service.public(service.scoped(db, ctx, ReconciliationRun, run_id))


@router.get("/reconciliation-runs/{run_id}/findings")
def findings(run_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    service.scoped(db, ctx, ReconciliationRun, run_id)
    return paged(
        db, ctx, ReconciliationFinding, offset, limit, [ReconciliationFinding.run_id == run_id]
    )


@router.post("/findings/{finding_id}/plan", dependencies=mutations)
def finding_plan(finding_id: uuid.UUID, request: Request, ctx: Manage, db: DB) -> dict:
    row = service.scoped(db, ctx, ReconciliationFinding, finding_id)
    if row.source_external_id is None:
        from fastapi import HTTPException

        raise HTTPException(422, "This finding requires identity or conflict review")
    run = service.scoped(db, ctx, ReconciliationRun, row.run_id)
    cfg = service.scoped(db, ctx, SyncConfiguration, run.configuration_id)
    plan, execution = service.plan(
        db,
        ctx,
        cfg,
        service.scoped(db, ctx, SyncConfigurationVersion, cfg.active_version_id),
        PlanPreview(source_external_id=row.source_external_id),
        uuid.UUID(request.state.correlation_id),
    )
    return {"plan": service.public(plan), "execution": service.public(execution)}


@router.get("/status")
def status(ctx: Read, db: DB) -> dict:
    counts = {}
    for model, key, column in [
        (SyncExecution, "executions", SyncExecution.state),
        (SyncConflict, "conflicts", SyncConflict.status),
    ]:
        counts[key] = dict(
            db.execute(
                select(column, func.count())
                .where(model.tenant_id == ctx.tenant.id)
                .group_by(column)
            ).all()
        )
    counts["enabled_configurations"] = db.scalar(
        select(func.count())
        .select_from(SyncConfiguration)
        .where(SyncConfiguration.tenant_id == ctx.tenant.id, SyncConfiguration.enabled)
    )
    counts["basis"] = "Persisted tenant synchronization records"
    return counts
