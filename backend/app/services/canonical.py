"""Tenant-authorized immutable mapping lifecycle, canonical identity and merge policy."""

import hashlib
import json
import uuid
from datetime import datetime

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.core.security import utcnow
from app.models.canonical import (
    CanonicalRecord,
    ExternalRecordMapping,
    FieldAuthorityRule,
    QuarantinedRecord,
    TransformationAttempt,
    TransformationMapping,
    TransformationMappingVersion,
)
from app.models.webhook import IncomingEvent
from app.schemas.canonical import (
    ENTITY_MODELS,
    SUPPORTED,
    CanonicalEnvelope,
    ExternalLink,
    MappingCreate,
    MappingDefinition,
    TransformInput,
    ValidationIssue,
    normalize_time,
)
from app.services.audit import record as audit
from app.services.integrations import get_connection
from app.services.webhooks import manage_lock, redact
from app.transformations.engine import (
    TransformationFailure,
    source_payload,
    template,
    transform,
    validate_definition,
)

# Business authority is code-owned, deterministic and inspectable per tenant.
AUTHORITY = {
    "client": {name: "synthetic_crm" for name in ENTITY_MODELS["client"].model_fields},
    "contact": {name: "synthetic_crm" for name in ENTITY_MODELS["contact"].model_fields},
    "candidate": {name: "synthetic_ats" for name in ENTITY_MODELS["candidate"].model_fields},
    "application": {name: "synthetic_ats" for name in ENTITY_MODELS["application"].model_fields},
    "activity": {name: "synthetic_calendar" for name in ENTITY_MODELS["activity"].model_fields},
    "job_requisition": {
        "title": "ORIGIN",
        "department": "ORIGIN",
        "location": "ORIGIN",
        "status": "synthetic_crm",
        "recruiting_status": "synthetic_ats",
        "commercial_rate": "synthetic_crm",
        "description": "REVIEW",
    },
}


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()


def initialize_authority(db: Session, context: TenantContext) -> None:
    existing = set(
        db.execute(
            select(FieldAuthorityRule.entity_type, FieldAuthorityRule.field).where(
                FieldAuthorityRule.tenant_id == context.tenant.id
            )
        ).all()
    )
    for entity, fields in AUTHORITY.items():
        for field, owner in fields.items():
            if (entity, field) not in existing:
                db.add(
                    FieldAuthorityRule(
                        tenant_id=context.tenant.id, entity_type=entity, field=field, owner=owner
                    )
                )
    db.flush()


def get_mapping(
    db: Session, context: TenantContext, mapping_id: uuid.UUID
) -> TransformationMapping:
    row = db.scalar(
        select(TransformationMapping).where(
            TransformationMapping.id == mapping_id,
            TransformationMapping.tenant_id == context.tenant.id,
        )
    )
    if row is None:
        raise HTTPException(404, "Mapping unavailable")
    return row


def resolve_version(
    db: Session,
    context: TenantContext,
    mapping: TransformationMapping,
    version_id: uuid.UUID | None = None,
) -> TransformationMappingVersion:
    scope = [
        TransformationMappingVersion.tenant_id == context.tenant.id,
        TransformationMappingVersion.mapping_id == mapping.id,
    ]
    scope.append(
        TransformationMappingVersion.id == version_id
        if version_id
        else TransformationMappingVersion.version == mapping.active_version
    )
    row = db.scalar(select(TransformationMappingVersion).where(*scope))
    if row is None:
        raise HTTPException(404, "Mapping version unavailable")
    return row


def mapping_output(row: TransformationMapping) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "connection_id",
            "source_kind",
            "entity_type",
            "name",
            "active_version",
            "created_at",
            "updated_at",
        )
    }


def version_output(row: TransformationMappingVersion) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "mapping_id",
            "version",
            "schema_version",
            "definition",
            "definition_digest",
            "created_at",
        )
    }


