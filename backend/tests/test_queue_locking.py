# ruff: noqa:F811
"""A claim must skip a tenant held by execution/recovery instead of reversing locks."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_notifications import configured, manual
from test_workflows import dispatched, setup, workflow_engine  # noqa:F401

from app.models.tenant import Tenant
from app.services import notifications
from app.workflows import engine as worker


@pytest.mark.parametrize("domain", ["workflow", "notification"])
def test_claim_skips_busy_tenant_without_queue_mutation(workflow_engine, domain):
    engine = workflow_engine
    if domain == "workflow":
        ids = setup(engine)
        dispatched(engine, ids)
        wid = ids[3]
        function = worker.claim
    else:
        ids = configured(engine)
        manual(engine, ids)
        wid = ids[3]
        function = notifications.claim
    with Session(engine) as holding, holding.begin():
        tenant = holding.scalar(
            select(Tenant).where(Tenant.slug == "demo-recruiting-alpha").with_for_update()
        )
        assert tenant

        def contender():
            with Session(engine) as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout='200ms'"))
                return function(db, wid)

        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(contender).result(timeout=3) is None
    with Session(engine) as db, db.begin():
        assert function(db, wid) is not None
