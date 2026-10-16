"""Ownership-aware planning and atomic local ATS effects using the existing engine."""

import uuid
from collections import Counter
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, assert_permission
from app.connectors.contracts import ConnectorOperation
from app.core.security import utcnow
from app.models.canonical import (
    CanonicalRecord,
    ExternalRecordMapping,
    FieldAuthorityRule,
    TransformationMapping,
    TransformationMappingVersion,
)
from app.models.integration import IntegrationConnection, SyntheticATSRecord, SyntheticCRMRecord
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
from app.models.workflow import WorkflowVersion
from app.schemas.canonical import MappingDefinition, TransformInput, normalize_time
from app.schemas.synchronization import (
    PlanPreview,
    ReconcileRequest,
    Resolution,
    SyncActivation,
    SyncCreate,
    SyncDefinition,
)
from app.schemas.workflow import WorkflowCreate, WorkflowVersionCreate
from app.services import canonical, integrations, workflows
from app.services.audit import record as audit
from app.services.webhooks import manage_lock
from app.synchronization.planner import calculate
from app.transformations.engine import TransformationFailure, transform


def scoped(db: Session, ctx: TenantContext, model, key: uuid.UUID):
    row = db.scalar(select(model).where(model.id == key, model.tenant_id == ctx.tenant.id))
    if row is None:
        raise HTTPException(404, "Synchronization resource unavailable")
    return row


def public(row) -> dict:
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
        if column.name != "tenant_id"
    }


def log(db: Session, ctx: TenantContext, action: str, row) -> None:
    audit(db, action, row.__tablename__, row.id, ctx.tenant.id, ctx.identity.user.id)


def validate(
    db: Session, ctx: TenantContext, cfg: SyncConfiguration, definition: SyncDefinition
) -> None:
    source = integrations.get_connection(db, ctx, cfg.source_connection_id)
    destination = integrations.get_connection(db, ctx, cfg.destination_connection_id)
    if source.connector_type != "synthetic_crm" or destination.connector_type != "synthetic_ats":
        raise HTTPException(422, "A synthetic CRM source and ATS destination are required")
    version = scoped(db, ctx, TransformationMappingVersion, definition.mapping_version_id)
    mapping = canonical.get_mapping(db, ctx, version.mapping_id)
    if mapping.connection_id != source.id or mapping.entity_type != "job_requisition":
        raise HTTPException(422, "Mapping must normalize this CRM requisition source")


def add_version(
    db: Session, ctx: TenantContext, cfg: SyncConfiguration, definition: SyncDefinition
) -> SyncConfigurationVersion:
    manage_lock(db, ctx)
    validate(db, ctx, cfg, definition)
    number = (
        db.scalar(
            select(func.max(SyncConfigurationVersion.version)).where(
                SyncConfigurationVersion.configuration_id == cfg.id
            )
        )
        or 0
    ) + 1
    row = SyncConfigurationVersion(
        configuration_id=cfg.id,
        tenant_id=ctx.tenant.id,
        version=number,
        mapping_version_id=definition.mapping_version_id,
        definition=definition.model_dump(mode="json"),
        definition_digest=canonical.digest(definition.model_dump(mode="json")),
        created_by=ctx.identity.user.id,
    )
    db.add(row)
    db.flush()
    log(db, ctx, "sync.version_created", row)
    return row


def create(db: Session, ctx: TenantContext, data: SyncCreate) -> SyncConfiguration:
    manage_lock(db, ctx)
    if db.scalar(
        select(SyncConfiguration.id).where(
            SyncConfiguration.tenant_id == ctx.tenant.id,
            SyncConfiguration.source_connection_id == data.source_connection_id,
            SyncConfiguration.destination_connection_id == data.destination_connection_id,
        )
    ):
        raise HTTPException(409, "A configuration already owns this source/destination route")
    cfg = SyncConfiguration(
        tenant_id=ctx.tenant.id,
        name=data.name,
        source_connection_id=data.source_connection_id,
        destination_connection_id=data.destination_connection_id,
    )
    validate(db, ctx, cfg, data.definition)
    db.add(cfg)
    db.flush()
    version = add_version(db, ctx, cfg, data.definition)
    cfg.active_version_id = version.id
    db.flush()
    log(db, ctx, "sync.configuration_created", cfg)
    return cfg


def workflow_definition(cfg: SyncConfiguration, version: SyncConfigurationVersion) -> dict:
    return {
        "schema_version": 1,
        "trigger": {
            "connection_id": str(cfg.source_connection_id),
            "source": "synthetic_crm",
            "event_types": version.definition["trigger_event_types"],
        },
        "steps": [
            {
                "id": "plan_sync",
                "type": "PLAN_CRM_ATS_SYNC",
                "config": {"configuration_version_id": str(version.id)},
            },
            {
                "id": "apply_sync",
                "type": "APPLY_CRM_ATS_SYNC",
                "config": {"plan_step_id": "plan_sync"},
                "retry": {"max_attempts": 5},
            },
            {
                "id": "record_sync",
                "type": "RECORD_SYNC_OUTCOME",
                "config": {"plan_step_id": "apply_sync"},
            },
        ],
    }


