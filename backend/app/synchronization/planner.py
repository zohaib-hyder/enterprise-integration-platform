"""Pure deterministic authority-aware CRM-to-ATS decisions; no I/O."""


def calculate(
    *,
    source: dict,
    destination_data: dict | None,
    owners: dict,
    fields: dict,
    baseline: dict | None,
    stale: bool = False,
    collision: bool = False,
    ambiguous: bool = False,
    allow_create: bool = True,
    allow_update: bool = True,
) -> dict:
    """Pure deterministic planner. Evidence ordering and digest are independent of requests."""
    changes, preserved, decisions, reasons = {}, {}, {}, []
    if stale:
        reasons.append("STALE_SOURCE")
    if collision:
        reasons.append("IDENTITY_COLLISION")
    if ambiguous:
        reasons.append("AMBIGUOUS_MATCH")
    for field, target in sorted(fields.items()):
        owner = owners.get(field, "REVIEW")
        decisions[field] = {
            "owner": owner,
            "authorized": owner == "synthetic_crm",
            "destination_field": target,
        }
        if target not in {"title", "department", "location"} or field not in source:
            reasons.append("INVALID_MAPPING")
        elif owner != "synthetic_crm":
            reasons.append("FIELD_AUTHORITY_VIOLATION")
        else:
            before = destination_data.get(target) if destination_data else None
            if (
                baseline is not None
                and destination_data is not None
                and before != baseline.get(target)
            ):
                reasons.append("DESTINATION_CHANGED")
            if before != source[field]:
                changes[target] = {"before": before, "after": source[field]}
    if destination_data:
        preserved = {
            key: value for key, value in sorted(destination_data.items()) if key not in changes
        }
    reasons = sorted(set(reasons))
    decision = (
        "CONFLICT"
        if reasons
        else "CREATE"
        if destination_data is None
        else "UPDATE"
        if changes
        else "NO_OP"
    )
    if decision == "CREATE" and (
        not allow_create or not {"title", "department", "location"} <= set(changes)
    ):
        decision = "SKIP" if not allow_create else "INVALID"
    if decision == "UPDATE" and not allow_update:
        decision = "SKIP"
    return {
        "decision": decision,
        "proposed_changes": changes,
        "preserved_destination_fields": preserved,
        "authority_decisions": decisions,
        "conflict_reasons": reasons,
        "explanation": {
            "CREATE": "Create a requisition with authorized CRM fields and initial ATS status OPEN.",
            "UPDATE": "Update authorized fields while preserving ATS recruiting status.",
            "NO_OP": "Authorized fields already match.",
            "CONFLICT": "Hold destination writes until explicit review and safe re-planning.",
            "SKIP": "Configured behavior disables this operation.",
            "INVALID": "Required ATS fields cannot be produced.",
        }[decision],
    }
