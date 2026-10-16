"""AI writes metadata only. No recovery execute/create/confirm functions imported."""

import asyncio
import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ai import providers
from app.ai.evidence import retrieve
from app.ai.validation import eligibility, make_context, validate_output
from app.api.dependencies import PERMISSIONS, TenantContext, assert_permission
from app.core.config import Settings
from app.core.security import utcnow
from app.models.ai import (
    AIAnalysisRequest,
    AIAnalysisResult,
    AIConfiguration,
    AIEvaluationCaseResult,
    AIEvaluationRun,
    AIEvidenceReference,
    AIRecommendation,
    AIReviewDecision,
)
from app.schemas.ai import AnalysisInput, ConfigurationInput, ReviewInput
from app.services.audit import record
from app.services.notifications import lock
from app.services.synchronization import scoped


def audit(db: Session, ctx: TenantContext, action: str, identifier: uuid.UUID) -> None:
    record(db, "ai." + action, "ai", identifier, ctx.tenant.id, ctx.identity.user.id)


def configuration(db: Session, ctx: TenantContext) -> dict[str, Any]:
    assert_permission(ctx, "operations:read")
    row = db.scalar(
        select(AIConfiguration)
        .where(AIConfiguration.tenant_id == ctx.tenant.id)
        .order_by(AIConfiguration.version.desc())
        .limit(1)
    )
    settings = db.info.get("settings") or Settings()
    return {
        "version": row.version if row else 0,
        **(
            row.settings
            if row
            else {
                "provider": "mock",
                "model": "deterministic-v1",
                "fallback_to_mock": True,
                "classification_enabled": False,
            }
        ),
        "openai_available": bool(
            settings.ai_openai_enabled and settings.ai_openai_api_key and settings.ai_openai_model
        ),
        "configured_openai_model": settings.ai_openai_model if settings.ai_openai_enabled else None,
    }


def configure(db: Session, ctx: TenantContext, data: ConfigurationInput) -> dict[str, Any]:
    lock(db, ctx)
    current = configuration(db, ctx)
    if data.expected_version != current["version"]:
        raise HTTPException(409, "Configuration changed")
    settings = db.info.get("settings") or Settings()
    if data.provider == "openai" and (
        not current["openai_available"] or data.model != settings.ai_openai_model
    ):
        raise HTTPException(422, "OpenAI must be explicitly configured on the server")
    if data.provider == "mock" and data.model != "deterministic-v1":
        raise HTTPException(422, "Mock model must be deterministic-v1")
    row = AIConfiguration(
        tenant_id=ctx.tenant.id,
        version=current["version"] + 1,
        actor_id=ctx.identity.user.id,
        settings=data.model_dump(exclude={"expected_version"}),
    )
    db.add(row)
    db.flush()
    audit(db, ctx, "configuration_created", row.id)
    return configuration(db, ctx)


def prune(db: Session, ctx: TenantContext, settings: Settings) -> None:
    # Bound AI-only storage. Business/audit/incident/recovery history is never touched.
    old = list(
        db.scalars(
            select(AIAnalysisRequest.id)
            .where(AIAnalysisRequest.tenant_id == ctx.tenant.id)
            .order_by(AIAnalysisRequest.created_at.desc(), AIAnalysisRequest.id)
            .offset(settings.ai_max_retained_requests - 1)
            .limit(100)
        )
    )
    old += list(
        db.scalars(
            select(AIAnalysisRequest.id)
            .where(
                AIAnalysisRequest.tenant_id == ctx.tenant.id,
                AIAnalysisRequest.created_at
                < utcnow() - timedelta(days=settings.ai_retention_days),
            )
            .limit(100)
        )
    )
    old_runs = list(
        db.scalars(
            select(AIEvaluationRun.id)
            .where(AIEvaluationRun.tenant_id == ctx.tenant.id)
            .order_by(AIEvaluationRun.created_at.desc(), AIEvaluationRun.id)
            .offset(99)
            .limit(100)
        )
    )
    old_runs += list(
        db.scalars(
            select(AIEvaluationRun.id)
            .where(
                AIEvaluationRun.tenant_id == ctx.tenant.id,
                AIEvaluationRun.created_at < utcnow() - timedelta(days=settings.ai_retention_days),
            )
            .limit(100)
        )
    )
    if old_runs:
        db.execute(
            delete(AIEvaluationCaseResult).where(
                AIEvaluationCaseResult.tenant_id == ctx.tenant.id,
                AIEvaluationCaseResult.run_id.in_(old_runs),
            )
        )
        db.execute(
            delete(AIEvaluationRun).where(
                AIEvaluationRun.tenant_id == ctx.tenant.id, AIEvaluationRun.id.in_(old_runs)
            )
        )
    if not old:
        return
    recs = select(AIRecommendation.id).where(
        AIRecommendation.tenant_id == ctx.tenant.id, AIRecommendation.request_id.in_(old)
    )
    db.execute(
        delete(AIReviewDecision).where(
            AIReviewDecision.tenant_id == ctx.tenant.id,
            AIReviewDecision.recommendation_id.in_(recs),
        )
    )
    for model in (AIRecommendation, AIEvidenceReference, AIAnalysisResult):
        db.execute(delete(model).where(model.tenant_id == ctx.tenant.id, model.request_id.in_(old)))
    db.execute(
        delete(AIAnalysisRequest).where(
            AIAnalysisRequest.tenant_id == ctx.tenant.id, AIAnalysisRequest.id.in_(old)
        )
    )


