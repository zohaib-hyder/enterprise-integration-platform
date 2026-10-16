"""Idempotent synthetic connector demo seed. Operator credentials are never printed."""

import getpass
import os
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import Identity, authorized_context
from app.bootstrap import bootstrap
from app.connectors.contracts import ConnectorOperation, ConnectorType
from app.connectors.registry import registry
from app.core.config import Settings
from app.core.security import token_hash, utcnow
from app.db.session import build_engine
from app.models.identity import AuthSession, User
from app.models.integration import IntegrationConnection
from app.models.tenant import Tenant
from app.schemas.integration import CreateConnection
from app.services.integrations import create_connection, execute

DEMO_ORGANIZATIONS = [
    ("demo-recruiting-alpha", "Cedar Ridge Recruiting (Synthetic)"),
    ("demo-recruiting-beta", "Great Lakes Talent (Synthetic)"),
]
DEMO_CONFIGS = {
    ConnectorType.CRM: {"workspace_label": "Synthetic sales workspace"},
    ConnectorType.ATS: {"workspace_label": "Synthetic recruiting workspace"},
    ConnectorType.SLACK: {"allowed_channels": ["recruiting", "operations", "alerts"]},
    ConnectorType.EMAIL: {
        "allowed_recipients": ["recruiter@example.test", "candidate@example.test"]
    },
    ConnectorType.CALENDAR: {"allowed_calendars": ["interviews", "recruiting"]},
}


def seed_demo(db: Session, email: str, password: str, display_name: str) -> dict[str, int]:
    for slug, name in DEMO_ORGANIZATIONS:
        bootstrap(db, email, password, display_name, name, slug)
        tenant = db.scalar(select(Tenant).where(Tenant.slug == slug))
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        identity = Identity(
            user,
            AuthSession(
                user_id=user.id,
                token_hash=token_hash(secrets.token_urlsafe(32)),
                expires_at=utcnow() + timedelta(hours=1),
                active_tenant_id=tenant.id,
            ),
        )
        context = authorized_context(db, identity, tenant.id)
        connections = {}
        for kind, config in DEMO_CONFIGS.items():
            connection_name = f"Demo {kind.value.removeprefix('synthetic_').upper()}"
            row = db.scalar(
                select(IntegrationConnection).where(
                    IntegrationConnection.tenant_id == tenant.id,
                    IntegrationConnection.name == connection_name,
                )
            )
            if row is None:
                row = create_connection(
                    db,
                    context,
                    CreateConnection(connector_type=kind, name=connection_name, config=config),
                )
            elif row.connector_type != kind.value or row.status != "ENABLED":
                raise ValueError(
                    "Existing demo connection is incompatible or disabled; seed will not reset it"
                )
            registry.resolve(kind).validate_config(row.public_config)
            connections[kind] = row.id

        def run(
            kind: ConnectorType, name: str, payload: dict, context=context, connections=connections
        ) -> None:
            execute(db, context, connections[kind], ConnectorOperation(name=name, payload=payload))

        for index in range(1, 4):
            run(
                ConnectorType.CRM,
                "contact.upsert",
                {
                    "external_id": f"demo-contact-{index}",
                    "data": {
                        "name": f"Synthetic Client Contact {index}",
                        "email": f"client-{index}@example.test",
                        "company": "Fictional Harbor Manufacturing",
                    },
                },
            )
            run(
                ConnectorType.ATS,
                "candidate.upsert",
                {
                    "external_id": f"demo-candidate-{index}",
                    "data": {
                        "name": f"Synthetic Candidate {index}",
                        "email": f"candidate-{index}@example.test",
                        "stage": "SCREENING",
                        "skills": ["Python", "Operations"],
                    },
                },
            )
        for kind in [ConnectorType.CRM, ConnectorType.ATS]:
            for index in range(1, 3):
                run(
                    kind,
                    "requisition.upsert",
                    {
                        "external_id": f"demo-job-{index}",
                        "data": {
                            "title": f"Synthetic Integration Specialist {index}",
                            "department": "Technology",
                            "location": "Toronto, Canada",
                            "status": "OPEN",
                        },
                    },
                )
        run(
            ConnectorType.SLACK,
            "message.send",
            {
                "channel": "recruiting",
                "text": "Synthetic interview coordination demo. No external Slack message was sent.",
                "idempotency_key": "demo-slack-message-v1",
            },
        )
        run(
            ConnectorType.EMAIL,
            "message.send",
            {
                "recipient": "recruiter@example.test",
                "subject": "Synthetic interview coordination",
                "body": "Local demo delivery only; no email was sent.",
                "idempotency_key": "demo-email-message-v1",
            },
        )
        for index in range(2):
            start = datetime(2030, 1, 15, 15 + index * 2, tzinfo=UTC)
            run(
                ConnectorType.CALENDAR,
                "event.create",
                {
                    "calendar": "interviews",
                    "title": f"Synthetic interview {index + 1}",
                    "starts_at": start.isoformat(),
                    "ends_at": (start + timedelta(hours=1)).isoformat(),
                    "idempotency_key": f"demo-calendar-event-{index + 1}-v1",
                },
            )
        from app.seed_canonical import seed_canonical

        seed_canonical(db, context, connections)
        from app.seed_workflows import seed_workflows

        seed_workflows(db, context, connections)

    from app.models.canonical import (
        CanonicalRecord,
        ExternalRecordMapping,
        QuarantinedRecord,
        TransformationAttempt,
        TransformationMapping,
    )

    demo_ids = list(
        db.scalars(
            select(Tenant.id).where(Tenant.slug.in_([slug for slug, _ in DEMO_ORGANIZATIONS]))
        )
    )
    canonical_counts = {
        name: db.scalar(
            select(func.count()).select_from(model).where(model.tenant_id.in_(demo_ids))
        )
        or 0
        for name, model in [
            ("canonical_records", CanonicalRecord),
            ("mappings", TransformationMapping),
            ("external_identities", ExternalRecordMapping),
            ("transformation_attempts", TransformationAttempt),
            ("invalid_records", QuarantinedRecord),
        ]
    }
    return {
        **canonical_counts,
        "organizations": 2,
        "connections": 10,
        "crm_records": 10,
        "ats_records": 10,
        "notifications": 2,
        "emails": 2,
        "calendar_events": 4,
    }


def main() -> None:
    engine = build_engine(Settings())
    if engine is None:
        raise SystemExit("DATABASE_URL is required")
    try:
        email = os.environ.get("BOOTSTRAP_EMAIL") or input("Synthetic administrator email: ")
        password = os.environ.get("BOOTSTRAP_PASSWORD") or getpass.getpass(
            "Administrator password (12+ characters): "
        )
        if (
            not os.environ.get("BOOTSTRAP_PASSWORD")
            and getpass.getpass("Confirm password: ") != password
        ):
            raise ValueError("Passwords did not match")
        name = os.environ.get("BOOTSTRAP_DISPLAY_NAME") or input("Display name: ")
        with Session(engine) as db, db.begin():
            counts = seed_demo(db, email, password, name)
        from app.seed_workflows import run_demo

        counts.update(run_demo(engine, email))
        print(f"Synthetic demo seeded without resetting data: {counts}")
    except Exception:  # noqa: BLE001 - never expose credentials/database exception details
        raise SystemExit(
            "Demo seed failed; verify operator credentials, migration and compatible demo connections."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