def add_version(
    db: Session,
    context: TenantContext,
    mapping: TransformationMapping,
    definition: MappingDefinition,
) -> TransformationMappingVersion:
    validate_definition(definition, mapping.source_kind, mapping.entity_type)
    number = (
        db.scalar(
            select(func.max(TransformationMappingVersion.version)).where(
                TransformationMappingVersion.mapping_id == mapping.id
            )
        )
        or 0
    ) + 1
    data = definition.model_dump(mode="json")
    row = TransformationMappingVersion(
        tenant_id=context.tenant.id,
        connection_id=mapping.connection_id,
        mapping_id=mapping.id,
        version=number,
        definition=data,
        definition_digest=digest(data),
        created_by=context.identity.user.id,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "mapping.version_created",
        "mapping_version",
        row.id,
        context.tenant.id,
        context.identity.user.id,
    )
    return row


def create_mapping(
    db: Session, context: TenantContext, data: MappingCreate
) -> TransformationMapping:
    manage_lock(db, context)
    connection = get_connection(db, context, data.connection_id)
    target = SUPPORTED.get((connection.connector_type, data.source_kind))
    if target is None or not data.name.strip():
        raise HTTPException(422, "Unsupported mapping source")
    if db.scalar(
        select(TransformationMapping.id).where(
            TransformationMapping.tenant_id == context.tenant.id,
            TransformationMapping.connection_id == connection.id,
            TransformationMapping.source_kind == data.source_kind,
        )
    ):
        raise HTTPException(409, "Mapping source already registered")
    definition = data.definition or template(connection.connector_type, data.source_kind)
    validate_definition(definition, data.source_kind, target)
    row = TransformationMapping(
        tenant_id=context.tenant.id,
        connection_id=connection.id,
        source_kind=data.source_kind,
        entity_type=target,
        name=data.name.strip(),
    )
    db.add(row)
    db.flush()
    add_version(db, context, row, definition)
    initialize_authority(db, context)
    audit(db, "mapping.created", "mapping", row.id, context.tenant.id, context.identity.user.id)
    return row


def create_version(
    db: Session, context: TenantContext, mapping_id: uuid.UUID, definition: MappingDefinition
) -> TransformationMappingVersion:
    manage_lock(db, context)
    return add_version(db, context, get_mapping(db, context, mapping_id), definition)


def activate(
    db: Session, context: TenantContext, mapping_id: uuid.UUID, version_id: uuid.UUID
) -> TransformationMapping:
    manage_lock(db, context)
    row = get_mapping(db, context, mapping_id)
    version = resolve_version(db, context, row, version_id)
    validate_definition(
        MappingDefinition.model_validate(version.definition), row.source_kind, row.entity_type
    )
    row.active_version = version.version
    audit(db, "mapping.activated", "mapping", row.id, context.tenant.id, context.identity.user.id)
    db.flush()
    return row


def identity(
    db: Session, context: TenantContext, connection: uuid.UUID, kind: str, external_id: str
) -> ExternalRecordMapping | None:
    return db.scalar(
        select(ExternalRecordMapping).where(
            ExternalRecordMapping.tenant_id == context.tenant.id,
            ExternalRecordMapping.connection_id == connection,
            ExternalRecordMapping.source_kind == kind,
            ExternalRecordMapping.external_id == external_id,
        )
    )


def get_record(
    db: Session, context: TenantContext, record_id: uuid.UUID, lock: bool = False
) -> CanonicalRecord:
    query = select(CanonicalRecord).where(
        CanonicalRecord.id == record_id, CanonicalRecord.tenant_id == context.tenant.id
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "Canonical record unavailable")
    return row


