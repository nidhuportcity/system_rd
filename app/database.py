"""Lazy PostgreSQL connections; importing the API never contacts the database."""
from functools import lru_cache

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url or URL.create(
        "postgresql+psycopg", username=settings.postgres_user,
        password=settings.postgres_password, host=settings.postgres_host,
        port=settings.postgres_port, database=settings.postgres_db,
    )
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
