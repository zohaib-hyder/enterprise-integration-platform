"""Read-only planning followed by revision-bound, idempotent service delegation."""

import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, assert_permission
from app.connectors.contracts import ConnectorError
from app.core.security import utcnow
from app.models.notification import NotificationDelivery, NotificationIntent
from app.models.operations import (
    Incident,
    IncidentEvidence,
    RecoveryApproval,
    RecoveryRequest,
)
from app.models.reliability import CircuitBreaker, ConnectionCredential, ConnectionReliabilityPolicy
from app.models.synchronization import SyncExecution, SyncOperation
from app.models.workflow import WorkflowActionReceipt, WorkflowStepExecution
from app.schemas.operations import ApprovalDecision, Confirmation, RecoveryCreate
from app.services import operations as ops
from app.services.notifications import digest, lock
from app.services.synchronization import public, scoped

EFFECTS = {
    "RETRY_NOTIFICATION": "Preserve the existing scheduled delivery retry and its backoff; never rerun ATS.",
    "REPLAY_WORKFLOW": "Delegate to existing checkpoint/receipt-preserving workflow replay.",
    "RESUME_ELIGIBLE_EXECUTION": "The existing worker resumes eligible pending/due or expired-lease work.",
    "RECHECK_CONNECTOR": "Run the existing synthetic connection health check; this is not proof of action delivery.",
    "REFRESH_SIMULATED_CREDENTIAL": "Rotate the existing synthetic token pair through its validated refresh service.",
    "RECONCILE_SYNC": "Run existing bounded read-only CRM/ATS reconciliation; no automatic repair.",
    "REVIEW_SYNC_CONFLICT": "Open the existing authority/digest-guarded conflict review; no field override.",
    "REVIEW_UNKNOWN_OUTCOME": "Check existing synthetic provider receipt/hash evidence; never resend.",
    "ACKNOWLEDGE_ALERT": "Acknowledge the existing operational alert without resolving business failure.",
    "ESCALATE_FOR_MANUAL_REVIEW": "Record a reviewed escalation without changing business state.",
}


def snapshot(db: Session, ctx: TenantContext, kind: str, row) -> dict:
    values = {"kind": kind, "record": public(row)}
    if kind == "workflow":
        values["steps"] = [
            public(s)
            for s in db.scalars(
                select(WorkflowStepExecution)
                .where(
                    WorkflowStepExecution.tenant_id == ctx.tenant.id,
                    WorkflowStepExecution.execution_id == row.id,
                )
                .order_by(WorkflowStepExecution.position)
            )
        ]
        values["receipts"] = [
            public(s)
            for s in db.scalars(
                select(WorkflowActionReceipt)
                .where(
                    WorkflowActionReceipt.tenant_id == ctx.tenant.id,
                    WorkflowActionReceipt.root_execution_id == row.root_id,
                )
                .order_by(WorkflowActionReceipt.id)
            )
        ]
        values["linked_deliveries"] = [
            {"id": str(s.id), "status": s.status, "attempt_count": s.attempt_count}
            for s in db.scalars(
                select(NotificationDelivery)
                .join(NotificationIntent, NotificationIntent.id == NotificationDelivery.intent_id)
                .where(
                    NotificationDelivery.tenant_id == ctx.tenant.id,
                    NotificationIntent.tenant_id == ctx.tenant.id,
                    NotificationIntent.workflow_execution_id.in_([row.id, row.root_id]),
                )
                .order_by(NotificationDelivery.id)
            )
        ]
        values["sync_operations"] = [
            public(s)
            for s in db.scalars(
                select(SyncOperation)
                .join(SyncExecution, SyncExecution.id == SyncOperation.execution_id)
                .where(
                    SyncOperation.tenant_id == ctx.tenant.id,
                    SyncExecution.workflow_execution_id.in_([row.id, row.root_id]),
                )
                .order_by(SyncOperation.id)
            )
        ]
    elif kind == "connection":
        values["policy"] = [
            public(s)
            for s in db.scalars(
                select(ConnectionReliabilityPolicy).where(
                    ConnectionReliabilityPolicy.tenant_id == ctx.tenant.id,
                    ConnectionReliabilityPolicy.connection_id == row.id,
                )
            )
        ]
        values["circuit"] = [
            public(s)
            for s in db.scalars(
                select(CircuitBreaker).where(
                    CircuitBreaker.tenant_id == ctx.tenant.id,
                    CircuitBreaker.connection_id == row.id,
                )
            )
        ]
        values["credential_metadata"] = [
            {
                "id": str(s.id),
                "status": s.status,
                "expires_at": str(s.expires_at),
                "generation": s.generation,
            }
            for s in db.scalars(
                select(ConnectionCredential)
                .where(
                    ConnectionCredential.tenant_id == ctx.tenant.id,
                    ConnectionCredential.connection_id == row.id,
                )
                .order_by(ConnectionCredential.id)
            )
        ]
    elif kind == "delivery":
        from app.models.notification import NotificationReceipt

        values["receipts"] = [
            public(s)
            for s in db.scalars(
                select(NotificationReceipt).where(
                    NotificationReceipt.tenant_id == ctx.tenant.id,
                    NotificationReceipt.delivery_id == row.id,
                )
            )
        ]
    return values