def link(db: Session, context: TenantContext, data: ExternalLink) -> ExternalRecordMapping:
    manage_lock(db, context)
    canonical = get_record(db, context, data.canonical_id, lock=True)
    connection = get_connection(db, context, data.connection_id)
    if SUPPORTED.get((connection.connector_type, data.source_kind)) != canonical.entity_type:
        raise HTTPException(422, "External identity entity mismatch")
    if identity(db, context, connection.id, data.source_kind, data.external_id):
        raise HTTPException(409, "External identity already linked")
    row = ExternalRecordMapping(
        tenant_id=context.tenant.id,
        connection_id=connection.id,
        source_kind=data.source_kind,
        external_id=data.external_id,
        canonical_id=canonical.id,
        entity_type=canonical.entity_type,
        provenance="MANUAL_LINK",
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "identity.linked",
        "external_identity",
        row.id,
        context.tenant.id,
        context.identity.user.id,
    )
    return row


def external_output(row: ExternalRecordMapping) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "connection_id",
            "source_kind",
            "external_id",
            "canonical_id",
            "entity_type",
            "last_source_updated_at",
            "provenance",
            "created_at",
        )
    }


def envelope(row: CanonicalRecord) -> CanonicalEnvelope:
    return CanonicalEnvelope(
        schema_version=1,
        entity_type=row.entity_type,
        canonical_id=row.id,
        tenant_id=row.tenant_id,
        source_connection_id=row.source_connection_id,
        source_external_id=row.source_external_id,
        source_updated_at=row.source_updated_at,
        normalized_at=row.normalized_at,
        correlation_id=row.correlation_id,
        normalized_data=row.normalized_data,
    )


def record_output(row: CanonicalRecord) -> dict:
    data = envelope(row).model_dump(mode="json")
    data["normalized_data"] = redact(data["normalized_data"])
    return {**data, "revision": row.revision, "field_provenance": row.field_provenance}


def attempt_output(row: TransformationAttempt) -> dict:
    result = {
        key: getattr(row, key)
        for key in (
            "id",
            "mapping_version_id",
            "event_id",
            "canonical_id",
            "entity_type",
            "connection_id",
            "source_kind",
            "external_id",
            "source_updated_at",
            "outcome",
            "issues",
            "correlation_id",
            "created_at",
        )
    }
    return {
        **result,
        "result_snapshot": redact(row.result_snapshot) if row.result_snapshot is not None else None,
    }


def authority_issues(
    db: Session,
    context: TenantContext,
    record: CanonicalRecord | None,
    data: dict,
    connection_type: str,
    connection_id: uuid.UUID,
    source_time: datetime | None,
) -> list[ValidationIssue]:
    entity_type = record.entity_type if record else None
    rules = dict(
        db.execute(
            select(FieldAuthorityRule.field, FieldAuthorityRule.owner).where(
                FieldAuthorityRule.tenant_id == context.tenant.id,
                FieldAuthorityRule.entity_type == entity_type,
            )
        ).all()
    )
    issues = []
    for field, value in data.items():
        if record and field in record.normalized_data and record.normalized_data[field] == value:
            continue
        owner = rules.get(field)
        if owner is None or owner == "REVIEW":
            issues.append(ValidationIssue(category="MANUAL_REVIEW", field=field))
            continue
        previous = record.field_provenance.get(field) if record else None
        if (
            owner != "ORIGIN"
            and owner != connection_type
            or owner == "ORIGIN"
            and previous
            and previous["connection_id"] != str(connection_id)
        ):
            issues.append(ValidationIssue(category="AUTHORITY_CONFLICT", field=field))
            continue
        if previous:
            old_time = (
                datetime.fromisoformat(previous["source_updated_at"])
                if previous.get("source_updated_at")
                else None
            )
            if source_time is None or old_time is None:
                issues.append(ValidationIssue(category="MANUAL_REVIEW", field=field))
            elif source_time < old_time:
                issues.append(ValidationIssue(category="STALE_UPDATE", field=field))
            elif source_time == old_time:
                issues.append(ValidationIssue(category="OUT_OF_ORDER", field=field))
    return issues