def activate(
    db: Session, ctx: TenantContext, cfg: SyncConfiguration, data: SyncActivation
) -> SyncConfiguration:
    manage_lock(db, ctx)
    db.refresh(cfg)
    if cfg.revision != data.expected_revision:
        raise HTTPException(409, "Configuration changed; refresh before activation")
    version = scoped(db, ctx, SyncConfigurationVersion, data.version_id or cfg.active_version_id)
    if version.configuration_id != cfg.id:
        raise HTTPException(404, "Version unavailable")
    validate(db, ctx, cfg, SyncDefinition.model_validate(version.definition))
    if data.enabled:
        definition = workflow_definition(cfg, version)
        if cfg.workflow_id is None:
            wf = workflows.create(
                db,
                ctx,
                WorkflowCreate(
                    name=f"Sync {cfg.id}",
                    description="Ownership-aware synthetic CRM to ATS synchronization",
                    definition=definition,
                ),
            )
            cfg.workflow_id = wf.id
            wfversion_id = db.scalar(
                select(WorkflowVersion.id).where(WorkflowVersion.workflow_id == wf.id)
            )
        else:
            wf = workflows.get_workflow(db, ctx, cfg.workflow_id)
            wfversion_id = workflows.add_version(
                db, ctx, wf, WorkflowVersionCreate(definition=definition).definition
            ).id
        workflows.activate(db, ctx, wf, True, wfversion_id)
    elif cfg.workflow_id:
        workflows.activate(db, ctx, workflows.get_workflow(db, ctx, cfg.workflow_id), False)
    cfg.active_version_id, cfg.enabled = version.id, data.enabled
    cfg.revision += 1
    log(
        db,
        ctx,
        "sync.configuration_activated" if data.enabled else "sync.configuration_deactivated",
        cfg,
    )
    db.flush()
    return cfg


def identities(
    db: Session, ctx: TenantContext, connection_id: uuid.UUID, canonical_id: uuid.UUID
) -> list[ExternalRecordMapping]:
    return list(
        db.scalars(
            select(ExternalRecordMapping).where(
                ExternalRecordMapping.tenant_id == ctx.tenant.id,
                ExternalRecordMapping.connection_id == connection_id,
                ExternalRecordMapping.source_kind == "requisition",
                ExternalRecordMapping.canonical_id == canonical_id,
            )
        )
    )


def destination(
    db: Session, ctx: TenantContext, connection_id: uuid.UUID, external_id: str | None
) -> SyntheticATSRecord | None:
    if external_id is None:
        return None
    return db.scalar(
        select(SyntheticATSRecord).where(
            SyntheticATSRecord.tenant_id == ctx.tenant.id,
            SyntheticATSRecord.connection_id == connection_id,
            SyntheticATSRecord.kind == "requisition",
            SyntheticATSRecord.external_id == external_id,
        )
    )


def destination_digest(row) -> str:
    return canonical.digest(
        {"id": str(row.id), "updated_at": row.updated_at.isoformat(), "data": row.payload}
        if row
        else None
    )


def authority(
    db: Session,
    ctx: TenantContext,
    record: CanonicalRecord | None,
    source_connection_id: uuid.UUID | None = None,
) -> dict:
    canonical.initialize_authority(db, ctx)
    owners = {
        r.field: r.owner
        for r in db.scalars(
            select(FieldAuthorityRule).where(
                FieldAuthorityRule.tenant_id == ctx.tenant.id,
                FieldAuthorityRule.entity_type == "job_requisition",
            )
        )
    }
    return {
        field: (
            "synthetic_crm"
            if owner == "ORIGIN"
            and (
                record is None
                or record.source_connection_id
                and record.field_provenance.get(field, {}).get("connection_id")
                == str(source_connection_id or record.source_connection_id)
            )
            else owner
        )
        for field, owner in owners.items()
    }


