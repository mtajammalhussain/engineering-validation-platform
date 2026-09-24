"""Alembic environment: how migrations connect to the database.

The schema comes only from app.models (Base.metadata) and the connection only from
app.config + app.db, so nothing about the table or the database URL is defined twice.
Migrations run only via the ``alembic`` command, never on application startup.
"""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.config import load_settings
from app.db import build_database_url
from app.logging_config import configure_logging
from app.models import Base

settings = load_settings()
configure_logging(settings)

# The models are the single source of truth for the schema (used by autogenerate).
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """``alembic upgrade head --sql``: print the SQL instead of running it (no connection)."""
    context.configure(
        url=build_database_url(settings),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and run the migrations inside a transaction.

    NullPool instead of the application's pool: a migration is a one-off process with a
    single connection, so keeping connections open for reuse (and pre-pinging them)
    brings no benefit. NullPool closes the connection as soon as the migration ends.
    """
    engine = create_engine(build_database_url(settings), poolclass=NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
