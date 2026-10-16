"""Deterministic tenant-scoped projections: free text and payloads never enter context."""

import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, assert_permission
from app.core.security import utcnow
from app.models.identity import AuditEvent
from app.models.notification import NotificationDeliveryAttempt
from app.models.operations import Incident, OperationsActivity
from app.models.reliability import CircuitBreaker, ConnectionCredential
from app.models.synchronization import SyncPlan
from app.models.workflow import WorkflowExecutionEvent, WorkflowStepExecution
from app.operations.runbooks import runbook
from app.services import operation_recovery as recovery
from app.services import operations as ops


# Even projected classifications may originate from a compromised source record.
def symbol(value: Any) -> str:
    value = str(value)
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else "REDACTED"


def redact(value: str) -> str:
    if re.search(
        r"(?i)ignore.*instructions|reveal.*credential|system\s*[:>]|execute.*approval|cross.tenant|bearer\s|password|secret|token|https?://|sk-[a-z0-9]|sim_[a-z0-9_-]{12}",
        value,
    ):
        return "[redacted untrusted text]"
    return re.sub(r"[\x00-\x1f<>]", "", value)[:600]


def retrieve(
    db: Session, ctx: TenantContext, kind: str, identifier: uuid.UUID
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str]:
    assert_permission(ctx, "operations:read")
    evidence = []

    def add(source, row, fields, link):
        # Fields are compile-time allowlists, never model-controlled.
        summary = "; ".join(
            f"{key}={symbol(getattr(row, key))}"
            for key in fields
            if getattr(row, key, None) is not None
        )
        evidence.append(
            {
                "key": f"{source}:{row.id}",
                "source_type": source,
                "source_id": str(row.id),
                "timestamp": (getattr(row, "updated_at", None) or row.created_at).isoformat(),
                "summary": redact(summary),
                "link": link,
            }
        )

    def related(model, condition, source, fields, link):
        assert_permission(ctx, "operations:read")
        rows = db.scalars(
            select(model)
            .where(model.tenant_id == ctx.tenant.id, condition)
            .order_by(model.created_at.desc(), model.id)
            .limit(8)
        )
        for row in rows:
            add(source, row, fields, link)

    if kind == "health":
        if identifier != ctx.tenant.id:
            from fastapi import HTTPException

            raise HTTPException(404, "Target unavailable")
        # No wall-clock timestamp in fingerprint; only actual bounded retained records.
        related(
            Incident,
            Incident.status != "RESOLVED",
            "incident",
            ["category", "severity", "status"],
            "/operations",
        )
        plan = None
    else:
        row = ops.target(db, ctx, kind, identifier)
        add(
            kind,
            row,
            [
                "state",
                "status",
                "category",
                "severity",
                "error_category",
                "health_status",
                "classification",
            ],
            ops.LINKS[kind],
        )
        plan = recovery.plan(db, ctx, kind, identifier)
        try:
            actual, business, _ = recovery.canonical_target(db, ctx, kind, identifier)
        except Exception as error:
            from fastapi import HTTPException

            if not isinstance(error, HTTPException) or error.status_code != 404:
                raise
            actual, business = kind, row
        if kind == "incident":
            related(
                OperationsActivity,
                OperationsActivity.incident_id == row.id,
                "activity",
                ["action", "outcome"],
                "/operations",
            )
        if actual == "workflow":
            related(
                WorkflowExecutionEvent,
                WorkflowExecutionEvent.execution_id == business.id,
                "workflow_history",
                ["event_type", "category"],
                "/executions",
            )
            related(
                WorkflowStepExecution,
                WorkflowStepExecution.execution_id == business.id,
                "step",
                ["state", "error_category"],
                "/executions",
            )
        if actual == "delivery":
            related(
                NotificationDeliveryAttempt,
                NotificationDeliveryAttempt.delivery_id == business.id,
                "delivery_attempt",
                ["status", "error_category"],
                "/notifications",
            )
        if actual in {"sync", "conflict"}:
            related(
                SyncPlan, SyncPlan.id == business.plan_id, "plan", ["decision"], "/synchronization"
            )
        if actual == "connection":
            related(
                CircuitBreaker,
                CircuitBreaker.connection_id == business.id,
                "circuit",
                ["state"],
                "/connections",
            )
            related(
                ConnectionCredential,
                ConnectionCredential.connection_id == business.id,
                "credential",
                ["status"],
                "/connections",
            )
            # Expiration is a deterministic projection, without any encrypted/clear secret data.
            for credential in db.scalars(
                select(ConnectionCredential)
                .where(
                    ConnectionCredential.tenant_id == ctx.tenant.id,
                    ConnectionCredential.connection_id == business.id,
                )
                .limit(8)
            ):
                if credential.expires_at and credential.expires_at <= utcnow():
                    evidence.append(
                        {
                            "key": f"credential-expiry:{credential.id}",
                            "source_type": "credential_expiry",
                            "source_id": str(credential.id),
                            "timestamp": credential.expires_at.isoformat(),
                            "summary": "credential_status=EXPIRED",
                            "link": "/connections",
                        }
                    )
        # audit:read is intentionally narrower; engineers/operations cannot retrieve these.
        from app.api.dependencies import PERMISSIONS

        if "audit:read" in PERMISSIONS[ctx.membership.role]:
            related(AuditEvent, AuditEvent.target_id == business.id, "audit", ["action"], "/audit")
    evidence = evidence[:32]
    # Runbook prose remains untrusted; provider receives only allowlisted identity/version.
    if plan and runbook(plan["runbook_id"]):
        book = runbook(plan["runbook_id"])
        evidence.append(
            {
                "key": f"runbook:{book['id']}:v{book['version']}",
                "source_type": "runbook",
                "source_id": book["id"],
                "timestamp": None,
                "summary": f"runbook_version={book['version']}",
                "link": "/operations",
            }
        )
    revision = hashlib.sha256(
        json.dumps(
            {"evidence": evidence, "planner": plan["evidence_revision"] if plan else None},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return evidence, plan, revision
