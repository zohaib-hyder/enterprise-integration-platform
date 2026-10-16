"""Enforce extractive grounding rather than guessing semantic entailment."""

import json
import re

from app.ai.providers import ProviderError
from app.schemas.ai import AnalysisInput, StructuredAnalysis

UNKNOWN = "Root cause is not established."
INFERENCE = "The retained state warrants human investigation; root cause remains unknown."


def validate_output(raw: str, context: dict) -> StructuredAnalysis:
    if len(raw.encode()) > 24576:
        raise ProviderError("OVERSIZED_RESPONSE")
    try:
        output = StructuredAnalysis.model_validate_json(raw)
    except ValueError:
        raise ProviderError("INVALID_SCHEMA") from None
    evidence = {e["key"]: e for e in context["evidence"]}
    refs = set(output.evidence_references)
    if not refs.issubset(evidence):
        raise ProviderError("INVALID_EVIDENCE")
    if (
        output.analysis_type != context["analysis_type"]
        or output.target_reference != context["target_reference"]
    ):
        raise ProviderError("INVALID_TARGET")
    if any(claim.kind != "OBSERVED_FACT" for claim in output.observed_facts) or any(
        claim.kind == "OBSERVED_FACT" for claim in output.possible_causes
    ):
        raise ProviderError("UNGROUNDED_CLAIM")
    for claim in output.observed_facts + output.possible_causes:
        if not set(claim.evidence_ids).issubset(refs):
            raise ProviderError("INVALID_EVIDENCE")
        if claim.kind == "OBSERVED_FACT":
            if not claim.evidence_ids or not any(
                claim.text == evidence[k]["summary"] for k in claim.evidence_ids
            ):
                raise ProviderError("UNGROUNDED_CLAIM")
        elif claim.kind == "SUPPORTED_INFERENCE":
            if not claim.evidence_ids or claim.text != INFERENCE:
                raise ProviderError("UNGROUNDED_CLAIM")
        elif claim.text != UNKNOWN or claim.evidence_ids:
            raise ProviderError("UNGROUNDED_CLAIM")
    candidates = {json.dumps(a, sort_keys=True) for a in context["candidates"]}
    seen = set()
    for action in output.recommended_actions:
        value = action.model_dump(mode="json")
        if json.dumps(value, sort_keys=True) not in candidates or action.action_type in seen:
            raise ProviderError("UNAUTHORIZED_RECOMMENDATION")
        if not set(action.evidence_ids).issubset(refs):
            raise ProviderError("INVALID_EVIDENCE")
        seen.add(action.action_type)
    for field, key in [
        ("suggested_classification", "classification"),
        ("suggested_severity", "severity"),
        ("suggested_runbook", "runbook"),
    ]:
        if getattr(output, field) not in (None, context.get(key)):
            raise ProviderError("UNGROUNDED_CLASSIFICATION")
    sufficient = bool(evidence)
    if not sufficient and (
        output.observed_facts
        or output.recommended_actions
        or output.summary != "Insufficient evidence."
        or output.validation_status != "INSUFFICIENT_EVIDENCE"
    ):
        raise ProviderError("ABSTENTION_REQUIRED")
    if sufficient and (
        not output.observed_facts
        or output.summary != "Authorized evidence summarized below."
        or output.validation_status != "VALID"
    ):
        raise ProviderError("MISSING_GROUNDING")
    return output


def make_context(
    data: AnalysisInput,
    evidence: list[dict],
    plan: dict | None,
    classification_enabled: bool = False,
) -> dict:
    target = f"{data.target_kind}:{data.target_id}"
    candidates = []
    for action in (plan["actions"] if plan and evidence else [])[:8]:
        candidates.append(
            {
                "action_type": action["action"],
                "target_reference": target,
                "evidence_ids": [evidence[0]["key"]],
                "reason": "Inspect the authorized target through the existing recovery planner.",
                "expected_outcome": action["expected_effect"],
                "risks": action["risks"][:4],
                "human_review_required": True,
            }
        )
    return {
        "analysis_type": data.analysis_type,
        "target_reference": target,
        "evidence": evidence,
        "candidates": candidates,
        "classification": (
            plan["classification"]
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", plan["classification"])
            else "REDACTED"
        )
        if plan and classification_enabled
        else None,
        "severity": next(
            (
                m.group(1)
                for e in evidence
                if (m := re.search(r"severity=(INFO|WARNING|CRITICAL)(?:;|$)", e["summary"]))
            ),
            None,
        )
        if classification_enabled
        else None,
        "runbook": plan["runbook_id"] if plan and classification_enabled else None,
    }


def eligibility(
    *,
    same_revision: bool,
    has_evidence: bool,
    selected: dict | None,
    permissions: frozenset[str],
    review_state: str,
) -> tuple[str, list[str]]:
    """Pure decision used by live revalidation and offline fixtures; never performs actions."""
    if not same_revision:
        return "STALE", ["Evidence changed; request a new analysis."]
    if not has_evidence:
        return "INSUFFICIENT_EVIDENCE", []
    if not selected or not selected["eligible"]:
        return "BLOCKED", selected["blocked_reasons"] if selected else [
            "No current planner option."
        ]
    if selected["required_permission"] not in permissions:
        return "BLOCKED", ["Current role cannot request this action."]
    if review_state in {"REJECTED", "NEEDS_REVISION", "SUPERSEDED"}:
        return "BLOCKED", ["Human decision does not authorize a recovery handoff."]
    return ("VALID" if review_state == "ACCEPTED" else "REQUIRES_REVIEW"), []
