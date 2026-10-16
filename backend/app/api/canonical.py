import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, get_db, require_tenant_permission
from app.api.webhooks import Limit, Offset
from app.core.security import require_csrf
from app.models.canonical import (
    CanonicalRecord,
    ExternalRecordMapping,
    FieldAuthorityRule,
    QuarantinedRecord,
    TransformationAttempt,
    TransformationMapping,
    TransformationMappingVersion,
)
from app.schemas.canonical import (
    ENTITY_MODELS,
    SOURCE_MODELS,
    SOURCE_PATHS,
    SUPPORTED,
    Activation,
    AttemptOutput,
    AttemptPage,
    AuthorityPage,
    CanonicalEnvelope,
    CanonicalPublic,
    EntityType,
    ExternalLink,
    ExternalOutput,
    ExternalPage,
    InvalidOutput,
    InvalidPage,
    MappingCreate,
    MappingOutput,
    MappingPage,
    MappingVersionCreate,
    PreviewOutput,
    RecordPage,
    SourceKind,
    TransformInput,
    VersionChoice,
    VersionOutput,
    VersionPage,
)
from app.services import canonical as service
from app.transformations.engine import template

router = APIRouter(prefix="/api/v1")
DB = Annotated[Session, Depends(get_db, scope="function")]
Read = Annotated[TenantContext, Depends(require_tenant_permission("operations:read"))]
Manage = Annotated[TenantContext, Depends(require_tenant_permission("integrations:manage"))]
mutations = [Depends(require_csrf)]