def canonical_target(db: Session, ctx: TenantContext, kind: str, identifier: uuid.UUID):
    row = ops.target(db, ctx, kind, identifier)
    if kind in {"incident", "finding"}:
        return row.target_kind, ops.target(db, ctx, row.target_kind, row.target_id), row
    if kind == "calendar":
        return "delivery", ops.target(db, ctx, "delivery", row.delivery_id), row
    return kind, row, None


def plan(db: Session, ctx: TenantContext, kind: str, identifier: uuid.UUID) -> dict:
    original_kind = kind
    try:
        kind, row, parent = canonical_target(db, ctx, kind, identifier)
    except HTTPException as error:
        if error.status_code != 404 or original_kind not in {"incident", "finding"}:
            raise
        row = ops.target(db, ctx, original_kind, identifier)
        kind = original_kind
        parent = None

    snap = snapshot(db, ctx, kind, row)
    if parent:
        snap["parent_revision"] = public(parent)
    revision = digest(snap)
    classification = (
        getattr(row, "error_category", None)
        or getattr(row, "category", None)
        or getattr(row, "state", None)
        or getattr(row, "status", None)
        or "OBSERVED_RECORD"
    )
    if kind in {"incident", "finding"}:
        classification = "SOURCE_EVIDENCE_UNAVAILABLE"
    if kind == "workflow" and any(r["state"] == "UNKNOWN" for r in snap["receipts"]):
        classification = "UNKNOWN_PROVIDER_WRITE_OUTCOME"
    if (
        kind == "workflow"
        and row.state == "RUNNING"
        and row.lease_expires_at
        and row.lease_expires_at < utcnow()
    ):
        classification = "WORKER_LEASE_EXPIRED"
    if kind == "connection" and any(c["state"] == "OPEN" for c in snap["circuit"]):
        classification = "CIRCUIT_OPEN"
    evidence_references = [{"kind": kind, "id": str(row.id), "link": ops.LINKS[kind]}]
    if isinstance(parent, Incident):
        observations = list(
            db.scalars(
                select(IncidentEvidence)
                .where(
                    IncidentEvidence.tenant_id == ctx.tenant.id,
                    IncidentEvidence.incident_id == parent.id,
                )
                .order_by(IncidentEvidence.observed_at.desc(), IncidentEvidence.id)
                .limit(100)
            )
        )
        if observations:
            classification = observations[0].classification
        evidence_references += [
            {
                "kind": e.source_kind,
                "id": str(e.source_id),
                "link": ops.LINKS.get(e.source_kind, "/operations"),
            }
            for e in observations
        ]
    options = []

    def option(
        action: str,
        eligible: bool = True,
        blocked=None,
        permission="executions:recover",
        approval=False,
    ):
        options.append(
            {
                "action": action,
                "eligible": eligible,
                "required_permission": permission,
                "target_kind": kind,
                "target_id": row.id,
                "expected_effect": EFFECTS[action],
                "preconditions": [
                    "Revalidate current evidence and active tenant permission",
                    "Confirm this exact unexpired request/revision",
                ],
                "idempotency_protections": [
                    "Database unique tenant/request UUID",
                    "Existing owning service locks, receipts and replay fences",
                ],
                "risks": [
                    "Approval never substitutes for receipt proof",
                    "Synthetic effects only; completed actions are retained",
                ],
                "blocked_reasons": blocked or [],
                "requires_approval": approval,
                "evidence_revision": revision,
                "detail_link": ops.LINKS[kind],
            }
        )

    if kind == "workflow":
        blocked = []
        if row.state not in {"FAILED", "DEAD_LETTERED"}:
            blocked.append("Execution must be terminal FAILED or DEAD_LETTERED")
        from app.services.workflows import principal_context

        try:
            principal_context(db, row)
        except HTTPException:
            blocked.append("Original execution principal is no longer authorized")
        if any(r["state"] == "UNKNOWN" for r in snap["receipts"]) or any(
            r["status"] == "UNKNOWN" for r in snap["linked_deliveries"]
        ):
            blocked.append(
                "Unknown external action outcome; replay remains blocked even with approval"
            )
        receipts = {r["step_id"]: r for r in snap["receipts"]}
        for step in snap["steps"]:
            if step["step_type"] == "MOCK_CONNECTOR_ACTION" and step["state"] == "SUCCEEDED":
                receipt = receipts.get(step["step_id"])
                if (
                    not receipt
                    or receipt["state"] != "APPLIED"
                    or receipt["result"] != step["output"]
                ):
                    blocked.append("Completed action receipt is missing or cannot be verified")
            if (
                step["step_type"] == "APPLY_CRM_ATS_SYNC"
                and step["state"] == "SUCCEEDED"
                and step["output"]
                and step["output"].get("decision") in {"CREATE", "UPDATE"}
            ) and not any(
                str(op["execution_id"]) == str(step["output"].get("execution_id"))
                and op["outcome"] in {"CREATE", "UPDATE"}
                for op in snap["sync_operations"]
            ):
                blocked.append("Completed ATS synchronization receipt cannot be verified")
        option("REPLAY_WORKFLOW", not blocked, blocked, "integrations:manage", True)
        option(
            "RESUME_ELIGIBLE_EXECUTION",
            False,
            [
                "No separate resume state mutation; existing worker owns due-time/expired-lease recovery"
            ],
        )
    elif kind == "delivery":
        option(
            "RETRY_NOTIFICATION",
            row.status == "RETRY_SCHEDULED",
            []
            if row.status == "RETRY_SCHEDULED"
            else [
                "Only an existing scheduled retry is eligible; do not reset exhausted budgets or resend UNKNOWN"
            ],
        )
        option(
            "REVIEW_UNKNOWN_OUTCOME",
            row.status == "UNKNOWN",
            [] if row.status == "UNKNOWN" else ["Only UNKNOWN actions require receipt inspection"],
        )
    elif kind == "connection":
        enabled = row.status == "ENABLED" and row.connector_type != "generic_rest"
        option(
            "RECHECK_CONNECTOR",
            enabled,
            [] if enabled else ["Use an enabled local synthetic connection"],
            "integrations:manage",
        )
        policy = snap["policy"][0] if snap["policy"] else None
        refresh = (
            enabled
            and policy is not None
            and policy["configuration"].get("credential_mode") == "OAUTH"
            if policy and "configuration" in policy
            else False
        )
        option(
            "REFRESH_SIMULATED_CREDENTIAL",
            refresh,
            [] if refresh else ["Configure an enforced synthetic OAuth token pair first"],
            "integrations:manage",
            True,
        )
    elif kind == "sync":
        option("RECONCILE_SYNC", True, permission="integrations:manage")
    elif kind == "conflict":
        option(
            "REVIEW_SYNC_CONFLICT",
            row.status == "OPEN",
            [] if row.status == "OPEN" else ["Conflict is already resolved"],
        )
    elif kind == "alert":
        option(
            "ACKNOWLEDGE_ALERT",
            row.status != "RESOLVED",
            [] if row.status != "RESOLVED" else ["Alert is resolved"],
            "alerts:acknowledge",
        )
    option("ESCALATE_FOR_MANUAL_REVIEW")
    category = parent.category if parent and isinstance(parent, Incident) else classification
    runbook = ops.RUNBOOKS.get(
        category,
        "unknown-outcome"
        if "UNKNOWN" in classification or "UNCERTAIN" in classification
        else "rate-limit-exceeded"
        if classification == "RATE_LIMITED"
        else "workflow-stuck"
        if kind == "workflow"
        else "notification-failed"
        if kind == "delivery"
        else "connector-unavailable",
    )
    if kind in {"sync", "conflict"}:
        runbook = "sync-conflict"
    if classification == "RATE_LIMITED":
        runbook = "rate-limit-exceeded"
    correlation = getattr(row, "correlation_id", None) or getattr(parent, "correlation_id", None)
    if kind in {"sync", "conflict"}:
        from app.models.synchronization import SyncPlan

        linked_plan = db.scalar(
            select(SyncPlan).where(SyncPlan.tenant_id == ctx.tenant.id, SyncPlan.id == row.plan_id)
        )
        if linked_plan:
            correlation = linked_plan.correlation_id
    return {
        "target_kind": original_kind,
        "target_id": identifier,
        "correlation_id": correlation,
        "evidence_revision": revision,
        "observed_symptom": f"{kind.upper()}: {classification}",
        "classification": classification,
        "certainty": "Observed persisted state/classification; no inferred root cause",
        "evidence_references": evidence_references,
        "runbook_id": runbook,
        "actions": options,
    }


