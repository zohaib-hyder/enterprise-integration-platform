"""Command center APIs with safe typed responses and existing security dependencies."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.canonical import DB, Manage, Read, mutations, page
from app.api.notifications import Recover
from app.api.webhooks import Limit, Offset
from app.models.operations import (
    Incident,
    OperationalFinding,
    OperationalReconciliationRun,
    OperationsActivity,
    RecoveryApproval,
    RecoveryRequest,
)
from app.operations.runbooks import CATALOG, runbook
from app.schemas.operations import (
    ActivityOutput,
    ApprovalDecision,
    ApprovalOutput,
    AssigneeOutput,
    Confirmation,
    FindingOutput,
    IncidentChange,
    IncidentOutput,
    IncidentRefreshOutput,
    IncidentStatus,
    OperationsPage,
    OverviewOutput,
    RecoveryCreate,
    RecoveryOutput,
    RecoveryPlanOutput,
    RecoveryPreview,
    RunbookOutput,
    ScanInput,
    ScanOutput,
    Severity,
    TimelineOutput,
)
from app.services import operation_recovery as recovery
from app.services import operational_reconciliation as reconciliation
from app.services import operations as service
from app.services.synchronization import scoped

router = APIRouter(prefix="/api/v1/operations", tags=["Operations command center"])
Hours = Literal["1", "24", "168", "720"]


def safe_row(row, dto) -> dict:
    return {key: getattr(row, key) for key in dto.model_fields}


def paged(db, ctx, model, dto, offset, limit, filters=None) -> dict:
    rows, total = page(db, model, ctx.tenant.id, offset, limit, filters)
    return {
        "items": [safe_row(r, dto) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/overview", response_model=OverviewOutput)
def overview(ctx: Read, db: DB, hours: Hours = "24") -> dict:
    return service.overview(db, ctx, int(hours))


@router.get("/health", response_model=OverviewOutput)
def health(ctx: Read, db: DB, hours: Hours = "1") -> dict:
    return service.overview(db, ctx, int(hours))


@router.get("/timeline/{correlation_id}", response_model=TimelineOutput)
def timeline(
    correlation_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 100
) -> dict:
    return service.timeline(db, ctx, correlation_id, offset, limit)


@router.get("/incidents", response_model=OperationsPage[IncidentOutput])
def incidents(
    ctx: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    status: IncidentStatus | None = None,
    severity: Severity | None = None,
) -> dict:
    return paged(
        db,
        ctx,
        Incident,
        IncidentOutput,
        offset,
        limit,
        ([Incident.status == status] if status else [])
        + ([Incident.severity == severity] if severity else []),
    )


@router.post("/incidents/refresh", dependencies=mutations, response_model=IncidentRefreshOutput)
def refresh(data: ScanInput, ctx: Recover, db: DB) -> dict:
    return service.collect_incidents(db, ctx, data.limit, data.hours)


@router.get("/incidents/{incident_id}", response_model=IncidentOutput)
def incident(incident_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    return safe_row(scoped(db, ctx, Incident, incident_id), IncidentOutput)


@router.patch("/incidents/{incident_id}", dependencies=mutations, response_model=IncidentOutput)
def update_incident(incident_id: uuid.UUID, data: IncidentChange, ctx: Recover, db: DB) -> dict:
    return safe_row(
        service.change_incident(db, ctx, scoped(db, ctx, Incident, incident_id), data),
        IncidentOutput,
    )


@router.get("/incidents/{incident_id}/activity", response_model=OperationsPage[ActivityOutput])
def incident_activity(
    incident_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    scoped(db, ctx, Incident, incident_id)
    return paged(
        db,
        ctx,
        OperationsActivity,
        ActivityOutput,
        offset,
        limit,
        [OperationsActivity.incident_id == incident_id],
    )


@router.get("/incidents/{incident_id}/evidence", response_model=RecoveryPlanOutput)
def evidence(incident_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    return recovery.plan(db, ctx, "incident", incident_id)


@router.post("/recovery/preview", dependencies=mutations, response_model=RecoveryPlanOutput)
def preview(data: RecoveryPreview, ctx: Read, db: DB) -> dict:
    return recovery.plan(db, ctx, data.target_kind, data.target_id)


@router.post(
    "/recovery/requests", dependencies=mutations, status_code=201, response_model=RecoveryOutput
)
def request_recovery(data: RecoveryCreate, request: Request, ctx: Recover, db: DB) -> dict:
    return safe_row(
        recovery.create_request(db, ctx, data, uuid.UUID(request.state.correlation_id)),
        RecoveryOutput,
    )


@router.get("/recovery/requests", response_model=OperationsPage[RecoveryOutput])
def requests(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    return paged(db, ctx, RecoveryRequest, RecoveryOutput, offset, limit)


@router.get("/recovery/requests/{request_id}", response_model=RecoveryOutput)
def get_request(request_id: uuid.UUID, ctx: Read, db: DB) -> dict:
    return safe_row(scoped(db, ctx, RecoveryRequest, request_id), RecoveryOutput)


@router.post(
    "/recovery/requests/{request_id}/confirm", dependencies=mutations, response_model=RecoveryOutput
)
def confirm(request_id: uuid.UUID, data: Confirmation, ctx: Recover, db: DB):
    row = recovery.confirm(db, ctx, scoped(db, ctx, RecoveryRequest, request_id), data)
    result = safe_row(row, RecoveryOutput)
    if row.status != "EXECUTED":
        return JSONResponse(jsonable_encoder(result), status_code=409)
    return result


@router.get(
    "/recovery/requests/{request_id}/activity", response_model=OperationsPage[ActivityOutput]
)
def request_activity(
    request_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    scoped(db, ctx, RecoveryRequest, request_id)
    return paged(
        db,
        ctx,
        OperationsActivity,
        ActivityOutput,
        offset,
        limit,
        [OperationsActivity.target_kind == "recovery", OperationsActivity.target_id == request_id],
    )


@router.get("/approvals", response_model=OperationsPage[ApprovalOutput])
def approvals(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    return paged(db, ctx, RecoveryApproval, ApprovalOutput, offset, limit)


@router.post(
    "/approvals/{approval_id}/decide", dependencies=mutations, response_model=ApprovalOutput
)
def decide(approval_id: uuid.UUID, data: ApprovalDecision, ctx: Manage, db: DB) -> dict:
    return safe_row(
        recovery.approve(db, ctx, scoped(db, ctx, RecoveryApproval, approval_id), data),
        ApprovalOutput,
    )


@router.post(
    "/reconciliation-runs", dependencies=mutations, status_code=201, response_model=ScanOutput
)
def reconcile(data: ScanInput, request: Request, ctx: Recover, db: DB) -> dict:
    return safe_row(
        reconciliation.scan(db, ctx, data, uuid.UUID(request.state.correlation_id)), ScanOutput
    )


@router.get("/reconciliation-runs", response_model=OperationsPage[ScanOutput])
def runs(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    return paged(db, ctx, OperationalReconciliationRun, ScanOutput, offset, limit)


@router.get("/reconciliation-runs/{run_id}/findings", response_model=OperationsPage[FindingOutput])
def findings(run_id: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    scoped(db, ctx, OperationalReconciliationRun, run_id)
    return paged(
        db,
        ctx,
        OperationalFinding,
        FindingOutput,
        offset,
        limit,
        [OperationalFinding.run_id == run_id],
    )


@router.get("/runbooks", response_model=list[RunbookOutput])
def runbooks(ctx: Read) -> list:
    return [runbook(key) for key in CATALOG]


@router.get("/runbooks/{identifier}", response_model=RunbookOutput)
def get_runbook(identifier: str, ctx: Read) -> dict:
    result = runbook(identifier)
    if result is None:
        raise HTTPException(404, "Runbook unavailable")
    return result


from sqlalchemy import func, select

from app.models.identity import Membership, User


@router.get("/assignees", response_model=OperationsPage[AssigneeOutput])
def assignees(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 100) -> dict:
    scope = [Membership.tenant_id == ctx.tenant.id, Membership.status == "ACTIVE", User.is_active]
    total = db.scalar(
        select(func.count())
        .select_from(Membership)
        .join(User, User.id == Membership.user_id)
        .where(*scope)
    )
    rows = db.execute(
        select(User.id, User.display_name, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(*scope)
        .order_by(User.display_name, User.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [
            {"id": identifier, "display_name": name, "role": role}
            for identifier, name, role in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }
