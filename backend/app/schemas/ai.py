"""AI has a deliberately small, extractive output language, never executable tools."""

import uuid
from typing import Literal

from pydantic import Field

from app.connectors.contracts import Contract
from app.schemas.operations import Action, Reason

Kind = Literal[
    "incident", "workflow", "connection", "sync", "conflict", "delivery", "finding", "health"
]
AnalysisType = Literal[
    "INCIDENT_EXPLANATION",
    "WORKFLOW_FAILURE",
    "CONNECTOR_FAILURE",
    "SYNC_CONFLICT",
    "NOTIFICATION_FAILURE",
    "OPERATIONAL_HEALTH",
    "NEXT_INVESTIGATION",
]


class AnalysisInput(Contract):
    analysis_type: AnalysisType
    target_kind: Kind
    target_id: uuid.UUID
    request_key: uuid.UUID


class ConfigurationInput(Contract):
    expected_version: int = Field(ge=0)
    provider: Literal["mock", "openai"]
    model: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    fallback_to_mock: bool = True
    classification_enabled: bool = False


class Claim(Contract):
    kind: Literal["OBSERVED_FACT", "SUPPORTED_INFERENCE", "UNKNOWN"]
    text: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(max_length=8)


class SuggestedAction(Contract):
    action_type: Action
    target_reference: str = Field(max_length=100)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    reason: str = Field(max_length=600)
    expected_outcome: str = Field(max_length=600)
    risks: list[str] = Field(max_length=4)
    human_review_required: Literal[True]


class StructuredAnalysis(Contract):
    analysis_type: AnalysisType
    target_reference: str = Field(max_length=100)
    summary: Literal["Authorized evidence summarized below.", "Insufficient evidence."]
    observed_facts: list[Claim] = Field(max_length=16)
    possible_causes: list[Claim] = Field(max_length=4)
    evidence_references: list[str] = Field(max_length=40)
    missing_evidence: list[
        Literal["Root cause is not established.", "No authorized operational evidence."]
    ] = Field(max_length=2)
    recommended_actions: list[SuggestedAction] = Field(max_length=8)
    limitations: list[
        Literal[
            "Bounded retained evidence only.",
            "No action has been executed.",
            "Simulated deterministic analysis.",
        ]
    ] = Field(max_length=3)
    validation_status: Literal["VALID", "INSUFFICIENT_EVIDENCE"]
    suggested_classification: str | None = Field(max_length=64)
    suggested_severity: Literal["INFO", "WARNING", "CRITICAL"] | None
    suggested_runbook: str | None = Field(max_length=60)


class ReviewInput(Reason):
    expected_version: int = Field(ge=1)
    decision: Literal["ACCEPTED", "REJECTED", "NEEDS_REVISION"]