def evaluate(
    db: Session,
    context: TenantContext,
    mapping: TransformationMapping,
    version: TransformationMappingVersion,
    data: TransformInput,
    correlation: uuid.UUID,
) -> dict:
    connection = get_connection(db, context, mapping.connection_id)
    if connection.status != "ENABLED":
        raise HTTPException(409, "Source connection disabled")
    if SUPPORTED.get((connection.connector_type, mapping.source_kind)) != mapping.entity_type:
        raise HTTPException(422, "Unsupported mapping source")
    updated = normalize_time(data.source_updated_at) if data.source_updated_at else None
    try:
        definition = MappingDefinition.model_validate(version.definition)
        output, safe_source = transform(
            definition, mapping.source_kind, mapping.entity_type, data.payload
        )
    except (TransformationFailure, ValidationError) as error:
        issues = (
            error.issues
            if isinstance(error, TransformationFailure)
            else [ValidationIssue(category="INVALID_MAPPING")]
        )
        try:
            safe_source = source_payload(mapping.source_kind, data.payload)
        except TransformationFailure:
            safe_source = {}
        return {
            "outcome": "QUARANTINED",
            "issues": issues,
            "source": safe_source,
            "output": None,
            "canonical": None,
            "external": None,
            "updated": updated,
        }
    external = identity(db, context, mapping.connection_id, mapping.source_kind, data.external_id)
    canonical = get_record(db, context, external.canonical_id, lock=True) if external else None
    candidate_id = (
        canonical.id
        if canonical
        else uuid.uuid5(
            context.tenant.id, f"{mapping.connection_id}:{mapping.source_kind}:{data.external_id}"
        )
    )
    issues = []
    # Never use a mapping change or idempotency fingerprint to bypass source ordering.
    if external and external.last_source_updated_at is not None:
        if updated is None:
            issues.append(ValidationIssue(category="MANUAL_REVIEW"))
        elif updated < external.last_source_updated_at:
            issues.append(ValidationIssue(category="STALE_UPDATE"))
    proposed = digest({"source": safe_source})
    if (
        external
        and external.last_source_updated_at
        and updated == external.last_source_updated_at
        and external.last_payload_digest != proposed
    ):
        issues.append(ValidationIssue(category="OUT_OF_ORDER"))
    # A transient empty canonical record lets the exact same authority policy validate inserts.
    policy_record = canonical or CanonicalRecord(
        id=candidate_id,
        tenant_id=context.tenant.id,
        entity_type=mapping.entity_type,
        normalized_data={},
        field_provenance={},
    )
    issues.extend(
        authority_issues(
            db, context, policy_record, output, connection.connector_type, connection.id, updated
        )
    )
    outcome = (
        (
            "MANUAL_REVIEW"
            if any(issue.category == "MANUAL_REVIEW" for issue in issues)
            else "CONFLICT"
        )
        if issues
        else "SUCCEEDED"
    )
    if (
        not issues
        and canonical
        and all(
            canonical.normalized_data.get(k) == v and k in canonical.normalized_data
            for k, v in output.items()
        )
    ):
        outcome = "NO_CHANGE"
    representation = CanonicalEnvelope(
        schema_version=1,
        entity_type=mapping.entity_type,
        canonical_id=candidate_id,
        tenant_id=context.tenant.id,
        source_connection_id=connection.id,
        source_external_id=data.external_id,
        source_updated_at=updated,
        normalized_at=utcnow(),
        correlation_id=correlation,
        normalized_data=output,
    )
    return {
        "outcome": outcome,
        "issues": issues,
        "source": safe_source,
        "output": representation.model_dump(mode="json"),
        "canonical": canonical,
        "external": external,
        "updated": updated,
    }


def preview(
    db: Session,
    context: TenantContext,
    mapping_id: uuid.UUID,
    data: TransformInput,
    correlation: uuid.UUID,
) -> dict:
    manage_lock(db, context)
    mapping = get_mapping(db, context, mapping_id)
    version = resolve_version(db, context, mapping, data.mapping_version_id)
    result = evaluate(db, context, mapping, version, data, correlation)
    return {
        "mapping_version_id": version.id,
        "outcome": result["outcome"],
        "issues": [issue.model_dump() for issue in result["issues"]],
        "canonical": redact(result["output"]),
        "persisted": False,
    }