def create_request(
    db: Session, ctx: TenantContext, data: RecoveryCreate, correlation_id: uuid.UUID
) -> RecoveryRequest:
    lock(db, ctx, "executions:recover")
    bound_incident_id = data.incident_id or (
        data.target_id if data.target_kind == "incident" else None
    )
    prior = db.scalar(
        select(RecoveryRequest).where(
            RecoveryRequest.tenant_id == ctx.tenant.id,
            RecoveryRequest.request_key == data.request_key,
        )
    )
    if prior:
        if (
            prior.target_kind,
            prior.target_id,
            prior.action,
            prior.evidence_revision,
            prior.reason,
            prior.incident_id,
        ) != (
            data.target_kind,
            data.target_id,
            data.action,
            data.evidence_revision,
            data.reason,
            bound_incident_id,
        ) or prior.actor_id != ctx.identity.user.id:
            raise HTTPException(409, "Recovery request ID is already bound to another preview")
        return prior
    preview = plan(db, ctx, data.target_kind, data.target_id)
    if preview["evidence_revision"] != data.evidence_revision:
        raise HTTPException(409, "Evidence changed after preview")
    selected = next((a for a in preview["actions"] if a["action"] == data.action), None)
    if not selected:
        raise HTTPException(422, "Action is not part of this recovery plan")
    assert_permission(ctx, selected["required_permission"])
    if bound_incident_id:
        linked = scoped(db, ctx, Incident, bound_incident_id)
        direct_incident = data.target_kind == "incident" and data.target_id == linked.id
        explicit_target = (linked.target_kind, linked.target_id) == (
            selected["target_kind"],
            selected["target_id"],
        )
        if not direct_incident and not explicit_target:
            raise HTTPException(422, "Incident must reference this exact recovery target")
    row = RecoveryRequest(
        tenant_id=ctx.tenant.id,
        request_key=data.request_key,
        actor_id=ctx.identity.user.id,
        incident_id=bound_incident_id,
        target_kind=data.target_kind,
        target_id=data.target_id,
        action=data.action,
        evidence_revision=data.evidence_revision,
        reason=data.reason,
        correlation_id=preview["correlation_id"] or correlation_id,
        requires_approval=selected["requires_approval"],
        expires_at=utcnow() + timedelta(minutes=15),
        status="AWAITING_APPROVAL" if selected["requires_approval"] else "PREVIEWED",
    )
    db.add(row)
    db.flush()
    if row.requires_approval:
        db.add(
            RecoveryApproval(
                tenant_id=ctx.tenant.id,
                request_id=row.id,
                requested_by=ctx.identity.user.id,
                reason=data.reason,
                expires_at=row.expires_at,
                evidence_revision=row.evidence_revision,
            )
        )
    ops.activity(
        db,
        ctx.tenant.id,
        "RECOVERY_PREVIEWED",
        "recovery",
        row.id,
        row.correlation_id,
        ctx.identity.user.id,
        data.reason,
        "BLOCKED" if not selected["eligible"] else row.status,
        row.incident_id,
    )
    db.flush()
    return row


