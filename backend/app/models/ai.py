"""AI metadata only; no business state or provider credentials."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.synchronization import SyncRow, ownership


class AIConfiguration(SyncRow, Base):
    __tablename__ = "ai_configurations"
    version: Mapped[int] = mapped_column(Integer)
    settings: Mapped[dict] = mapped_column(JSONB)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        *ownership(__tablename__),
        UniqueConstraint("tenant_id", "version", name="uq_ai_config_version"),
        CheckConstraint("version >= 1", name="ck_ai_config_version"),
    )


class AIAnalysisRequest(SyncRow, Base):
    __tablename__ = "ai_analysis_requests"
    request_key: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    target_kind: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    analysis_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(80))
    configuration_version: Mapped[int] = mapped_column(Integer)
    evidence_revision: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(24))
    telemetry: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        *ownership(__tablename__),
        UniqueConstraint("tenant_id", "request_key", name="uq_ai_request_key"),
        CheckConstraint("status IN ('SUCCEEDED','FAILED')", name="ck_ai_request_status"),
    )


class AIAnalysisResult(SyncRow, Base):
    __tablename__ = "ai_analysis_results"
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    output: Mapped[dict] = mapped_column(JSONB)
    validation_status: Mapped[str] = mapped_column(String(32))
    __table_args__ = ownership(__tablename__, {"request_id": "ai_analysis_requests"})


class AIEvidenceReference(SyncRow, Base):
    __tablename__ = "ai_evidence_references"
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    evidence_key: Mapped[str] = mapped_column(String(140))
    projection: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        *ownership(__tablename__, {"request_id": "ai_analysis_requests"}),
        UniqueConstraint("request_id", "evidence_key", name="uq_ai_evidence"),
    )


class AIRecommendation(SyncRow, Base):
    __tablename__ = "ai_recommendations"
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(40))
    eligibility: Mapped[str] = mapped_column(String(32))
    review_state: Mapped[str] = mapped_column(String(24), default="PENDING_REVIEW")
    version: Mapped[int] = mapped_column(Integer, default=1)
    details: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        *ownership(__tablename__, {"request_id": "ai_analysis_requests"}),
        UniqueConstraint("request_id", "action", name="uq_ai_recommendation"),
        CheckConstraint(
            "review_state IN ('PENDING_REVIEW','ACCEPTED','REJECTED','NEEDS_REVISION','SUPERSEDED') AND version >= 1",
            name="ck_ai_review",
        ),
        CheckConstraint(
            "eligibility IN ('VALID','BLOCKED','STALE','INSUFFICIENT_EVIDENCE','REQUIRES_REVIEW')",
            name="ck_ai_eligibility",
        ),
    )


class AIReviewDecision(SyncRow, Base):
    __tablename__ = "ai_review_decisions"
    recommendation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decision: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        *ownership(__tablename__, {"recommendation_id": "ai_recommendations"}),
        UniqueConstraint("recommendation_id", "version", name="uq_ai_review_version"),
    )


class AIEvaluationRun(SyncRow, Base):
    __tablename__ = "ai_evaluation_runs"
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    summary: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = ownership(__tablename__)


class AIEvaluationCaseResult(SyncRow, Base):
    __tablename__ = "ai_evaluation_case_results"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    case_key: Mapped[str] = mapped_column(String(100))
    result: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        *ownership(__tablename__, {"run_id": "ai_evaluation_runs"}),
        UniqueConstraint("run_id", "case_key", name="uq_ai_eval_case"),
    )