def normalize(
    db: Session,
    context: TenantContext,
    mapping_id: uuid.UUID,
    data: TransformInput,
    correlation: uuid.UUID,
    event_id: uuid.UUID | None = None,
) -> TransformationAttempt:
    manage_lock(db, context)
    mapping = get_mapping(db, context, mapping_id)
    version = resolve_version(db, context, mapping, data.mapping_version_id)
    # A bounded digest may describe invalid input, but never stores its unknown fields.
    try:
        raw_digest = digest(data.payload)
    except (TypeError, ValueError, RecursionError):
        raise HTTPException(422, "Invalid source payload") from None
    fingerprint = digest(
        {
            "mapping_version": str(version.id),
            "external_id": data.external_id,
            "time": normalize_time(data.source_updated_at).isoformat()
            if data.source_updated_at
            else None,
            "digest": raw_digest,
            "event_id": str(event_id) if event_id else None,
        }
    )
    previous = db.scalar(
        select(TransformationAttempt).where(
            TransformationAttempt.tenant_id == context.tenant.id,
            TransformationAttempt.fingerprint == fingerprint,
        )
    )
    if previous is not None:
        return previous
    result = evaluate(db, context, mapping, version, data, correlation)
    source_digest = digest({"source": result["source"]})
    row = TransformationAttempt(
        id=uuid.uuid4(),
        tenant_id=context.tenant.id,
        connection_id=mapping.connection_id,
        mapping_version_id=version.id,
        event_id=event_id,
        canonical_id=result["canonical"].id if result["canonical"] else None,
        entity_type=mapping.entity_type,
        source_kind=mapping.source_kind,
        external_id=data.external_id,
        source_updated_at=result["updated"],
        fingerprint=fingerprint,
        payload_digest=source_digest,
        outcome=result["outcome"],
        issues=[issue.model_dump() for issue in result["issues"]],
        source_snapshot=result["source"],
        result_snapshot=result["output"],
        correlation_id=correlation,
    )
    if row.outcome in {"SUCCEEDED", "NO_CHANGE"}:
        output = result["output"]
        canonical = result["canonical"]
        if canonical is None:
            canonical = CanonicalRecord(
                id=uuid.UUID(output["canonical_id"]),
                tenant_id=context.tenant.id,
                source_connection_id=mapping.connection_id,
                source_external_id=data.external_id,
                source_updated_at=result["updated"],
                entity_type=mapping.entity_type,
                normalized_data={},
                field_provenance={},
                normalized_at=utcnow(),
                correlation_id=correlation,
            )
            db.add(canonical)
            db.flush()
        fields = dict(canonical.normalized_data)
        provenance = dict(canonical.field_provenance)
        for field, value in output["normalized_data"].items():
            if field not in fields or fields[field] != value:
                provenance[field] = {
                    "connection_id": str(mapping.connection_id),
                    "mapping_version_id": str(version.id),
                    "source_updated_at": result["updated"].isoformat()
                    if result["updated"]
                    else None,
                    "attempt_id": str(row.id),
                }
                fields[field] = value
            elif (
                provenance.get(field, {}).get("connection_id") == str(mapping.connection_id)
                and result["updated"] is not None
                and (
                    not provenance[field].get("source_updated_at")
                    or result["updated"]
                    > datetime.fromisoformat(provenance[field]["source_updated_at"])
                )
            ):
                # A newer confirmation advances the owning field's high-water mark.
                provenance[field] = {
                    **provenance[field],
                    "source_updated_at": result["updated"].isoformat(),
                    "mapping_version_id": str(version.id),
                    "attempt_id": str(row.id),
                }
        if row.outcome == "SUCCEEDED" and result["canonical"] is not None:
            canonical.revision += 1
        canonical.normalized_data, canonical.field_provenance = fields, provenance
        canonical.normalized_at, canonical.correlation_id = utcnow(), correlation
        if canonical.source_connection_id == mapping.connection_id:
            canonical.source_updated_at = result["updated"]
        row.canonical_id = canonical.id
        external = result["external"]
        if external is None:
            external = ExternalRecordMapping(
                tenant_id=context.tenant.id,
                connection_id=mapping.connection_id,
                source_kind=mapping.source_kind,
                external_id=data.external_id,
                canonical_id=canonical.id,
                entity_type=mapping.entity_type,
            )
            db.add(external)
        external.last_source_updated_at, external.last_payload_digest = (
            result["updated"],
            source_digest,
        )
    db.add(row)
    db.flush()
    if row.outcome not in {"SUCCEEDED", "NO_CHANGE"}:
        db.add(
            QuarantinedRecord(
                tenant_id=context.tenant.id, connection_id=mapping.connection_id, attempt_id=row.id
            )
        )
    audit(
        db,
        "transformation.attempted",
        "transformation_attempt",
        row.id,
        context.tenant.id,
        context.identity.user.id,
    )
    db.flush()
    return row


