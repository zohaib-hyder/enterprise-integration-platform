"""Versioned AI assistance API; there is intentionally no execution endpoint."""

import uuid

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.ai.evaluation import run_cases
from app.api.canonical import DB, Manage, Read, mutations, page
from app.api.webhooks import Limit, Offset
from app.models.ai import (
    AIAnalysisRequest,
    AIEvaluationCaseResult,
    AIEvaluationRun,
    AIRecommendation,
    AIReviewDecision,
)
from app.schemas.ai import AnalysisInput, ConfigurationInput, ReviewInput
from app.services import ai_assistance as service
from app.services.notifications import lock
from app.services.synchronization import scoped

router = APIRouter(prefix="/api/v1/ai", tags=["AI assistance"])


@router.get("/configuration")
def configuration(ctx: Read, db: DB) -> dict:
    return service.configuration(db, ctx)


@router.put("/configuration", dependencies=mutations)
def configure(data: ConfigurationInput, ctx: Manage, db: DB) -> dict:
    return service.configure(db, ctx, data)


@router.post("/analyses", dependencies=mutations, status_code=201)
def analyze(data: AnalysisInput, request: Request, ctx: Read, db: DB) -> dict:
    return service.analyze(db, ctx, data, uuid.UUID(request.state.correlation_id))


@router.get("/analyses")
def history(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    rows, total = page(db, AIAnalysisRequest, ctx.tenant.id, offset, limit)
    return {
        "items": [
            {
                key: getattr(r, key)
                for key in (
                    "id",
                    "analysis_type",
                    "target_kind",
                    "target_id",
                    "status",
                    "created_at",
                )
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/analyses/{identifier}")
def detail(identifier: uuid.UUID, ctx: Read, db: DB) -> dict:
    return service.detail(db, ctx, identifier)


@router.get("/analyses/{identifier}/evidence")
def evidence(identifier: uuid.UUID, ctx: Read, db: DB) -> list[dict]:
    return service.detail(db, ctx, identifier)["evidence"]


@router.get("/recommendations")
def recommendations(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    rows, total = page(db, AIRecommendation, ctx.tenant.id, offset, limit)
    # Return no restricted analysis/evidence content via queue shortcuts.
    return {
        "items": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "review_state": r.review_state,
                "eligibility": r.eligibility,
                "action": r.action,
                "version": r.version,
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/review-queue")
def queue(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    rows, total = page(
        db,
        AIRecommendation,
        ctx.tenant.id,
        offset,
        limit,
        [AIRecommendation.review_state == "PENDING_REVIEW"],
    )
    return {
        "items": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "action": r.action,
                "review_state": r.review_state,
                "version": r.version,
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/recommendations/{identifier}/validate", dependencies=mutations)
def validate(identifier: uuid.UUID, ctx: Read, db: DB) -> dict:
    lock(db, ctx, "operations:read")
    row = scoped(db, ctx, AIRecommendation, identifier)
    result = service.revalidate(db, ctx, row)
    service.audit(db, ctx, "recommendation_validated", row.id)
    return result


@router.post("/recommendations/{identifier}/review", dependencies=mutations)
def review(identifier: uuid.UUID, data: ReviewInput, ctx: Manage, db: DB) -> dict:
    return service.review(db, ctx, scoped(db, ctx, AIRecommendation, identifier), data)


@router.get("/recommendations/{identifier}/reviews")
def reviews(
    identifier: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25
) -> dict:
    row = scoped(db, ctx, AIRecommendation, identifier)
    service.detail(db, ctx, row.request_id)
    rows, total = page(
        db,
        AIReviewDecision,
        ctx.tenant.id,
        offset,
        limit,
        [AIReviewDecision.recommendation_id == identifier],
    )
    return {
        "items": [
            {
                key: getattr(r, key)
                for key in ("id", "actor_id", "decision", "reason", "version", "created_at")
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/metrics")
def metrics(ctx: Read, db: DB) -> dict:
    return service.metrics(db, ctx)


@router.post("/evaluation-runs", dependencies=mutations, status_code=201)
def evaluate(ctx: Manage, db: DB) -> dict:
    lock(db, ctx)
    # Bounded explicit offline runs, never external models even when configured.
    from datetime import timedelta

    from sqlalchemy import func

    from app.core.security import utcnow

    count = db.scalar(
        select(func.count())
        .select_from(AIEvaluationRun)
        .where(
            AIEvaluationRun.tenant_id == ctx.tenant.id,
            AIEvaluationRun.created_at > utcnow() - timedelta(hours=1),
        )
    )
    if count >= 5:
        from fastapi import HTTPException

        raise HTTPException(429, "Evaluation run limit reached")
    from app.core.config import Settings

    service.prune(db, ctx, db.info.get("settings") or Settings())
    cases = run_cases()
    row = AIEvaluationRun(
        tenant_id=ctx.tenant.id,
        actor_id=ctx.identity.user.id,
        summary={
            "provider": "mock",
            "model": "deterministic-v1",
            "total": len(cases),
            "passed": sum(c["passed"] for c in cases),
            "real_model_accuracy": None,
        },
    )
    db.add(row)
    db.flush()
    for case in cases:
        db.add(
            AIEvaluationCaseResult(
                tenant_id=ctx.tenant.id, run_id=row.id, case_key=case["case_key"], result=case
            )
        )
    service.audit(db, ctx, "evaluation_completed", row.id)
    return {"id": row.id, "summary": row.summary}


@router.get("/evaluation-runs")
def evaluations(ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 25) -> dict:
    rows, total = page(db, AIEvaluationRun, ctx.tenant.id, offset, limit)
    return {
        "items": [{"id": r.id, "summary": r.summary, "created_at": r.created_at} for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/evaluation-runs/{identifier}/results")
def cases(identifier: uuid.UUID, ctx: Read, db: DB, offset: Offset = 0, limit: Limit = 100) -> dict:
    scoped(db, ctx, AIEvaluationRun, identifier)
    rows, total = page(
        db,
        AIEvaluationCaseResult,
        ctx.tenant.id,
        offset,
        limit,
        [AIEvaluationCaseResult.run_id == identifier],
    )
    return {"items": [r.result for r in rows], "total": total, "offset": offset, "limit": limit}
