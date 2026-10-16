"""Idempotent plans and engine-run demonstrations; no fabricated execution states."""

import json
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import Identity, authorized_context
from app.connectors.contracts import ConnectorType
from app.connectors.webhook_sender import signed_request
from app.core.config import Settings
from app.core.webhook_security import unseal
from app.models.canonical import TransformationMapping, TransformationMappingVersion
from app.models.identity import User
from app.models.integration import IntegrationConnection, SyntheticCRMRecord
from app.models.tenant import Tenant
from app.models.webhook import WebhookEndpoint, WebhookSecretVersion
from app.models.workflow import WorkerLease, WorkflowDefinition, WorkflowExecution
from app.schemas.webhook import EndpointCreate
from app.schemas.workflow import WorkflowCreate, WorkflowJSON
from app.services import webhooks, workflows
from app.workflows.engine import claim, dispatch, register_worker, run_claim


def seed_workflows(db: Session, context, connections: dict) -> None:
    crm = connections[ConnectorType.CRM]
    mapping = db.scalar(
        select(TransformationMapping).where(
            TransformationMapping.connection_id == crm,
            TransformationMapping.tenant_id == context.tenant.id,
            TransformationMapping.source_kind == "requisition",
        )
    )
    version = db.scalar(
        select(TransformationMappingVersion).where(
            TransformationMappingVersion.mapping_id == mapping.id,
            TransformationMappingVersion.version == mapping.active_version,
        )
    )
    job = db.scalar(
        select(SyntheticCRMRecord).where(
            SyntheticCRMRecord.connection_id == crm, SyntheticCRMRecord.external_id == "demo-job-1"
        )
    )
    for retry_demo in [False, True]:
        name = "Demo job retry and failure" if retry_demo else "Demo job observation"
        existing = db.scalar(
            select(WorkflowDefinition).where(
                WorkflowDefinition.tenant_id == context.tenant.id, WorkflowDefinition.name == name
            )
        )
        if existing:
            continue  # Do not overwrite or reactivate operator-edited definitions.
        plan = WorkflowJSON.model_validate(
            {
                "schema_version": 1,
                "trigger": {
                    "connection_id": str(crm),
                    "source": "synthetic_crm",
                    "event_types": ["job.created" if retry_demo else "job.updated"],
                    "event_id_prefix": "demo-workflow-",
                },
                "steps": [
                    {
                        "id": "normalize",
                        "type": "NORMALIZE_EVENT",
                        "config": {"mapping_version_id": str(version.id)},
                    },
                    {
                        "id": "condition",
                        "type": "CONDITION",
                        "config": {
                            "reference": {
                                "source": "step",
                                "step_id": "normalize",
                                "path": "normalized_data.title",
                            },
                            "operator": "EXISTS",
                        },
                    },
                    {
                        "id": "observe",
                        "type": "MOCK_CONNECTOR_ACTION",
                        "config": {
                            "connection_id": str(crm),
                            "operation": "requisition.get",
                            "payload": {"id": str(job.id)},
                            "fault": {
                                "mode": "TRANSIENT" if retry_demo else "NONE",
                                "fail_first_attempts": 1,
                                "permanent_event_ids": ["demo-workflow-terminal"],
                            },
                        },
                        "retry": {"jitter_fraction": 0},
                    },
                    {
                        "id": "result",
                        "type": "RECORD_RESULT",
                        "config": {
                            "summary": "Synthetic CRM job observed",
                            "outcome": "OBSERVED",
                            "canonical_id": {
                                "source": "step",
                                "step_id": "normalize",
                                "path": "canonical_id",
                            },
                        },
                    },
                ],
            }
        )
        row = workflows.create(db, context, WorkflowCreate(name=name, definition=plan))
        workflows.activate(db, context, row, True)


def run_demo(engine, email: str) -> dict:
    settings = Settings()
    with Session(engine) as db, db.begin():
        user = db.scalar(select(User).where(User.email == email.lower()))
        for tenant in db.scalars(
            select(Tenant).where(Tenant.slug.in_(["demo-recruiting-alpha", "demo-recruiting-beta"]))
        ):
            context = authorized_context(db, Identity(user, None), tenant.id)
            crm = db.scalar(
                select(IntegrationConnection).where(
                    IntegrationConnection.tenant_id == tenant.id,
                    IntegrationConnection.name == "Demo CRM",
                )
            )
            endpoint = db.scalar(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.tenant_id == tenant.id, WebhookEndpoint.connection_id == crm.id
                )
            )
            if endpoint is None:
                disclosure = webhooks.create_endpoint(
                    db,
                    context,
                    settings,
                    EndpointCreate(connection_id=crm.id, source="synthetic_crm"),
                )
                endpoint = db.get(WebhookEndpoint, disclosure.endpoint.id)
            if endpoint.status != "ACTIVE":
                raise ValueError("Demo webhook disabled")
            secret_version = db.scalar(
                select(WebhookSecretVersion).where(
                    WebhookSecretVersion.endpoint_id == endpoint.id,
                    WebhookSecretVersion.valid_until.is_(None),
                )
            )
            secret = unseal(settings, secret_version)
            job = db.scalar(
                select(SyntheticCRMRecord).where(
                    SyntheticCRMRecord.connection_id == crm.id,
                    SyntheticCRMRecord.external_id == "demo-job-1",
                )
            )
            for kind in ["success", "retry", "terminal"]:
                body = json.dumps(
                    {
                        "schema_version": 1,
                        "source": "synthetic_crm",
                        "event_id": f"demo-workflow-{kind}",
                        "event_type": "job.updated" if kind == "success" else "job.created",
                        "external_id": job.external_id,
                        "occurred_at": "2030-01-15T12:00:00Z",
                        "data": job.payload,
                    }
                ).encode()
                body, headers = signed_request(secret, body)
                webhooks.ingest(
                    db,
                    settings,
                    endpoint.public_id,
                    headers["X-Webhook-Timestamp"],
                    headers["X-Webhook-Signature"],
                    body,
                    uuid.uuid4(),
                    time.monotonic(),
                )
        dispatch(db)
        worker = register_worker(db)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            with Session(engine) as db, db.begin():
                job = claim(db, worker)
                remaining = db.scalar(
                    select(WorkflowExecution.id)
                    .where(
                        WorkflowExecution.state.in_(
                            ["PENDING", "RUNNING", "WAITING", "RETRY_SCHEDULED"]
                        )
                    )
                    .limit(1)
                )
            if job:
                run_claim(engine, job)
            elif remaining:
                time.sleep(0.1)  # outside any database transaction
            else:
                break
        if remaining and time.monotonic() >= deadline:
            raise ValueError("Demo worker deadline exceeded; inspect pending work")
    finally:
        with Session(engine) as db, db.begin():
            db.get(WorkerLease, worker).stopped_at = datetime.now(UTC)
    with Session(engine) as db:
        rows = list(
            db.scalars(
                select(WorkflowExecution)
                .join(Tenant, Tenant.id == WorkflowExecution.tenant_id)
                .where(Tenant.slug.in_(["demo-recruiting-alpha", "demo-recruiting-beta"]))
            )
        )
        return {
            "workflow_executions": len(rows),
            "workflow_states": {
                state: sum(r.state == state for r in rows)
                for state in sorted({r.state for r in rows})
            },
        }
