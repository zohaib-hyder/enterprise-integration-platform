from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings


def build_engine(settings: Settings) -> Engine | None:
    if settings.database_url is None:
        return None
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        connect_args={"connect_timeout": 3},
    )


def check_database(engine: Engine | None) -> bool:
    if engine is None:
        return False
    with engine.connect() as connection:
        return connection.scalar(text("SELECT 1")) == 1
