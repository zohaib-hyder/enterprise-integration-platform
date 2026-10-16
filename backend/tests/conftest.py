"""Real PostgreSQL tests isolated from application data by a run-specific schema."""

import secrets
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_db
from app.core.config import Settings
from app.core.security import password_hasher
from app.main import create_app
from app.models.identity import Membership, User
from app.models.tenant import Tenant


@pytest.fixture(scope="session")
def pg_engine():
    settings = Settings()
    if settings.database_url is None:
        pytest.skip("PostgreSQL integration tests require DATABASE_URL in backend/.env")
    url = settings.database_url.get_secret_value()
    schema = "phase1_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema}"}, pool_size=4, max_overflow=2
    )
    try:
        with engine.begin() as connection:
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        # This schema was created by this fixture, never public/application tables.
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def identity_env(pg_engine):
    connection = pg_engine.connect()
    transaction = connection.begin()
    password = secrets.token_urlsafe(24)
    with Session(
        connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    ) as db:
        a, b = (
            Tenant(name="Synthetic Alpha", slug="synthetic-alpha"),
            Tenant(name="Synthetic Beta", slug="synthetic-beta"),
        )
        db.add_all([a, b])
        db.flush()
        users = {
            role: User(
                email=f"{role.lower()}@example.test",
                display_name=role,
                password_hash=password_hasher.hash(password),
            )
            for role in ["TENANT_ADMIN", "INTEGRATION_ENGINEER", "OPERATIONS_USER"]
        }
        outsider = User(
            email="outsider@example.test",
            display_name="Outsider",
            password_hash=password_hasher.hash(password),
        )
        db.add_all([*users.values(), outsider])
        db.flush()
        members = {
            role: Membership(tenant_id=a.id, user_id=user.id, role=role)
            for role, user in users.items()
        }
        db.add_all(
            [
                *members.values(),
                Membership(tenant_id=b.id, user_id=users["TENANT_ADMIN"].id, role="TENANT_ADMIN"),
            ]
        )
        db.flush()
        db.commit()
    app = create_app(Settings(_env_file=None, app_env="test"))

    def override_db():
        with Session(
            connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    app.dependency_overrides[get_db] = override_db
    tokens = {}

    def delivery(request, invitation, raw):
        tokens[str(invitation.id)] = raw

    with (
        patch("app.services.tenancy.deliver_invitation", side_effect=delivery),
        TestClient(app) as client,
    ):
        yield {
            "client": client,
            "connection": connection,
            "password": password,
            "users": users,
            "outsider": outsider,
            "members": members,
            "a": a,
            "b": b,
            "tokens": tokens,
            "app": app,
        }
    transaction.rollback()
    connection.close()


@pytest.fixture
def db(identity_env):
    with Session(
        identity_env["connection"], join_transaction_mode="create_savepoint", expire_on_commit=False
    ) as db:
        yield db
        db.commit()