def analyze(
    db: Session, ctx: TenantContext, data: AnalysisInput, correlation_id: uuid.UUID
) -> dict[str, Any]:
    lock(db, ctx, "operations:read")
    prior = db.scalar(
        select(AIAnalysisRequest).where(
            AIAnalysisRequest.tenant_id == ctx.tenant.id,
            AIAnalysisRequest.request_key == data.request_key,
        )
    )
    if prior:
        if (prior.actor_id, prior.target_kind, prior.target_id, prior.analysis_type) != (
            ctx.identity.user.id,
            data.target_kind,
            data.target_id,
            data.analysis_type,
        ):
            raise HTTPException(409, "Request key already bound")
        return detail(db, ctx, prior.id)
    settings = db.info.get("settings") or Settings()
    count = db.scalar(
        select(func.count())
        .select_from(AIAnalysisRequest)
        .where(
            AIAnalysisRequest.tenant_id == ctx.tenant.id,
            AIAnalysisRequest.created_at > utcnow() - timedelta(hours=1),
        )
    )
    if count >= settings.ai_requests_per_hour:
        raise HTTPException(429, "Tenant AI request limit reached")
    evidence, plan, revision = retrieve(db, ctx, data.target_kind, data.target_id)
    config = configuration(db, ctx)
    context = make_context(data, evidence, plan, config["classification_enabled"])
    context.update(model=config["model"], correlation_id=str(correlation_id))
    if len(__import__("json").dumps(context).encode()) > 24576:
        raise HTTPException(422, "Evidence context exceeds bounded limit")
    row = AIAnalysisRequest(
        tenant_id=ctx.tenant.id,
        request_key=data.request_key,
        actor_id=ctx.identity.user.id,
        target_kind=data.target_kind,
        target_id=data.target_id,
        analysis_type=data.analysis_type,
        provider=config["provider"],
        model=config["model"],
        configuration_version=config["version"],
        evidence_revision=revision,
        correlation_id=correlation_id,
        status="FAILED",
    )
    db.add(row)
    db.flush()
    started = time.perf_counter()
    errors = []
    result = None
    usage = None
    actual = config["provider"]
    try:
        generated = asyncio.run(providers.generate(actual, context, settings))
        usage = generated.usage
        result = validate_output(generated.text, context)
    except providers.ProviderError as error:
        errors.append(error.category)
        if config["fallback_to_mock"] and actual != "mock":
            actual = "mock"
            try:
                generated = asyncio.run(providers.generate("mock", context, settings))
                result = validate_output(generated.text, context)
            except providers.ProviderError as fallback_error:
                errors.append(fallback_error.category)
    row.telemetry = {
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "provider_errors": errors,
        "actual_provider": actual,
        "actual_model": "deterministic-v1" if actual == "mock" else config["model"],
        "simulated": actual == "mock",
        "usage": usage,
        "estimated_cost_usd": None,
        "classification_disagreement": (result.suggested_classification != plan["classification"])
        if result and result.suggested_classification and plan
        else None,
    }
    if (
        usage
        and settings.ai_input_price_per_million is not None
        and settings.ai_output_price_per_million is not None
        and set(usage) == {"input_tokens", "output_tokens"}
    ):
        row.telemetry["estimated_cost_usd"] = (
            usage["input_tokens"] * settings.ai_input_price_per_million
            + usage["output_tokens"] * settings.ai_output_price_per_million
        ) / 1_000_000
    for item in evidence:
        db.add(
            AIEvidenceReference(
                tenant_id=ctx.tenant.id,
                request_id=row.id,
                evidence_key=item["key"],
                projection=item,
            )
        )
    if result:
        row.status = "SUCCEEDED"
        db.add(
            AIAnalysisResult(
                tenant_id=ctx.tenant.id,
                request_id=row.id,
                output=result.model_dump(mode="json"),
                validation_status=result.validation_status,
            )
        )
        for action in result.recommended_actions:
            rec = AIRecommendation(
                tenant_id=ctx.tenant.id,
                request_id=row.id,
                action=action.action_type,
                eligibility="REQUIRES_REVIEW",
                details=action.model_dump(mode="json"),
            )
            db.add(rec)
            db.flush()
            revalidate(db, ctx, rec)
        # Explicit revision requests are superseded only when a new successful matching analysis exists.
        superseded = db.scalars(
            select(AIRecommendation)
            .join(AIAnalysisRequest, AIAnalysisRequest.id == AIRecommendation.request_id)
            .where(
                AIRecommendation.tenant_id == ctx.tenant.id,
                AIAnalysisRequest.tenant_id == ctx.tenant.id,
                AIAnalysisRequest.target_kind == row.target_kind,
                AIAnalysisRequest.target_id == row.target_id,
                AIAnalysisRequest.actor_id == row.actor_id,
                AIRecommendation.review_state == "NEEDS_REVISION",
            )
        )
        for old in superseded:
            old.review_state = "SUPERSEDED"
            old.version += 1
            db.add(
                AIReviewDecision(
                    tenant_id=ctx.tenant.id,
                    recommendation_id=old.id,
                    actor_id=ctx.identity.user.id,
                    decision="SUPERSEDED",
                    reason="New successful analysis supersedes requested revision.",
                    version=old.version,
                )
            )
    db.flush()
    audit(db, ctx, "analysis_" + row.status.lower(), row.id)
    prune(db, ctx, settings)
    return detail(db, ctx, row.id)