def approve(
    db: Session, ctx: TenantContext, row: RecoveryApproval, data: ApprovalDecision
) -> RecoveryApproval:
    lock(db, ctx)
    db.refresh(row)
    request = scoped(db, ctx, RecoveryRequest, row.request_id)
    if row.status != "PENDING":
        raise HTTPException(409, "Approval already decided")
    if row.requested_by == ctx.identity.user.id:
        raise HTTPException(403, "A different authorized reviewer must decide")
    if row.expires_at <= utcnow() or request.expires_at <= utcnow():
        raise HTTPException(409, "Approval expired")
    current = plan(db, ctx, request.target_kind, request.target_id)
    if (
        data.evidence_revision != row.evidence_revision
        or current["evidence_revision"] != row.evidence_revision
    ):
        raise HTTPException(409, "Evidence changed; request a new reviewed plan")
    row.status = data.decision
    row.approved_by = ctx.identity.user.id
    if data.decision == "REJECTED":
        request.status = "REJECTED"
    ops.activity(
        db,
        ctx.tenant.id,
        "APPROVAL_" + data.decision,
        "approval",
        row.id,
        request.correlation_id,
        ctx.identity.user.id,
        data.reason,
        data.decision,
        request.incident_id,
    )
    db.flush()
    return row


def confirm(
    db: Session, ctx: TenantContext, row: RecoveryRequest, data: Confirmation
) -> RecoveryRequest:
    lock(db, ctx, "executions:recover")
    db.refresh(row)
    if row.actor_id != ctx.identity.user.id:
        raise HTTPException(403, "Only the requesting operator may confirm this exact request")
    if data.evidence_revision != row.evidence_revision:
        raise HTTPException(409, "Confirmation revision does not match")
    if row.status == "EXECUTED":
        return row
    if row.status in {"REJECTED", "STALE", "EXPIRED", "FAILED", "BLOCKED"}:
        raise HTTPException(409, "Request is no longer executable; preview again")
    if row.expires_at <= utcnow():
        row.status = "EXPIRED"
        row.result = {"outcome": "EXPIRED"}
        ops.activity(
            db,
            ctx.tenant.id,
            "RECOVERY_CONFIRMED",
            "recovery",
            row.id,
            row.correlation_id,
            ctx.identity.user.id,
            row.reason,
            "EXPIRED",
            row.incident_id,
        )
        db.flush()
        return row
    # Pin source state while the existing owning service performs the confirmed action.
    # Claim transactions may otherwise advance a due queue between preview and execution.
    source = ops.target(db, ctx, row.target_kind, row.target_id)
    db.refresh(source, with_for_update=True)
    if row.target_kind in {"incident", "finding", "calendar"}:
        try:
            _, business, _ = canonical_target(db, ctx, row.target_kind, row.target_id)
            db.refresh(business, with_for_update=True)
        except HTTPException as error:
            if error.status_code != 404:
                raise
    current = plan(db, ctx, row.target_kind, row.target_id)
    if current["evidence_revision"] != row.evidence_revision:
        row.status = "STALE"
        row.result = {"outcome": "STALE_EVIDENCE"}
    else:
        selected = next(a for a in current["actions"] if a["action"] == row.action)
        assert_permission(ctx, selected["required_permission"])
        approval = db.scalar(
            select(RecoveryApproval).where(
                RecoveryApproval.request_id == row.id, RecoveryApproval.tenant_id == ctx.tenant.id
            )
        )
        if row.requires_approval and (
            not approval or approval.status != "APPROVED" or approval.expires_at <= utcnow()
        ):
            raise HTTPException(409, "An unexpired independent approval is required")
        if not selected["eligible"]:
            row.status = "BLOCKED"
            row.result = {
                "outcome": "TECHNICAL_SAFETY_BLOCK",
                "reasons": selected["blocked_reasons"],
            }
        else:
            try:
                with db.begin_nested():
                    result = execute(db, ctx, row)
                row.status = "EXECUTED"
                row.result = result
            except (HTTPException, ConnectorError) as error:
                row.status = "FAILED"
                row.result = {
                    "outcome": "OWNING_SERVICE_REJECTED",
                    **(
                        {"http_status": error.status_code}
                        if isinstance(error, HTTPException)
                        else {"error_category": error.category.value}
                    ),
                }

    ops.activity(
        db,
        ctx.tenant.id,
        "RECOVERY_CONFIRMED",
        "recovery",
        row.id,
        row.correlation_id,
        ctx.identity.user.id,
        row.reason,
        row.status,
        row.incident_id,
    )
    db.flush()
    return row


