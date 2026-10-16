"""Repeatable canonical demo fixtures, called by the existing operator seed."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext
from app.models.canonical import (
    ExternalRecordMapping,
    TransformationMapping,
    TransformationMappingVersion,
)
from app.models.integration import SyntheticATSRecord, SyntheticCRMRecord
from app.schemas.canonical import ExternalLink, MappingCreate, TransformInput
from app.services import canonical as service

FIXTURE_TIME = datetime(2030, 1, 15, 12, tzinfo=UTC)


def seed_canonical(db: Session, context: TenantContext, connections: dict) -> None:
    from app.connectors.contracts import ConnectorType

    mappings = {}
    for source, source_kind in [
        (ConnectorType.CRM, "contact"),
        (ConnectorType.CRM, "requisition"),
        (ConnectorType.ATS, "candidate"),
        (ConnectorType.ATS, "requisition"),
    ]:
        row = db.scalar(
            select(TransformationMapping).where(
                TransformationMapping.tenant_id == context.tenant.id,
                TransformationMapping.connection_id == connections[source],
                TransformationMapping.source_kind == source_kind,
            )
        )
        if row is None:
            row = service.create_mapping(
                db,
                context,
                MappingCreate(
                    connection_id=connections[source],
                    source_kind=source_kind,
                    name=f"Demo {source.value.removeprefix('synthetic_')} {source_kind}",
                ),
            )
        mappings[source, source_kind] = row
        version = db.scalar(
            select(TransformationMappingVersion).where(
                TransformationMappingVersion.mapping_id == row.id,
                TransformationMappingVersion.version == 1,
            )
        )
        model = SyntheticCRMRecord if source == ConnectorType.CRM else SyntheticATSRecord
        records = db.scalars(
            select(model).where(
                model.tenant_id == context.tenant.id,
                model.connection_id == connections[source],
                model.kind == source_kind,
                model.external_id.in_(
                    [f"demo-job-{index}" for index in range(1, 3)]
                    if source_kind == "requisition"
                    else [f"demo-{source_kind}-{index}" for index in range(1, 4)]
                ),
            )
        )
        for record in records:
            service.normalize(
                db,
                context,
                row.id,
                TransformInput(
                    mapping_version_id=version.id,
                    external_id=record.external_id,
                    source_updated_at=FIXTURE_TIME,
                    payload={"data": record.payload},
                ),
                uuid.uuid4(),
            )
    crm = mappings[ConnectorType.CRM, "contact"]
    source_version = service.resolve_version(db, context, crm)
    service.normalize(
        db,
        context,
        crm.id,
        TransformInput(
            mapping_version_id=source_version.id,
            external_id="demo-invalid-contact-v1",
            source_updated_at=FIXTURE_TIME,
            payload={
                "data": {
                    "name": "",
                    "email": "invalid-synthetic-address",
                    "company": "Fictional Harbor",
                }
            },
        ),
        uuid.uuid4(),
    )
    crm_job = db.scalar(
        select(ExternalRecordMapping).where(
            ExternalRecordMapping.tenant_id == context.tenant.id,
            ExternalRecordMapping.connection_id == connections[ConnectorType.CRM],
            ExternalRecordMapping.source_kind == "requisition",
            ExternalRecordMapping.external_id == "demo-job-1",
        )
    )
    alias = service.identity(
        db, context, connections[ConnectorType.ATS], "requisition", "demo-authority-conflict-v1"
    )
    if alias is None and crm_job is not None:
        service.link(
            db,
            context,
            ExternalLink(
                canonical_id=crm_job.canonical_id,
                connection_id=connections[ConnectorType.ATS],
                source_kind="requisition",
                external_id="demo-authority-conflict-v1",
            ),
        )
    if crm_job is not None:
        ats = mappings[ConnectorType.ATS, "requisition"]
        job = db.scalar(
            select(SyntheticCRMRecord).where(
                SyntheticCRMRecord.connection_id == connections[ConnectorType.CRM],
                SyntheticCRMRecord.external_id == "demo-job-1",
                SyntheticCRMRecord.kind == "requisition",
            )
        )
        service.normalize(
            db,
            context,
            ats.id,
            TransformInput(
                external_id="demo-authority-conflict-v1",
                source_updated_at=FIXTURE_TIME,
                payload={"data": {**job.payload, "commercial_rate": "999.00"}},
            ),
            uuid.uuid4(),
        )