def recommendation_output(row: AIRecommendation) -> dict[str, Any]:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "request_id",
            "action",
            "eligibility",
            "review_state",
            "version",
            "details",
            "created_at",
        )
    }


def detail(db: Session, ctx: TenantContext, identifier: uuid.UUID) -> dict[str, Any]:
    assert_permission(ctx, "operations:read")
    row = scoped(db, ctx, AIAnalysisRequest, identifier)
    result = db.scalar(
        select(AIAnalysisResult).where(
            AIAnalysisResult.tenant_id == ctx.tenant.id, AIAnalysisResult.request_id == row.id
        )
    )
    evidence = list(
        db.scalars(
            select(AIEvidenceReference)
            .where(
                AIEvidenceReference.tenant_id == ctx.tenant.id,
                AIEvidenceReference.request_id == row.id,
            )
            .order_by(AIEvidenceReference.evidence_key)
        )
    )
    recs = list(
        db.scalars(
            select(AIRecommendation)
            .where(
                AIRecommendation.tenant_id == ctx.tenant.id, AIRecommendation.request_id == row.id
            )
            .order_by(AIRecommendation.created_at, AIRecommendation.id)
        )
    )
    # Historical audit projections remain subject to present-day permission, including after role changes.
    permitted = "audit:read" in PERMISSIONS[ctx.membership.role]
    if any(e.projection["source_type"] == "audit" for e in evidence) and not permitted:
        raise HTTPException(403, "Analysis contains restricted audit evidence")
    return {
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "target_kind",
                "target_id",
                "analysis_type",
                "provider",
                "model",
                "configuration_version",
                "correlation_id",
                "status",
                "telemetry",
                "created_at",
                "evidence_revision",
            )
        },
        "output": result.output if result else None,
        "evidence": [e.projection for e in evidence],
        "recommendations": [recommendation_output(r) for r in recs],
    }


def revalidate(db: Session, ctx: TenantContext, row: AIRecommendation) -> dict[str, Any]:
    assert_permission(ctx, "operations:read")
    request = scoped(db, ctx, AIAnalysisRequest, row.request_id)
    detail(db, ctx, request.id)  # enforce current evidence authorization
    try:
        evidence, plan, revision = retrieve(db, ctx, request.target_kind, request.target_id)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        evidence, plan, revision = [], None, None
    selected = (
        next((a for a in plan["actions"] if a["action"] == row.action), None) if plan else None
    )
    status, reasons = eligibility(
        same_revision=revision == request.evidence_revision,
        has_evidence=bool(evidence),
        selected=selected,
        permissions=PERMISSIONS[ctx.membership.role],
        review_state=row.review_state,
    )
    row.eligibility = status
    return {
        "recommendation": recommendation_output(row),
        "status": status,
        "blocked_reasons": reasons,
        "requires_independent_approval": bool(selected and selected["requires_approval"]),
        "preview_only": True,
        "recovery_preview": {
            "target_kind": request.target_kind,
            "target_id": str(request.target_id),
            "evidence_revision": plan["evidence_revision"],
        }
        if plan and status == "VALID"
        else None,
        "link": f"/operations?target_kind={request.target_kind}&target_id={request.target_id}"
        if plan and status == "VALID"
        else None,
    }


