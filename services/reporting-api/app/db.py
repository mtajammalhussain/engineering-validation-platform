"""Database connection setup (docs/APP_SPEC.md §2, §7, §9).

Nothing here connects at import time. ``create_db_engine`` only prepares a connection pool;
the first real connection is opened when a query runs.
"""

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

# Seconds libpq waits while opening a NEW connection before giving up (its default is to
# wait forever). Keeps /ready and normal requests from hanging when PostgreSQL is
# unreachable. Does not limit how long a query runs on an established connection.
# Kubernetes probe timeouts (timeoutSeconds) must be set longer than this.
DB_CONNECT_TIMEOUT_S = 3

# libpq "options": server settings applied when each connection starts. With this one,
# every transaction on the connection starts read-only, so an accidental INSERT/UPDATE/
# DELETE/DDL from application code is refused by PostgreSQL (docs/APP_SPEC.md §7).
# It is only a default that a session could override; the SELECT-only database user
# remains the real security boundary.
DB_READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"


def build_database_url(settings: Settings) -> URL:
    """Build the PostgreSQL URL from the separate DB_* settings.

    ``URL.create`` takes each part separately, so special characters in the password
    (e.g. ``@``, ``:`` or ``/``) cannot break the URL. Printing the URL shows ``***``
    instead of the password.
    """
    return URL.create(
        drivername="postgresql+psycopg2",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )


def create_db_engine(settings: Settings) -> Engine:
    """Create the engine (connection pool).

    ``pool_pre_ping=True`` tests each pooled connection before use and replaces dead ones,
    e.g. after a database restart. ``connect_args`` is passed unchanged to psycopg2/libpq;
    ``connect_timeout`` and ``options`` are separate libpq parameters, so both apply.
    """
    return create_engine(
        build_database_url(settings),
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": DB_CONNECT_TIMEOUT_S,
            "options": DB_READ_ONLY_OPTIONS,
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a factory that creates database sessions bound to the given engine."""
    return sessionmaker(bind=engine)