def source_input(
    db: Session,
    ctx: TenantContext,
    cfg: SyncConfiguration,
    version: SyncConfigurationVersion,
    request: PlanPreview,
) -> tuple[TransformationMapping, TransformInput]:
    if request.event_id:
        mapping, data = canonical.event_input(db, ctx, request.event_id, version.mapping_version_id)
        from app.models.webhook import IncomingEvent

        event = scoped(db, ctx, IncomingEvent, request.event_id)
        if (
            event.connection_id != cfg.source_connection_id
            or event.event_type not in version.definition["trigger_event_types"]
        ):
            raise HTTPException(422, "Event does not match configuration trigger")
        return mapping, data
    row = db.scalar(
        select(SyntheticCRMRecord).where(
            SyntheticCRMRecord.tenant_id == ctx.tenant.id,
            SyntheticCRMRecord.connection_id == cfg.source_connection_id,
            SyntheticCRMRecord.kind == "requisition",
            SyntheticCRMRecord.external_id == request.source_external_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Source record unavailable")

    mapping = canonical.get_mapping(
        db,
        ctx,
        scoped(db, ctx, TransformationMappingVersion, version.mapping_version_id).mapping_id,
    )
    return mapping, TransformInput(
        mapping_version_id=version.mapping_version_id,
        external_id=row.external_id,
        source_updated_at=row.updated_at,
        payload={"data": row.payload},
    )


def plan(
    db: Session,
    ctx: TenantContext,
    cfg: SyncConfiguration,
    version: SyncConfigurationVersion,
    request: PlanPreview,
    correlation: uuid.UUID,
    *,
    normalize: bool = False,
    workflow_execution_id: uuid.UUID | None = None,
    accept_destination: bool = False,
) -> tuple[SyncPlan, SyncExecution]:
    manage_lock(db, ctx)
    validate(db, ctx, cfg, SyncDefinition.model_validate(version.definition))
    if version.configuration_id != cfg.id:
        raise HTTPException(404, "Configuration version unavailable")
    mapping, data = source_input(db, ctx, cfg, version, request)
    source_link = canonical.identity(
        db, ctx, mapping.connection_id, "requisition", data.external_id
    )
    record = scoped(db, ctx, CanonicalRecord, source_link.canonical_id) if source_link else None
    stale = bool(
        record
        and record.source_updated_at
        and data.source_updated_at
        and data.source_updated_at < record.source_updated_at
    )
    source_record = current_source(db, ctx, cfg, data.external_id)
    if (
        request.event_id
        and source_record
        and data.source_updated_at
        and source_record.updated_at > data.source_updated_at
        and source_record.payload != data.payload["data"]
    ):
        stale = True
    invalid = False
    normalization_conflict = None
    try:
        normalized, _ = transform(
            MappingDefinition.model_validate(
                scoped(db, ctx, TransformationMappingVersion, version.mapping_version_id).definition
            ),
            "requisition",
            "job_requisition",
            data.payload,
        )
    except TransformationFailure:
        normalized, invalid = {}, True
    if normalize and not invalid:
        attempt = canonical.normalize(db, ctx, mapping.id, data, correlation, request.event_id)
        if attempt.outcome in {"SUCCEEDED", "NO_CHANGE"}:
            source_link = canonical.identity(
                db, ctx, mapping.connection_id, "requisition", data.external_id
            )
            record = scoped(db, ctx, CanonicalRecord, source_link.canonical_id)
        elif not stale:
            categories = {issue.get("category") for issue in attempt.issues}
            normalization_conflict = (
                "CONCURRENT_UPDATE"
                if "OUT_OF_ORDER" in categories
                else "FIELD_AUTHORITY_VIOLATION"
                if categories & {"AUTHORITY_CONFLICT", "MANUAL_REVIEW"}
                else "INVALID_MAPPING"
            )
            invalid = normalization_conflict == "INVALID_MAPPING"
    # Preview identity matches Phase 4's deterministic canonical identity without creating it.
    canonical_id = (
        record.id
        if record
        else uuid.uuid5(ctx.tenant.id, f"{mapping.connection_id}:requisition:{data.external_id}")
    )
    links = identities(db, ctx, cfg.destination_connection_id, canonical_id)
    external_id = links[0].external_id if len(links) == 1 else f"sync:{canonical_id.hex}"
    target = destination(db, ctx, cfg.destination_connection_id, external_id)
    candidates = []
    if (
        not links
        and version.definition["match_rule"] == "EXACT_TITLE_DEPARTMENT_LOCATION"
        and not invalid
    ):
        candidates = list(
            db.scalars(
                select(SyntheticATSRecord)
                .where(
                    SyntheticATSRecord.tenant_id == ctx.tenant.id,
                    SyntheticATSRecord.connection_id == cfg.destination_connection_id,
                    SyntheticATSRecord.kind == "requisition",
                    *(
                        SyntheticATSRecord.payload[k].as_string() == normalized.get(k)
                        for k in ("title", "department", "location")
                    ),
                )
                .order_by(SyntheticATSRecord.id)
                .limit(2)
            )
        )
        if len(candidates) == 1:
            target, external_id = candidates[0], candidates[0].external_id
    other_link = db.scalar(
        select(ExternalRecordMapping).where(
            ExternalRecordMapping.tenant_id == ctx.tenant.id,
            ExternalRecordMapping.connection_id == cfg.destination_connection_id,
            ExternalRecordMapping.source_kind == "requisition",
            ExternalRecordMapping.external_id == external_id,
            ExternalRecordMapping.canonical_id != canonical_id,
        )
    )
    previous = db.scalar(
        select(SyncOperation)
        .where(
            SyncOperation.tenant_id == ctx.tenant.id,
            SyncOperation.canonical_id == canonical_id,
            SyncOperation.destination_connection_id == cfg.destination_connection_id,
        )
        .order_by(SyncOperation.created_at.desc(), SyncOperation.id.desc())
        .limit(1)
    )
    uncertain_baseline = previous is not None and (
        previous.outcome not in {"CREATE", "UPDATE", "NO_OP"}
        or not isinstance(previous.result.get("destination_data"), dict)
    )
    baseline = (
        previous.result.get("destination_data") if previous and not uncertain_baseline else None
    )
    result = calculate(
        source=normalized,
        destination_data=target.payload if target else None,
        owners=authority(db, ctx, record, cfg.source_connection_id),
        fields=version.definition["destination_fields"],
        baseline=None if accept_destination else baseline,
        stale=stale,
        collision=len(links) > 1 or other_link is not None,
        ambiguous=len(candidates) > 1,
        allow_create=version.definition["create_enabled"],
        allow_update=version.definition["update_enabled"],
    )
    if (
        links
        and target
        and baseline is None
        and result["proposed_changes"]
        and result["decision"] == "UPDATE"
        and not accept_destination
    ):
        result.update(
            decision="CONFLICT",
            conflict_reasons=["DESTINATION_CHANGED"],
            explanation="An existing mapped ATS record has no trusted synchronization baseline; review before writing.",
        )
    if invalid:
        result.update(
            decision="INVALID",
            conflict_reasons=["INVALID_MAPPING"],
            explanation="Source normalization requires review; no ATS write is eligible.",
        )
    if (target is not None or previous is not None) and not links and not candidates:
        result.update(
            decision="CONFLICT",
            conflict_reasons=sorted(set(result["conflict_reasons"] + ["IDENTITY_COLLISION"])),
            explanation="Destination identity evidence is missing or an unmapped ATS record occupies its stable ID; explicit identity review is required.",
        )
    if uncertain_baseline:
        result.update(
            decision="CONFLICT",
            conflict_reasons=sorted(set(result["conflict_reasons"] + ["UNKNOWN_WRITE_OUTCOME"])),
            explanation="An uncertain prior action requires review; automatic destination writes are blocked.",
        )
    if normalization_conflict and not invalid:
        result.update(
            decision="CONFLICT",
            conflict_reasons=sorted(set(result["conflict_reasons"] + [normalization_conflict])),
            explanation="Canonical governance rejected the source change; destination writes are held.",
        )
    if links and target is None:
        result.update(
            decision="CONFLICT",
            conflict_reasons=sorted(set(result["conflict_reasons"] + ["IDENTITY_COLLISION"])),
            explanation="Mapped destination is absent; review the stale identity before writing.",
        )
    result.update(
        source_identity={
            "connection_id": str(cfg.source_connection_id),
            "external_id": data.external_id,
            "canonical_id": str(canonical_id),
        },
        destination_identity={
            "connection_id": str(cfg.destination_connection_id),
            "external_id": external_id,
            "record_id": str(target.id) if target else None,
        },
        source_data=normalized,
        source_record_digest=destination_digest(source_record),
        source_record_updated_at=source_record.updated_at.isoformat() if source_record else None,
        source_updated_at=normalize_time(data.source_updated_at).isoformat()
        if data.source_updated_at
        else None,
        destination_digest=destination_digest(target),
        destination_updated_at=target.updated_at.isoformat() if target else None,
        mapping_version_id=str(version.mapping_version_id),
        configuration_version_id=str(version.id),
        candidate_ids=[str(r.id) for r in candidates[:10]],
    )
    from app.schemas.synchronization import PlanEvidence

    result = PlanEvidence.model_validate(result).model_dump(mode="json")
    digest = canonical.digest(result)
    row = SyncPlan(
        tenant_id=ctx.tenant.id,
        configuration_id=cfg.id,
        version_id=version.id,
        source_connection_id=cfg.source_connection_id,
        event_id=request.event_id,
        canonical_id=record.id if record else None,
        correlation_id=correlation,
        decision=result["decision"],
        digest=digest,
        evidence=result,
    )
    db.add(row)
    db.flush()
    key = canonical.digest(
        {
            "purpose": "execute" if normalize else "preview",
            "version": str(version.id),
            "source": data.external_id,
            "source_time": result["source_updated_at"],
            "source_data": normalized,
            "destination": result["destination_digest"],
            "decision": result["decision"],
        }
    )
    execution = db.scalar(
        select(SyncExecution).where(
            SyncExecution.tenant_id == ctx.tenant.id, SyncExecution.business_key == key
        )
    )
    if execution is None:
        execution = SyncExecution(
            tenant_id=ctx.tenant.id,
            configuration_id=cfg.id,
            plan_id=row.id,
            workflow_execution_id=workflow_execution_id,
            business_key=key,
            state="HELD" if row.decision in {"CONFLICT", "INVALID"} else "PLANNED",
        )
        db.add(execution)
        db.flush()
        for category in result["conflict_reasons"]:
            db.add(
                SyncConflict(
                    tenant_id=ctx.tenant.id,
                    plan_id=row.id,
                    execution_id=execution.id,
                    category=category,
                    evidence=result,
                )
            )
    if execution.state == "HELD" and workflow_execution_id:
        from app.services.notifications import emit_sync

        emit_sync(db, ctx, row, execution, conflict=True)
    log(db, ctx, "sync.plan_created", row)
    db.flush()
    return row, execution


def hold(
    db: Session, ctx: TenantContext, row: SyncPlan, execution: SyncExecution, category: str
) -> None:
    execution.state = "HELD"
    if not db.scalar(
        select(SyncConflict.id).where(
            SyncConflict.plan_id == row.id, SyncConflict.category == category
        )
    ):
        db.add(
            SyncConflict(
                tenant_id=ctx.tenant.id,
                plan_id=row.id,
                execution_id=execution.id,
                category=category,
                evidence=row.evidence,
            )
        )
    log(db, ctx, "sync.operation_held", execution)
    if execution.workflow_execution_id:
        from app.services.notifications import emit_sync

        emit_sync(db, ctx, row, execution, conflict=True)


def apply(
    db: Session,
    ctx: TenantContext,
    row: SyncPlan,
    execution: SyncExecution,
    *,
    attempt: int = 1,
    deadline: int = 5,
) -> SyncExecution:
    manage_lock(db, ctx)
    db.refresh(execution)
    if execution.state != "PLANNED":
        return execution
    cfg = scoped(db, ctx, SyncConfiguration, row.configuration_id)
    if not cfg.enabled or cfg.active_version_id != row.version_id:
        hold(db, ctx, row, execution, "CONCURRENT_UPDATE")
        return execution
    if row.decision in {"CONFLICT", "INVALID"}:
        execution.state = "HELD"
        return execution
    if row.decision == "SKIP":
        execution.state = "SKIPPED"
        return execution
    if row.canonical_id is None:
        raise HTTPException(409, "Preview plans cannot execute; dispatch a verified event")
    # Same tenant->connection locking order as all simulator mutations.
    db.scalar(
        select(IntegrationConnection)
        .where(
            IntegrationConnection.id == cfg.destination_connection_id,
            IntegrationConnection.tenant_id == ctx.tenant.id,
        )
        .with_for_update()
    )
    evidence = row.evidence
    external_id = evidence["destination_identity"]["external_id"]
    target = destination(db, ctx, cfg.destination_connection_id, external_id)
    record = scoped(db, ctx, CanonicalRecord, row.canonical_id)
    source_record = current_source(db, ctx, cfg, evidence["source_identity"]["external_id"])
    if destination_digest(source_record) != evidence["source_record_digest"]:
        hold(db, ctx, row, execution, "CONCURRENT_UPDATE")
        return execution
    if (
        record.source_updated_at
        and evidence["source_updated_at"]
        and record.source_updated_at != datetime.fromisoformat(evidence["source_updated_at"])
    ):
        hold(db, ctx, row, execution, "STALE_SOURCE")
        return execution
    owners = authority(db, ctx, record, cfg.source_connection_id)
    if any(owners.get(field) != "synthetic_crm" for field in evidence["authority_decisions"]):
        hold(db, ctx, row, execution, "FIELD_AUTHORITY_VIOLATION")
        return execution
    action_key = "sync:" + execution.business_key
    existing = db.scalar(
        select(SyncOperation).where(
            SyncOperation.tenant_id == ctx.tenant.id, SyncOperation.action_key == action_key
        )
    )
    if existing:
        if (
            existing.execution_id != execution.id
            or existing.outcome not in {"CREATE", "UPDATE", "NO_OP"}
            or not isinstance(existing.result.get("destination_data"), dict)
        ):
            hold(db, ctx, row, execution, "UNKNOWN_WRITE_OUTCOME")
            return execution
        execution.state = "SUCCEEDED"
        return execution
    if destination_digest(target) != evidence["destination_digest"]:
        hold(db, ctx, row, execution, "CONCURRENT_UPDATE")
        return execution
    links = identities(db, ctx, cfg.destination_connection_id, record.id)
    other = db.scalar(
        select(ExternalRecordMapping).where(
            ExternalRecordMapping.tenant_id == ctx.tenant.id,
            ExternalRecordMapping.connection_id == cfg.destination_connection_id,
            ExternalRecordMapping.source_kind == "requisition",
            ExternalRecordMapping.external_id == external_id,
            ExternalRecordMapping.canonical_id != record.id,
        )
    )
    if other or len(links) > 1 or links and links[0].external_id != external_id:
        hold(db, ctx, row, execution, "IDENTITY_COLLISION")
        return execution
    data = dict(target.payload) if target else {"status": "OPEN"}
    data.update({key: value["after"] for key, value in evidence["proposed_changes"].items()})
    response = None
    if row.decision != "NO_OP":
        response = integrations.execute(
            db,
            ctx,
            cfg.destination_connection_id,
            ConnectorOperation(
                name="requisition.upsert", payload={"external_id": external_id, "data": data}
            ),
            correlation_id=row.correlation_id,
            workflow_execution_id=execution.workflow_execution_id,
            attempt=attempt,
            deadline_seconds=deadline,
        ).model_dump(mode="json")
    if not links:
        db.add(
            ExternalRecordMapping(
                tenant_id=ctx.tenant.id,
                connection_id=cfg.destination_connection_id,
                source_kind="requisition",
                external_id=external_id,
                canonical_id=record.id,
                entity_type="job_requisition",
                provenance="MANUAL_LINK",
            )
        )
    db.add(
        SyncOperation(
            tenant_id=ctx.tenant.id,
            execution_id=execution.id,
            plan_id=row.id,
            canonical_id=record.id,
            destination_connection_id=cfg.destination_connection_id,
            action_key=action_key,
            outcome=row.decision,
            result={
                "destination_data": data,
                "provider_response": response,
                "source_updated_at": evidence["source_updated_at"],
                "synthetic": True,
            },
        )
    )
    execution.state = "SUCCEEDED"
    log(db, ctx, "sync.operation_applied", execution)
    from app.services.notifications import emit_sync

    emit_sync(db, ctx, row, execution)
    db.flush()
    return execution


def checkpoint(row: SyncPlan, execution: SyncExecution) -> dict:
    return {
        "plan_id": str(row.id),
        "execution_id": str(execution.id),
        "decision": row.decision,
        "state": execution.state,
    }


def current_source(
    db: Session, ctx: TenantContext, cfg: SyncConfiguration, external_id: str
) -> SyntheticCRMRecord | None:
    return db.scalar(
        select(SyntheticCRMRecord).where(
            SyntheticCRMRecord.tenant_id == ctx.tenant.id,
            SyntheticCRMRecord.connection_id == cfg.source_connection_id,
            SyntheticCRMRecord.kind == "requisition",
            SyntheticCRMRecord.external_id == external_id,
        )
    )


def enqueue_reviewed_plan(
    db: Session,
    ctx: TenantContext,
    cfg: SyncConfiguration,
    version: SyncConfigurationVersion,
    row: SyncPlan,
    execution: SyncExecution,
    source_event_id: uuid.UUID | None,
) -> None:
    """Seed a reviewed plan checkpoint; the existing worker owns all apply/retry/fencing."""
    from app.models.webhook import IncomingEvent
    from app.models.workflow import WorkflowExecution, WorkflowStepExecution
    from app.schemas.workflow import WorkflowJSON

    if execution.state != "PLANNED" or execution.workflow_execution_id is not None:
        return
    if not cfg.enabled or cfg.workflow_id is None:
        raise HTTPException(
            409, "Activate the synchronization route before queueing a reviewed plan"
        )
    event = (
        scoped(db, ctx, IncomingEvent, source_event_id)
        if source_event_id
        else db.scalar(
            select(IncomingEvent)
            .where(
                IncomingEvent.tenant_id == ctx.tenant.id,
                IncomingEvent.connection_id == cfg.source_connection_id,
                IncomingEvent.ingestion_status == "ACCEPTED",
                IncomingEvent.payload["external_id"].as_string()
                == row.evidence["source_identity"]["external_id"],
            )
            .order_by(IncomingEvent.received_at.desc())
            .limit(1)
        )
    )
    if (
        event is None
        or event.connection_id != cfg.source_connection_id
        or event.ingestion_status != "ACCEPTED"
    ):
        raise HTTPException(
            409, "A verified source event is required before executing a reviewed plan"
        )
    definition = WorkflowJSON.model_validate(workflow_definition(cfg, version))
    workflow = workflows.get_workflow(db, ctx, cfg.workflow_id)
    wfversion = workflows.add_version(db, ctx, workflow, definition)
    job_id = uuid.uuid4()
    job = WorkflowExecution(
        id=job_id,
        root_id=job_id,
        tenant_id=ctx.tenant.id,
        workflow_id=workflow.id,
        version_id=wfversion.id,
        event_id=event.id,
        connection_id=cfg.source_connection_id,
        principal_id=ctx.identity.user.id,
        correlation_id=row.correlation_id,
        next_run_at=utcnow(),
        cursor=1,
    )
    db.add(job)
    db.flush()
    for position, step in enumerate(definition.steps):
        db.add(
            WorkflowStepExecution(
                tenant_id=ctx.tenant.id,
                execution_id=job.id,
                step_id=step.id,
                position=position,
                step_type=step.type,
                state="SUCCEEDED" if position == 0 else "PENDING",
                output=checkpoint(row, execution) if position == 0 else None,
            )
        )
    execution.workflow_execution_id = job.id
    workflows.history(db, job, "DISPATCHED")
    log(db, ctx, "sync.reviewed_plan_queued", execution)
    db.flush()


def resolve(
    db: Session,
    ctx: TenantContext,
    conflict: SyncConflict,
    data: Resolution,
    correlation: uuid.UUID,
) -> SyncResolution:
    assert_permission(ctx, "sync:resolve")
    # Review-only operations roles may never run a connector mutation or link identities.
    if data.action in {
        "ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS",
        "RETRY_AFTER_REFRESH",
        "LINK_EXISTING_IDENTITY",
    }:
        assert_permission(ctx, "integrations:manage")
        manage_lock(db, ctx)
    else:
        workflows.recover_lock(db, ctx)
    prior = db.scalar(
        select(SyncResolution).where(
            SyncResolution.tenant_id == ctx.tenant.id,
            SyncResolution.request_key == data.request_key,
        )
    )
    if prior:
        if (
            prior.conflict_id != conflict.id
            or prior.action != data.action
            or prior.reason != data.reason
        ):
            raise HTTPException(409, "Resolution key already used")
        return prior
    db.refresh(conflict)
    if conflict.status != "OPEN" or conflict.revision != data.expected_revision:
        raise HTTPException(409, "Conflict changed; refresh before review")
    old = scoped(db, ctx, SyncPlan, conflict.plan_id)
    cfg = scoped(db, ctx, SyncConfiguration, old.configuration_id)
    target = destination(
        db, ctx, cfg.destination_connection_id, old.evidence["destination_identity"]["external_id"]
    )
    if destination_digest(target) != data.expected_destination_digest:
        raise HTTPException(409, "Destination changed; review current state")
    source = current_source(db, ctx, cfg, old.evidence["source_identity"]["external_id"])
    if destination_digest(source) != data.expected_source_digest:
        raise HTTPException(409, "Source changed; review current source before resolution")
    new_plan = None
    if data.action in {
        "ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS",
        "RETRY_AFTER_REFRESH",
        "LINK_EXISTING_IDENTITY",
    }:
        version = scoped(db, ctx, SyncConfigurationVersion, cfg.active_version_id)
        if data.action == "LINK_EXISTING_IDENTITY":
            if old.canonical_id is None or not data.destination_external_id:
                raise HTTPException(422, "A canonical identity and destination ID are required")
            linked = destination(
                db, ctx, cfg.destination_connection_id, data.destination_external_id
            )
            if destination_digest(linked) != data.expected_link_digest:
                raise HTTPException(409, "Review the selected destination identity before linking")
            if linked is None or identities(
                db, ctx, cfg.destination_connection_id, old.canonical_id
            ):
                raise HTTPException(409, "Identity cannot be linked safely")
            from app.schemas.canonical import ExternalLink

            canonical.link(
                db,
                ctx,
                ExternalLink(
                    connection_id=cfg.destination_connection_id,
                    source_kind="requisition",
                    external_id=data.destination_external_id,
                    canonical_id=old.canonical_id,
                ),
            )
        # Refresh from current CRM; never replay an old event over newer source state.
        new_plan, execution = plan(
            db,
            ctx,
            cfg,
            version,
            PlanPreview(source_external_id=old.evidence["source_identity"]["external_id"]),
            correlation,
            normalize=True,
            accept_destination=data.action == "ACCEPT_SOURCE_FOR_AUTHORIZED_FIELDS",
        )
        if new_plan.decision in {"CONFLICT", "INVALID"}:
            raise HTTPException(409, "Current authority or identity still requires review")
        if new_plan.decision == "SKIP":
            execution.state = "SKIPPED"
        else:
            enqueue_reviewed_plan(db, ctx, cfg, version, new_plan, execution, old.event_id)
    conflict.status, conflict.revision = "RESOLVED", conflict.revision + 1
    result = SyncResolution(
        tenant_id=ctx.tenant.id,
        conflict_id=conflict.id,
        plan_id=new_plan.id if new_plan else None,
        action=data.action,
        reason=data.reason,
        actor_id=ctx.identity.user.id,
        request_key=data.request_key,
    )
    db.add(result)
    db.flush()
    log(db, ctx, "sync.conflict_resolved", result)
    return result


def reconcile(
    db: Session, ctx: TenantContext, cfg: SyncConfiguration, request: ReconcileRequest
) -> ReconciliationRun:
    manage_lock(db, ctx)
    version = scoped(db, ctx, SyncConfigurationVersion, cfg.active_version_id)
    run = ReconciliationRun(
        tenant_id=ctx.tenant.id,
        configuration_id=cfg.id,
        version_id=version.id,
        started_by=ctx.identity.user.id,
        offset=request.offset,
        limit=request.limit,
        summary={},
        completed_at=utcnow(),
    )
    db.add(run)
    db.flush()
    counts = Counter()

    def finding(category: str, external_id: str | None, evidence: dict):
        counts[category] += 1
        db.add(
            ReconciliationFinding(
                tenant_id=ctx.tenant.id,
                run_id=run.id,
                category=category,
                source_external_id=external_id,
                evidence=evidence,
            )
        )

    sources = list(
        db.scalars(
            select(SyntheticCRMRecord)
            .where(
                SyntheticCRMRecord.tenant_id == ctx.tenant.id,
                SyntheticCRMRecord.connection_id == cfg.source_connection_id,
                SyntheticCRMRecord.kind == "requisition",
            )
            .order_by(SyntheticCRMRecord.id)
            .offset(request.offset)
            .limit(request.limit)
        )
    )
    for source in sources:
        link = canonical.identity(
            db, ctx, cfg.source_connection_id, "requisition", source.external_id
        )
        linked = (
            identities(db, ctx, cfg.destination_connection_id, link.canonical_id) if link else []
        )
        targets = [
            destination(db, ctx, cfg.destination_connection_id, l.external_id) for l in linked
        ]
        target = targets[0] if len(targets) == 1 else None
        try:
            normalized, _ = transform(
                MappingDefinition.model_validate(
                    scoped(
                        db, ctx, TransformationMappingVersion, version.mapping_version_id
                    ).definition
                ),
                "requisition",
                "job_requisition",
                {"data": source.payload},
            )
        except TransformationFailure:
            finding("INVALID_MAPPING", source.external_id, {"source_record_id": str(source.id)})
            continue
        record = scoped(db, ctx, CanonicalRecord, link.canonical_id) if link else None
        owners = authority(db, ctx, record, cfg.source_connection_id)
        authorized = {
            k for k in version.definition["destination_fields"] if owners.get(k) == "synthetic_crm"
        }
        if len(authorized) != len(version.definition["destination_fields"]):
            finding("AUTHORITY_REVIEW", source.external_id, {"authority": owners})
        if not linked and version.definition["match_rule"] == "EXACT_TITLE_DEPARTMENT_LOCATION":
            candidates = list(
                db.scalars(
                    select(SyntheticATSRecord)
                    .where(
                        SyntheticATSRecord.tenant_id == ctx.tenant.id,
                        SyntheticATSRecord.connection_id == cfg.destination_connection_id,
                        SyntheticATSRecord.kind == "requisition",
                        *(
                            SyntheticATSRecord.payload[k].as_string() == normalized.get(k)
                            for k in ("title", "department", "location")
                        ),
                    )
                    .order_by(SyntheticATSRecord.id)
                    .limit(2)
                )
            )
            if candidates:
                finding(
                    "DUPLICATE_CANDIDATES" if len(candidates) > 1 else "UNLINKED_CANDIDATE",
                    source.external_id,
                    {"candidate_ids": [str(c.id) for c in candidates]},
                )
        if len(linked) > 1:
            finding(
                "DUPLICATE_CANDIDATES",
                source.external_id,
                {"mapping_ids": [str(l.id) for l in linked]},
            )
        elif linked and target is None:
            finding("STALE_MAPPING", source.external_id, {"external_id": linked[0].external_id})
        elif target is None:
            finding("MISSING_DESTINATION", source.external_id, {"source_record_id": str(source.id)})
        elif any(normalized.get(k) != target.payload.get(k) for k in authorized):
            finding(
                "DIVERGENT_AUTHORIZED_FIELDS",
                source.external_id,
                {
                    "differences": {
                        k: {"source": normalized.get(k), "destination": target.payload.get(k)}
                        for k in sorted(authorized)
                        if normalized.get(k) != target.payload.get(k)
                    }
                },
            )
    # Independently bounded identity/conflict pages make incomplete scans explicit.
    for link in db.scalars(
        select(ExternalRecordMapping)
        .where(
            ExternalRecordMapping.tenant_id == ctx.tenant.id,
            ExternalRecordMapping.connection_id == cfg.destination_connection_id,
            ExternalRecordMapping.source_kind == "requisition",
        )
        .order_by(ExternalRecordMapping.id)
        .offset(request.offset)
        .limit(request.limit)
    ):
        source_identity = db.scalar(
            select(ExternalRecordMapping.id).where(
                ExternalRecordMapping.tenant_id == ctx.tenant.id,
                ExternalRecordMapping.connection_id == cfg.source_connection_id,
                ExternalRecordMapping.canonical_id == link.canonical_id,
            )
        )
        if not source_identity:
            finding("ORPHANED_EXTERNAL_IDENTITY", None, {"mapping_id": str(link.id)})
    for conflict in db.scalars(
        select(SyncConflict)
        .join(SyncPlan, SyncPlan.id == SyncConflict.plan_id)
        .where(
            SyncConflict.tenant_id == ctx.tenant.id,
            SyncPlan.configuration_id == cfg.id,
            SyncConflict.status == "OPEN",
        )
        .order_by(SyncConflict.id)
        .offset(request.offset)
        .limit(request.limit)
    ):
        finding(
            "UNRESOLVED_CONFLICT",
            None,
            {"conflict_id": str(conflict.id), "category": conflict.category},
        )
    run.summary = {
        "counts": dict(sorted(counts.items())),
        "finding_count": sum(counts.values()),
        "source_records_scanned": len(sources),
        "read_only": True,
        "bounded_page": True,
        "offset": request.offset,
        "limit": request.limit,
    }
    log(db, ctx, "sync.reconciliation_completed", run)
    db.flush()
    return run