def review(
    db: Session, ctx: TenantContext, row: AIRecommendation, data: ReviewInput
) -> dict[str, Any]:
    lock(db, ctx)
    db.refresh(row)
    if row.version != data.expected_version or row.review_state != "PENDING_REVIEW":
        raise HTTPException(409, "Review already decided or revision changed")
    validation = revalidate(db, ctx, row)
    if data.decision == "ACCEPTED" and validation["status"] in {
        "STALE",
        "BLOCKED",
        "INSUFFICIENT_EVIDENCE",
    }:
        raise HTTPException(409, "Recommendation is not currently eligible")
    row.review_state = data.decision
    row.version += 1
    db.add(
        AIReviewDecision(
            tenant_id=ctx.tenant.id,
            recommendation_id=row.id,
            actor_id=ctx.identity.user.id,
            decision=data.decision,
            reason=data.reason,
            version=row.version,
        )
    )
    audit(db, ctx, "review_" + data.decision.lower(), row.id)
    db.flush()
    return revalidate(db, ctx, row)


def metrics(db: Session, ctx: TenantContext) -> dict[str, Any]:
    assert_permission(ctx, "operations:read")
    cutoff = utcnow() - timedelta(days=30)
    rows = list(
        db.scalars(
            select(AIAnalysisRequest)
            .where(
                AIAnalysisRequest.tenant_id == ctx.tenant.id, AIAnalysisRequest.created_at >= cutoff
            )
            .order_by(AIAnalysisRequest.created_at.desc())
            .limit(5000)
        )
    )
    errors = [e for r in rows for e in r.telemetry.get("provider_errors", [])]
    reviews = dict(
        db.execute(
            select(AIReviewDecision.decision, func.count())
            .where(
                AIReviewDecision.tenant_id == ctx.tenant.id, AIReviewDecision.created_at >= cutoff
            )
            .group_by(AIReviewDecision.decision)
        ).all()
    )
    return {
        "basis": "Requests/reviews: retained last 30 days; requests capped at 5000. Blocked recommendations: retained snapshot. No mock token estimates.",
        "requests": len(rows),
        "successes": sum(r.status == "SUCCEEDED" for r in rows),
        "failures": sum(r.status == "FAILED" for r in rows),
        "provider_errors": len(errors),
        "validation_failures": sum(
            e
            in {
                "INVALID_SCHEMA",
                "INVALID_EVIDENCE",
                "INVALID_TARGET",
                "UNGROUNDED_CLAIM",
                "UNAUTHORIZED_RECOMMENDATION",
                "UNGROUNDED_CLASSIFICATION",
                "ABSTENTION_REQUIRED",
                "MISSING_GROUNDING",
                "OVERSIZED_RESPONSE",
            }
            for e in errors
        ),
        "invalid_evidence_references": errors.count("INVALID_EVIDENCE"),
        "blocked_recommendations": db.scalar(
            select(func.count())
            .select_from(AIRecommendation)
            .where(
                AIRecommendation.tenant_id == ctx.tenant.id,
                AIRecommendation.eligibility == "BLOCKED",
            )
        ),
        "human_decisions": reviews,
        "mean_latency_ms": round(sum(r.telemetry.get("latency_ms", 0) for r in rows) / len(rows), 2)
        if rows
        else None,
        "input_tokens": sum(
            r.telemetry["usage"].get("input_tokens", 0) for r in rows if r.telemetry.get("usage")
        )
        if any(r.telemetry.get("usage") for r in rows)
        else None,
        "output_tokens": sum(
            r.telemetry["usage"].get("output_tokens", 0) for r in rows if r.telemetry.get("usage")
        )
        if any(r.telemetry.get("usage") for r in rows)
        else None,
        "estimated_cost_usd": sum(
            r.telemetry["estimated_cost_usd"]
            for r in rows
            if r.telemetry.get("estimated_cost_usd") is not None
        )
        if any(r.telemetry.get("estimated_cost_usd") is not None for r in rows)
        else None,
    }