def page(
    db: Session,
    model,
    tenant_id: uuid.UUID,
    offset: int,
    limit: int,
    predicates: list | None = None,
) -> tuple[list, int]:
    scope = [model.tenant_id == tenant_id, *(predicates or [])]
    total = db.scalar(select(func.count()).select_from(model).where(*scope)) or 0
    rows = list(
        db.scalars(
            select(model)
            .where(*scope)
            .order_by(model.created_at.desc(), model.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    return rows, total


def outcome_response(row: TransformationAttempt) -> JSONResponse:
    result = service.attempt_output(row)
    status = (
        422
        if row.outcome == "QUARANTINED"
        else 409
        if row.outcome in {"CONFLICT", "MANUAL_REVIEW"}
        else 200
    )
    if status != 200:
        result["detail"] = "Transformation requires review"
    return JSONResponse(jsonable_encoder(result), status_code=status)


@router.get("/canonical-schemas")
def schemas(context: Read) -> dict:
    return {
        "schema_version": 1,
        "entities": {name: model.model_json_schema() for name, model in ENTITY_MODELS.items()},
        "envelope": CanonicalEnvelope.model_json_schema(),
    }


@router.get("/mapping-templates")
def templates(context: Read) -> list[dict]:
    return [
        {
            "source_type": kind,
            "source_kind": source_kind,
            "entity_type": target,
            "source_schema": SOURCE_MODELS[source_kind].model_json_schema(),
            "target_schema": ENTITY_MODELS[target].model_json_schema(),
            "source_paths": sorted(SOURCE_PATHS[source_kind]),
            "target_fields": list(ENTITY_MODELS[target].model_fields),
            "definition": template(kind, source_kind).model_dump(mode="json"),
        }
        for (kind, source_kind), target in SUPPORTED.items()
    ]


@router.get("/mappings", response_model=MappingPage)
def mappings(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    connection_id: uuid.UUID | None = None,
) -> dict:
    rows, total = page(
        db,
        TransformationMapping,
        context.tenant.id,
        offset,
        limit,
        [TransformationMapping.connection_id == connection_id] if connection_id else [],
    )
    return {
        "items": [service.mapping_output(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/mappings", response_model=MappingOutput, status_code=201, dependencies=mutations)
def create(data: MappingCreate, context: Manage, db: DB) -> dict:
    return service.mapping_output(service.create_mapping(db, context, data))


@router.get("/mappings/{mapping_id}", response_model=MappingOutput)
def details(mapping_id: uuid.UUID, context: Read, db: DB) -> dict:
    return service.mapping_output(service.get_mapping(db, context, mapping_id))


@router.get("/mappings/{mapping_id}/versions", response_model=VersionPage)
def versions(
    mapping_id: uuid.UUID, context: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    service.get_mapping(db, context, mapping_id)
    rows, total = page(
        db,
        TransformationMappingVersion,
        context.tenant.id,
        offset,
        limit,
        [TransformationMappingVersion.mapping_id == mapping_id],
    )
    return {
        "items": [service.version_output(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post(
    "/mappings/{mapping_id}/versions",
    response_model=VersionOutput,
    status_code=201,
    dependencies=mutations,
)
def new_version(mapping_id: uuid.UUID, data: MappingVersionCreate, context: Manage, db: DB) -> dict:
    return service.version_output(service.create_version(db, context, mapping_id, data.definition))


@router.post(
    "/mappings/{mapping_id}/activate", response_model=MappingOutput, dependencies=mutations
)
def activate(mapping_id: uuid.UUID, data: Activation, context: Manage, db: DB) -> dict:
    return service.mapping_output(
        service.activate(db, context, mapping_id, data.mapping_version_id)
    )


@router.post("/mappings/{mapping_id}/preview", response_model=PreviewOutput, dependencies=mutations)
def preview(
    mapping_id: uuid.UUID, data: TransformInput, context: Manage, db: DB, request: Request
) -> dict:
    return service.preview(db, context, mapping_id, data, uuid.UUID(request.state.correlation_id))


@router.post("/mappings/{mapping_id}/normalize", dependencies=mutations)
def normalize(
    mapping_id: uuid.UUID, data: TransformInput, context: Manage, db: DB, request: Request
) -> JSONResponse:
    return outcome_response(
        service.normalize(db, context, mapping_id, data, uuid.UUID(request.state.correlation_id))
    )


@router.post(
    "/events/{event_id}/preview-transformation",
    response_model=PreviewOutput,
    dependencies=mutations,
)
def preview_event(
    event_id: uuid.UUID, data: VersionChoice, context: Manage, db: DB, request: Request
) -> dict:
    service.manage_lock(db, context)
    mapping, source = service.event_input(db, context, event_id, data.mapping_version_id)
    return service.preview(db, context, mapping.id, source, uuid.UUID(request.state.correlation_id))


@router.post("/events/{event_id}/normalize", dependencies=mutations)
def normalize_event(
    event_id: uuid.UUID, data: VersionChoice, context: Manage, db: DB, request: Request
) -> JSONResponse:
    service.manage_lock(db, context)
    mapping, source = service.event_input(db, context, event_id, data.mapping_version_id)
    return outcome_response(
        service.normalize(
            db, context, mapping.id, source, uuid.UUID(request.state.correlation_id), event_id
        )
    )


@router.get("/canonical-records", response_model=RecordPage)
def records(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    entity_type: EntityType | None = None,
) -> dict:
    rows, total = page(
        db,
        CanonicalRecord,
        context.tenant.id,
        offset,
        limit,
        [CanonicalRecord.entity_type == entity_type] if entity_type else [],
    )
    return {
        "items": [service.record_output(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/canonical-records/{canonical_id}", response_model=CanonicalPublic)
def record_details(canonical_id: uuid.UUID, context: Read, db: DB) -> dict:
    return service.record_output(service.get_record(db, context, canonical_id))


@router.get("/external-identities", response_model=ExternalPage)
def identities(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    connection_id: uuid.UUID | None = None,
    canonical_id: uuid.UUID | None = None,
    source_kind: SourceKind | None = None,
    external_id: Annotated[
        str | None, Query(max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    ] = None,
) -> dict:
    scope = []
    for field, value in [
        ("connection_id", connection_id),
        ("canonical_id", canonical_id),
        ("source_kind", source_kind),
        ("external_id", external_id),
    ]:
        if value is not None:
            scope.append(getattr(ExternalRecordMapping, field) == value)
    rows, total = page(db, ExternalRecordMapping, context.tenant.id, offset, limit, scope)
    return {
        "items": [service.external_output(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post(
    "/external-identities", response_model=ExternalOutput, status_code=201, dependencies=mutations
)
def link(data: ExternalLink, context: Manage, db: DB) -> dict:
    return service.external_output(service.link(db, context, data))


@router.get("/field-authority", response_model=AuthorityPage)
def authority(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 100,
    entity_type: EntityType | None = None,
) -> dict:
    rows, total = page(
        db,
        FieldAuthorityRule,
        context.tenant.id,
        offset,
        limit,
        [FieldAuthorityRule.entity_type == entity_type] if entity_type else [],
    )
    return {
        "items": [
            {key: getattr(row, key) for key in ("id", "entity_type", "field", "owner")}
            for row in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def attempt_page(db: Session, context: TenantContext, offset: int, limit: int, scope: list) -> dict:
    rows, total = page(db, TransformationAttempt, context.tenant.id, offset, limit, scope)
    return {
        "items": [service.attempt_output(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/transformation-attempts", response_model=AttemptPage)
def attempts(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    outcome: Literal["SUCCEEDED", "NO_CHANGE", "QUARANTINED", "CONFLICT", "MANUAL_REVIEW"]
    | None = None,
) -> dict:
    return attempt_page(
        db, context, offset, limit, [TransformationAttempt.outcome == outcome] if outcome else []
    )


@router.get("/transformation-attempts/{attempt_id}", response_model=AttemptOutput)
def attempt_details(attempt_id: uuid.UUID, context: Read, db: DB) -> dict:
    row = db.scalar(
        select(TransformationAttempt).where(
            TransformationAttempt.id == attempt_id,
            TransformationAttempt.tenant_id == context.tenant.id,
        )
    )
    if row is None:
        raise HTTPException(404, "Attempt unavailable")
    return service.attempt_output(row)


@router.get("/transformation-conflicts", response_model=AttemptPage)
def conflicts(context: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    return attempt_page(
        db,
        context,
        offset,
        limit,
        [TransformationAttempt.outcome.in_(["CONFLICT", "MANUAL_REVIEW"])],
    )


def invalid_output(db: Session, row: QuarantinedRecord) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "resolution_attempt_id": row.resolution_attempt_id,
        "created_at": row.created_at,
        "attempt": service.attempt_output(db.get(TransformationAttempt, row.attempt_id)),
    }


@router.get("/invalid-records", response_model=InvalidPage)
def invalid_records(
    context: Read,
    db: DB,
    offset: Offset = 0,
    limit: Limit = 25,
    status: Literal["OPEN", "RESOLVED"] | None = None,
) -> dict:
    rows, total = page(
        db,
        QuarantinedRecord,
        context.tenant.id,
        offset,
        limit,
        [QuarantinedRecord.status == status] if status else [],
    )
    return {
        "items": [invalid_output(db, row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/invalid-records/{invalid_id}", response_model=InvalidOutput)
def invalid_details(invalid_id: uuid.UUID, context: Read, db: DB) -> dict:
    row = db.scalar(
        select(QuarantinedRecord).where(
            QuarantinedRecord.id == invalid_id, QuarantinedRecord.tenant_id == context.tenant.id
        )
    )
    if row is None:
        raise HTTPException(404, "Invalid record unavailable")
    return invalid_output(db, row)


@router.post("/invalid-records/{invalid_id}/reprocess", dependencies=mutations)
def reprocess(
    invalid_id: uuid.UUID, data: Activation, context: Manage, db: DB, request: Request
) -> JSONResponse:
    return outcome_response(
        service.reprocess(
            db,
            context,
            invalid_id,
            data.mapping_version_id,
            uuid.UUID(request.state.correlation_id),
        )
    )
