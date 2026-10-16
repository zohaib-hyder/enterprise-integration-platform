import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, get_db, require_tenant_permission
from app.connectors.contracts import (
    ConnectorCapabilities,
    ConnectorHealth,
    ConnectorOperation,
    ConnectorResult,
    OperationSpec,
)
from app.connectors.registry import registry
from app.core.security import require_csrf
from app.models.integration import ConnectorDefinition, IntegrationConnection
from app.schemas.integration import (
    ConnectionOutput,
    ConnectionPage,
    CreateConnection,
    InspectionPage,
    UpdateConnection,
)
from app.services import integrations

router = APIRouter(prefix="/api/v1")
DB = Annotated[Session, Depends(get_db, scope="function")]
ReadContext = Annotated[TenantContext, Depends(require_tenant_permission("tenant:read"))]
ManageContext = Annotated[TenantContext, Depends(require_tenant_permission("integrations:manage"))]
mutations = [Depends(require_csrf)]


@router.get("/connector-definitions", response_model=list[ConnectorCapabilities])
def definitions(context: ReadContext, db: DB) -> list[ConnectorCapabilities]:
    types = db.scalars(
        select(ConnectorDefinition.connector_type).order_by(ConnectorDefinition.name)
    )
    return [registry.capabilities(kind) for kind in types]


@router.get("/connections", response_model=ConnectionPage)
def connections(
    context: ReadContext,
    db: DB,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ConnectionPage:
    scope = IntegrationConnection.tenant_id == context.tenant.id
    total = db.scalar(select(func.count()).select_from(IntegrationConnection).where(scope))
    rows = db.scalars(
        select(IntegrationConnection)
        .where(scope)
        .order_by(IntegrationConnection.created_at, IntegrationConnection.id)
        .offset(offset)
        .limit(limit)
    )
    return ConnectionPage(
        items=[integrations.connection_output(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/connections", response_model=ConnectionOutput, status_code=201, dependencies=mutations
)
def create(data: CreateConnection, context: ManageContext, db: DB) -> ConnectionOutput:
    return integrations.connection_output(integrations.create_connection(db, context, data))


@router.get("/connections/{connection_id}", response_model=ConnectionOutput)
def details(connection_id: uuid.UUID, context: ReadContext, db: DB) -> ConnectionOutput:
    return integrations.connection_output(integrations.get_connection(db, context, connection_id))


@router.patch(
    "/connections/{connection_id}", response_model=ConnectionOutput, dependencies=mutations
)
def update(
    connection_id: uuid.UUID, data: UpdateConnection, context: ManageContext, db: DB
) -> ConnectionOutput:
    return integrations.connection_output(
        integrations.update_connection(db, context, connection_id, data)
    )


@router.post(
    "/connections/{connection_id}/health", response_model=ConnectorHealth, dependencies=mutations
)
def health(connection_id: uuid.UUID, context: ManageContext, db: DB) -> ConnectorHealth:
    return integrations.test_health(db, context, connection_id)


@router.get("/connections/{connection_id}/operations", response_model=list[OperationSpec])
def operations(connection_id: uuid.UUID, context: ReadContext, db: DB) -> list[OperationSpec]:
    row = integrations.get_connection(db, context, connection_id)
    return registry.capabilities(row.connector_type).operations


@router.post(
    "/connections/{connection_id}/execute", response_model=ConnectorResult, dependencies=mutations
)
def execute(
    connection_id: uuid.UUID,
    data: ConnectorOperation,
    context: ManageContext,
    db: DB,
    request: Request,
):
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    from app.api.reliability import error_response
    from app.connectors.reliability import ReliabilityError

    try:
        result = integrations.execute(
            db,
            context,
            connection_id,
            data,
            correlation_id=uuid.UUID(request.state.correlation_id),
            settings=request.app.state.settings,
        )
        return JSONResponse(
            jsonable_encoder(result), headers={"X-Operation-ID": str(db.info["last_operation_id"])}
        )
    except ReliabilityError as error:
        # Return a response so the normal dependency commits the failed-attempt evidence.
        return error_response(error, uuid.UUID(request.state.correlation_id))


@router.get("/connections/{connection_id}/records", response_model=InspectionPage)
def records(
    connection_id: uuid.UUID,
    context: ManageContext,
    db: DB,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> InspectionPage:
    return integrations.inspect_records(db, context, connection_id, offset, limit)
