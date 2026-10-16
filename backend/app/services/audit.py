import uuid

from sqlalchemy.orm import Session

from app.models.identity import AuditEvent

# Allowlisted metadata only; never copy a request dictionary into audit persistence.
SAFE_VALUES = {
    "role": {"TENANT_ADMIN", "INTEGRATION_ENGINEER", "OPERATIONS_USER"},
    "previous_role": {"TENANT_ADMIN", "INTEGRATION_ENGINEER", "OPERATIONS_USER"},
    "status": {"ACTIVE", "INACTIVE"},
    "previous_status": {"ACTIVE", "INACTIVE"},
    "reason": {"invalid_credentials", "rate_limited"},
}


def record(
    db: Session,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    safe = {
        key: value
        for key, value in (metadata or {}).items()
        if key in SAFE_VALUES and value in SAFE_VALUES[key]
    }
    db.add(
        AuditEvent(
            action=action,
            target_type=target_type,
            target_id=target_id,
            tenant_id=tenant_id,
            actor_user_id=actor_id,
            details=safe,
        )
    )
