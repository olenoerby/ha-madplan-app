from __future__ import annotations

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context

import app.models  # noqa: F401  (registers the tables on Base.metadata)
from app.core.db import Base, UTCDateTime, engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Keeps migrations free of application code, so that they stay valid as the code changes."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if type_ == "type" and isinstance(obj, sa.JSON):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')"
    return False


def run_migrations_online() -> None:
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            # Batch migrations rebuild tables, which SQLite refuses while it enforces foreign keys.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            # The statement above began a transaction. Alembic must start its own, or nothing is kept.
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
