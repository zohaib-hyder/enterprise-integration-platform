"""Verify migrated constraints with rollback-only synthetic writes; run from backend."""

import uuid

from app.core.config import Settings
from app.db.session import build_engine
from app.models.tenant import Tenant
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

engine = build_engine(Settings())
if engine is None:
    raise SystemExit("Database configuration required")
try:
    indexes = {index["name"]: index for index in inspect(engine).get_indexes("tenants")}
    assert indexes["ix_tenants_slug"]["unique"]
    assert "ix_tenants_created_at" in indexes
    with Session(engine) as session:
        slug = f"verify-{uuid.uuid4()}"
        tenant = Tenant(name="Synthetic verification tenant", slug=slug)
        session.add(tenant)
        session.flush()
        session.refresh(tenant)
        assert isinstance(tenant.id, uuid.UUID)
        assert tenant.created_at.tzinfo is not None
        assert tenant.updated_at.tzinfo is not None
        try:
            with session.begin_nested():
                session.add(Tenant(name="Synthetic duplicate", slug=slug))
                session.flush()
        except IntegrityError:
            pass
        else:
            raise AssertionError("Duplicate slug accepted")
        tenant_id = tenant.id
        session.rollback()
        assert session.scalar(select(Tenant).where(Tenant.id == tenant_id)) is None
    print(
        "Tenant UUID, timezone timestamps, indexes, uniqueness and rollback verified."
    )
finally:
    engine.dispose()