def event_input(
    db: Session, context: TenantContext, event_id: uuid.UUID, version_id: uuid.UUID | None
) -> tuple[TransformationMapping, TransformInput]:
    event = db.scalar(
        select(IncomingEvent).where(
            IncomingEvent.id == event_id,
            IncomingEvent.tenant_id == context.tenant.id,
            IncomingEvent.ingestion_status == "ACCEPTED",
        )
    )
    if event is None:
        raise HTTPException(404, "Accepted event unavailable")
    kind = "candidate" if event.event_type.startswith("candidate.") else "requisition"
    mapping = db.scalar(
        select(TransformationMapping).where(
            TransformationMapping.tenant_id == context.tenant.id,
            TransformationMapping.connection_id == event.connection_id,
            TransformationMapping.source_kind == kind,
        )
    )
    if mapping is None:
        raise HTTPException(404, "Register a mapping for this event source")
    # Revalidate the exact preserved version-one inbox contract before transformation.
    from app.schemas.webhook import BusinessEvent

    source = BusinessEvent.model_validate(event.payload)
    return mapping, TransformInput(
        mapping_version_id=version_id,
        external_id=source.external_id,
        source_updated_at=source.occurred_at,
        payload={"data": source.data.model_dump(mode="json")},
    )


def reprocess(
    db: Session,
    context: TenantContext,
    invalid_id: uuid.UUID,
    version_id: uuid.UUID,
    correlation: uuid.UUID,
) -> TransformationAttempt:
    manage_lock(db, context)
    invalid = db.scalar(
        select(QuarantinedRecord)
        .where(QuarantinedRecord.id == invalid_id, QuarantinedRecord.tenant_id == context.tenant.id)
        .with_for_update()
    )
    if invalid is None:
        raise HTTPException(404, "Invalid record unavailable")
    previous = db.get(TransformationAttempt, invalid.attempt_id)
    version = db.scalar(
        select(TransformationMappingVersion).where(
            TransformationMappingVersion.id == version_id,
            TransformationMappingVersion.tenant_id == context.tenant.id,
            TransformationMappingVersion.connection_id == previous.connection_id,
        )
    )
    if (
        version is None
        or version.mapping_id
        != db.get(TransformationMappingVersion, previous.mapping_version_id).mapping_id
    ):
        raise HTTPException(404, "Mapping version unavailable")
    if version.id == previous.mapping_version_id:
        raise HTTPException(409, "Reprocessing requires a corrected mapping version")
    row = normalize(
        db,
        context,
        version.mapping_id,
        TransformInput(
            mapping_version_id=version.id,
            external_id=previous.external_id,
            source_updated_at=previous.source_updated_at,
            payload=previous.source_snapshot,
        ),
        correlation,
        previous.event_id,
    )
    if row.outcome in {"SUCCEEDED", "NO_CHANGE"}:
        invalid.status, invalid.resolution_attempt_id = "RESOLVED", row.id
    return row
