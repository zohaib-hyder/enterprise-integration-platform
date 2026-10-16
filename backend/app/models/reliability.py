"""Tenant-owned reliability controls and bounded operational evidence."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.identity import IdentityTimestamp


def connection_fk(name: str):
    return ForeignKeyConstraint(
        ["connection_id", "tenant_id"],
        ["integration_connections.id", "integration_connections.tenant_id"],
        name=name,
        ondelete="RESTRICT",
    )


class ConnectionReliabilityPolicy(IdentityTimestamp, Base):
    __tablename__ = "connection_reliability_policies"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    configuration: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (connection_fk("fk_reliability_connection"),)


class RateLimitPolicy(IdentityTimestamp, Base):
    __tablename__ = "rate_limit_policies"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    scope: Mapped[str] = mapped_column(String(16))
    scope_key: Mapped[str] = mapped_column(String(70))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    limit: Mapped[int] = mapped_column(Integer)
    window_seconds: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        connection_fk("fk_rate_connection"),
        UniqueConstraint("id", "tenant_id", name="uq_rate_policy_tenant"),
        UniqueConstraint("tenant_id", "scope", "scope_key", name="uq_rate_scope"),
        CheckConstraint(
            "scope IN ('TENANT','CONNECTION','CONNECTOR') AND \"limit\" BETWEEN 1 AND 100000 AND window_seconds BETWEEN 1 AND 120",
            name="ck_rate_policy",
        ),
        CheckConstraint(
            "(scope = 'CONNECTION' AND connection_id IS NOT NULL AND scope_key = connection_id::text) OR (scope = 'TENANT' AND connection_id IS NULL AND scope_key = 'tenant') OR (scope = 'CONNECTOR' AND connection_id IS NULL AND scope_key IN ('synthetic_crm','synthetic_ats','synthetic_slack','synthetic_email','synthetic_calendar'))",
            name="ck_rate_scope_ownership",
        ),
    )


class RateLimitBucket(IdentityTimestamp, Base):
    __tablename__ = "rate_limit_buckets"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_id", "tenant_id"],
            ["rate_limit_policies.id", "rate_limit_policies.tenant_id"],
            name="fk_bucket_policy",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("policy_id", "window_start", name="uq_rate_window"),
        CheckConstraint("used >= 0", name="ck_rate_used"),
        Index("ix_rate_bucket_retention", "tenant_id", "window_start"),
    )


class CircuitBreaker(IdentityTimestamp, Base):
    __tablename__ = "circuit_breakers"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    state: Mapped[str] = mapped_column(String(16), default="CLOSED")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    operation_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    probe_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        connection_fk("fk_circuit_connection"),
        CheckConstraint(
            "state IN ('CLOSED','OPEN','HALF_OPEN') AND failure_count >= 0 AND operation_failure_count >= 0",
            name="ck_circuit_state",
        ),
    )


class CircuitTransition(IdentityTimestamp, Base):
    __tablename__ = "circuit_transitions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    from_state: Mapped[str] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(40))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    __table_args__ = (
        connection_fk("fk_transition_connection"),
        CheckConstraint(
            "from_state IN ('CLOSED','OPEN','HALF_OPEN') AND to_state IN ('CLOSED','OPEN','HALF_OPEN')",
            name="ck_circuit_transition",
        ),
        Index("ix_circuit_transition_window", "tenant_id", "connection_id", "created_at"),
    )


class ConnectionCredential(IdentityTimestamp, Base):
    __tablename__ = "connection_credentials"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    credential_type: Mapped[str] = mapped_column(String(40))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary)
    key_reference: Mapped[str] = mapped_column(String(40))
    generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaces_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    __table_args__ = (
        connection_fk("fk_credential_connection"),
        UniqueConstraint("id", "tenant_id", "connection_id", name="uq_credential_ownership"),
        ForeignKeyConstraint(
            ["replaces_id", "tenant_id", "connection_id"],
            [
                "connection_credentials.id",
                "connection_credentials.tenant_id",
                "connection_credentials.connection_id",
            ],
            name="fk_credential_replaces",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "connection_id", "credential_type", "generation", name="uq_credential_generation"
        ),
        CheckConstraint(
            "credential_type IN ('API_KEY','OAUTH_ACCESS_TOKEN','OAUTH_REFRESH_TOKEN','SERVICE_ACCOUNT_REFERENCE') AND generation >= 1 AND status IN ('ACTIVE','EXPIRED','REVOKED','ROTATED')",
            name="ck_credential_contract",
        ),
        Index(
            "uq_current_credential",
            "connection_id",
            "credential_type",
            unique=True,
            postgresql_where=text("status IN ('ACTIVE','EXPIRED')"),
        ),
        Index("ix_credential_expiry", "tenant_id", "status", "expires_at"),
    )


class CredentialRefreshAttempt(IdentityTimestamp, Base):
    __tablename__ = "credential_refresh_attempts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(40))
    access_credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    refresh_credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    __table_args__ = (
        connection_fk("fk_refresh_connection"),
        ForeignKeyConstraint(
            ["access_credential_id", "tenant_id", "connection_id"],
            [
                "connection_credentials.id",
                "connection_credentials.tenant_id",
                "connection_credentials.connection_id",
            ],
            name="fk_refresh_access",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["refresh_credential_id", "tenant_id", "connection_id"],
            [
                "connection_credentials.id",
                "connection_credentials.tenant_id",
                "connection_credentials.connection_id",
            ],
            name="fk_refresh_token",
            ondelete="RESTRICT",
        ),
        CheckConstraint("outcome IN ('SUCCEEDED','FAILED')", name="ck_refresh_outcome"),
        Index("ix_refresh_window", "tenant_id", "connection_id", "created_at"),
    )


class FailureInjection(IdentityTimestamp, Base):
    __tablename__ = "failure_injections"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    scenario: Mapped[str] = mapped_column(String(40), default="NONE")
    remaining: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[int] = mapped_column(Integer, default=0)
    retry_after: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        connection_fk("fk_fault_connection"),
        CheckConstraint(
            "remaining BETWEEN 0 AND 10 AND consumed BETWEEN 0 AND 10", name="ck_fault_budget"
        ),
        CheckConstraint(
            "scenario IN ('NONE','HTTP_401','HTTP_403','HTTP_429','HTTP_500','HTTP_503','CONNECT_TIMEOUT','READ_TIMEOUT','EXPIRED_ACCESS_TOKEN','REVOKED_REFRESH_TOKEN','REFRESH_FAILED','RECOVERING_OUTAGE')",
            name="ck_fault_scenario",
        ),
    )


class ConnectorObservation(IdentityTimestamp, Base):
    __tablename__ = "connector_observations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connector_type: Mapped[str] = mapped_column(String(40))
    operation_name: Mapped[str] = mapped_column(String(40))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(40))
    error_category: Mapped[str | None] = mapped_column(String(40))
    admitted: Mapped[bool] = mapped_column(Boolean)
    retry_attempt: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        connection_fk("fk_observation_connection"),
        ForeignKeyConstraint(
            ["workflow_execution_id", "tenant_id"],
            ["workflow_executions.id", "workflow_executions.tenant_id"],
            name="fk_observation_execution",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "outcome IN ('SUCCEEDED','FAILED','DEFERRED') AND latency_ms >= 0 AND (retry_after_seconds IS NULL OR retry_after_seconds BETWEEN 0 AND 300)",
            name="ck_observation_outcome",
        ),
        Index("ix_operation_metrics", "tenant_id", "created_at", "connection_id"),
        Index("ix_operation_connection_window", "tenant_id", "connection_id", "created_at"),
    )


class OperationalAlert(IdentityTimestamp, Base):
    __tablename__ = "operational_alerts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    category: Mapped[str] = mapped_column(String(40))
    resource_key: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_alert_tenant"),
        UniqueConstraint("tenant_id", "category", "resource_key", name="uq_alert_dedup"),
        CheckConstraint(
            "category IN ('CONNECTION_UNHEALTHY','CREDENTIAL_EXPIRED','REFRESH_FAILED','RATE_LIMIT_EXCEEDED','CIRCUIT_OPEN','WORKFLOW_DEAD_LETTERED','WORKER_RECOVERY','REPEATED_CONNECTOR_FAILURE','REPEATED_DELIVERY_FAILURE','TERMINAL_NOTIFICATION_FAILURE','UNKNOWN_DELIVERY_OUTCOME','CALENDAR_CONFLICT') AND status IN ('OPEN','ACKNOWLEDGED','RESOLVED') AND severity IN ('INFO','WARNING','CRITICAL') AND occurrence_count >= 1",
            name="ck_alert_contract",
        ),
        Index("ix_alert_tenant_status", "tenant_id", "status", "severity", "last_seen_at"),
    )


class AlertEvent(IdentityTimestamp, Base):
    __tablename__ = "alert_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    alert_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(20))
    source_key: Mapped[str] = mapped_column(String(100))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    __table_args__ = (
        ForeignKeyConstraint(
            ["alert_id", "tenant_id"],
            ["operational_alerts.id", "operational_alerts.tenant_id"],
            name="fk_alert_event_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("alert_id", "event_type", "source_key", name="uq_alert_event_source"),
        CheckConstraint(
            "event_type IN ('OPENED','RECURRED','ACKNOWLEDGED','RESOLVED')",
            name="ck_alert_event_type",
        ),
        Index("ix_alert_history", "tenant_id", "alert_id", "created_at"),
    )
