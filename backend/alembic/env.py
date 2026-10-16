from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import Settings
from app.db.base import Base
from app.models import (
    ai,  # noqa: F401
    canonical,  # noqa: F401
    identity,  # noqa: F401
    integration,  # noqa: F401
    notification,  # noqa: F401
    operations,  # noqa: F401
    reliability,  # noqa: F401
    synchronization,  # noqa: F401
    webhook,  # noqa: F401
    workflow,  # noqa: F401
)
from app.models.tenant import Tenant  # noqa: F401

settings = Settings()
if settings.database_url is None:
    raise RuntimeError("DATABASE_URL is required for migrations")
url = settings.database_url.get_secret_value()
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    provided_connection = context.config.attributes.get("connection")
    if provided_connection is not None:
        context.configure(
            connection=provided_connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(url, poolclass=pool.NullPool)
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=target_metadata, compare_type=True
            )
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()