def execute(db: Session, ctx: TenantContext, request: RecoveryRequest) -> dict:
    if request.action == "ESCALATE_FOR_MANUAL_REVIEW":
        ops.target(db, ctx, request.target_kind, request.target_id)
        return {"outcome": "ESCALATED", "link": ops.LINKS[request.target_kind]}
    kind, row, _ = canonical_target(db, ctx, request.target_kind, request.target_id)
    action = request.action
    from app.services import (
        credentials,
        integrations,
        notifications,
        observability,
        synchronization,
        workflows,
    )

    if action == "REPLAY_WORKFLOW":
        replay = workflows.replay(db, ctx, row.id)
        return {"outcome": "QUEUED", "execution_id": str(replay.id), "link": "/executions"}
    if action == "RETRY_NOTIFICATION":
        notifications.recover(db, ctx, row, "retry")
        return {
            "outcome": "SCHEDULE_PRESERVED",
            "delivery_id": str(row.id),
            "next_attempt_at": row.next_attempt_at.isoformat(),
            "link": "/notifications",
        }
    if action == "REVIEW_UNKNOWN_OUTCOME":
        reviewed = notifications.recover(db, ctx, row, "review")
        return {
            "outcome": "CONFIRMED_BY_RECEIPT" if reviewed["resolved"] else "REVIEWED_UNRESOLVED",
            "link": "/notifications",
        }
    if action == "RECHECK_CONNECTOR":
        result = integrations.test_health(db, ctx, row.id)
        return {"outcome": result.status, "connection_id": str(row.id), "link": "/connections"}
    if action == "REFRESH_SIMULATED_CREDENTIAL":
        connection = integrations.get_connection(db, ctx, row.id, True)
        from app.core.config import Settings

        result = credentials.refresh(
            db,
            db.info.get("settings") or Settings(),
            connection,
            actor_id=ctx.identity.user.id,
            correlation_id=request.correlation_id,
        )
        return {"outcome": result["outcome"], "link": "/connections"}
    if action == "RECONCILE_SYNC":
        from app.schemas.synchronization import ReconcileRequest

        cfg = scoped(db, ctx, synchronization.SyncConfiguration, row.configuration_id)
        result = synchronization.reconcile(db, ctx, cfg, ReconcileRequest(limit=100))
        return {"outcome": "READ_ONLY_SCAN", "run_id": str(result.id), "link": "/synchronization"}
    if action == "ACKNOWLEDGE_ALERT":
        result = observability.update_alert(db, ctx, row.id, "acknowledge", request.correlation_id)
        return {"outcome": result.status, "link": "/alerts"}
    return {
        "outcome": "REVIEW_REQUIRED" if action == "REVIEW_SYNC_CONFLICT" else "ESCALATED",
        "link": ops.LINKS[kind],
    }
