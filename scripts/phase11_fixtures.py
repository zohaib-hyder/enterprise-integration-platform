"""Create a retained unique schema for local E2E/load verification. Never reset public data."""

import json
import os
import secrets
import sys
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.security import password_hasher
from app.main import app  # noqa:F401 - complete metadata
from app.models.identity import Membership, User
from app.models.tenant import Tenant
from app.models.workflow import WorkflowExecution
from app.seed_demo import seed_demo
from app.seed_workflows import run_demo
from app.services import operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]


def public_fingerprint(engine):
    tables = [
        "tenants",
        "integration_connections",
        "incoming_events",
        "workflow_executions",
        "sync_operations",
        "notification_deliveries",
        "operation_incidents",
    ]
    with engine.connect() as conn:
        return {
            name: conn.scalar(
                text(
                    f"SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM public.{name} t"
                )
            )
            for name in tables
        }


def main():
    config = Settings()
    assert config.database_url
    import base64

    for key in ("WEBHOOK_ENCRYPTION_KEY", "CREDENTIAL_ENCRYPTION_KEY"):
        if not os.environ.get(key):
            current = getattr(config, key.lower())
            os.environ[key] = (
                current.get_secret_value()
                if current
                else base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            )
    admin = create_engine(config.database_url.get_secret_value())
    schema = "phase11_" + uuid.uuid4().hex[:12]
    before = public_fingerprint(admin)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = admin.url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(url, pool_size=2, max_overflow=2)
    with engine.begin() as conn:
        cfg = Config(str(ROOT / "backend/alembic.ini"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    password = secrets.token_urlsafe(30)
    email = "workflow-admin@example.test"
    with Session(engine) as db, db.begin():
        seed_demo(db, email, password, "Phase 11 synthetic operator")
    run_demo(engine, email)
    # Reuse existing deterministic domain fixture services, never raw fabricated success rows.
    sys.path.insert(0, str(ROOT / "backend/tests"))
    from test_notifications import configured, fault, manual, tick
    from test_synchronization import conflict_setup, context
    from test_workflows import setup

    ids = configured(engine)
    retry = manual(engine, ids)
    fault(engine, ids[2], "HTTP_503", retry_after="3600")
    tick(engine, ids[3])
    unknown = manual(engine, ids)
    fault(engine, ids[2], "READ_TIMEOUT")
    tick(engine, ids[3])
    sync_ids, conflict_id = conflict_setup(engine)
    wf_ids = setup(engine)
    from app.workflows.engine import dispatch

    with Session(engine) as db, db.begin():
        dispatch(db, wf_ids[0])
        execution_id = db.scalar(
            select(WorkflowExecution.id)
            .where(WorkflowExecution.workflow_id == wf_ids[1])
            .order_by(WorkflowExecution.created_at.desc())
            .limit(1)
        )
        assert execution_id
    with Session(engine) as db, db.begin():
        ctx = context(db)
        # Explicit test-only terminal fixture; real failure/restart paths run in existing verifier.
        execution = db.get(WorkflowExecution, execution_id)
        execution.state = "FAILED"
        incident = operations.observe(
            db,
            ctx.tenant.id,
            "CONNECTOR_UNHEALTHY",
            "connection",
            ids[2],
            "connection",
            ids[2],
            schema,
            "TEMPORARY_UNAVAILABLE",
            uuid.uuid4(),
        )
        alpha = ctx.tenant.id
        beta = db.scalar(select(Tenant.id).where(Tenant.slug == "demo-recruiting-beta"))
        operator = User(
            email="phase11-operations@example.test",
            display_name="Phase 11 Operations",
            password_hash=password_hasher.hash(password),
        )
        db.add(operator)
        db.flush()
        db.add(Membership(user_id=operator.id, tenant_id=alpha, role="OPERATIONS_USER"))
        actor = ctx.identity.user.id
        incident_id = incident.id
        from app.models.integration import IntegrationConnection

        connections = {
            r.connector_type: str(r.id)
            for r in db.scalars(
                select(IntegrationConnection).where(
                    IntegrationConnection.tenant_id == alpha,
                    IntegrationConnection.name.like("Demo %"),
                )
            )
        }
    payload = {
        "schema": schema,
        "webhook_encryption_key": os.environ["WEBHOOK_ENCRYPTION_KEY"],
        "credential_encryption_key": os.environ["CREDENTIAL_ENCRYPTION_KEY"],
        "database_url": url.render_as_string(hide_password=False),
        "email": email,
        "password": password,
        "operations_email": "phase11-operations@example.test",
        "alpha": str(alpha),
        "beta": str(beta),
        "actor_id": str(actor),
        "incident": str(incident_id),
        "workflow": str(wf_ids[1]),
        "execution": str(execution_id),
        "event": str(wf_ids[2]),
        "retry_delivery": str(retry),
        "unknown_delivery": str(unknown),
        "conflict": str(conflict_id),
        "sync_configuration": str(sync_ids[0]),
        "connections": connections,
        "public_fingerprint": before,
    }
    path = ROOT / "backend/.local/phase11-fixtures.json"
    path.write_text(json.dumps(payload, indent=2))
    os.chmod(path, 0o600)
    print(
        "Created retained isolated schema "
        + schema
        + "; private fixture stored without printing credentials."
    )
    engine.dispose()
    admin.dispose()


if __name__ == "__main__":
    main()
