"""Readiness, the connection settings and query cancellation against real PostgreSQL.

default_transaction_read_only=on is an application connection defence: it stops ordinary
application code from writing by accident. It is NOT the database authorization boundary
(a session could switch it off); the SELECT-only evp_reader role is proven in Stage 8.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.main import QUERY_CANCELED_SQLSTATE, is_query_canceled
from tests.integration.support import (
    COUNT_MARKER_SQL,
    INSERT_SQL,
    MARKER_PREFIX,
    READONLY_ATTEMPT_AT,
    result_row,
)

pytestmark = pytest.mark.integration

READ_ONLY_SQL_TRANSACTION = "25006"


def test_ready_returns_ok_with_real_database(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_application_connection_is_read_only_and_rejects_writes(client, writer_engine):
    # The engine the real lifespan created via app.db.create_db_engine (not the writer).
    app_engine = client.app.state.engine
    marker = f"{MARKER_PREFIX}readonly-{uuid.uuid4()}"
    # Fully valid row in a period no report test queries, so a CHECK or NOT NULL
    # violation cannot be the reason for the failure.
    row = result_row(READONLY_ATTEMPT_AT, "ECU-901", "Sleep Current", "PASS", marker)

    with app_engine.connect() as connection:
        setting = connection.execute(text("SHOW default_transaction_read_only")).scalar_one()
        assert setting == "on"

        with pytest.raises(DBAPIError) as exc_info:
            connection.execute(INSERT_SQL, row)
        connection.rollback()

    assert exc_info.value.orig.pgcode == READ_ONLY_SQL_TRANSACTION

    with writer_engine.connect() as connection:
        stored = connection.execute(COUNT_MARKER_SQL, {"marker": marker}).scalar_one()
    assert stored == 0


def test_application_connection_has_statement_timeout_and_read_only_default(client):
    # The settings come from app.db's libpq options on the application's own engine;
    # a plain psql session would not show them.
    with client.app.state.engine.connect() as connection:
        timeout = connection.execute(text("SHOW statement_timeout")).scalar_one()
        read_only = connection.execute(text("SHOW default_transaction_read_only")).scalar_one()

    assert timeout == "10s"
    assert read_only == "on"


def test_application_recovers_after_a_real_query_cancellation(client):
    """A real SQLSTATE 57014, then normal work through the application's session factory.

    The first session lowers the timeout for its own transaction only (SET LOCAL 50ms), so
    PostgreSQL cancels pg_sleep(1) quickly instead of waiting 10 seconds. The session is
    closed in ``finally`` exactly like app.dependencies.get_db does after a failed request.

    This proves recovery after a real 57014 with the normal session cleanup: a following
    session from the same factory sees statement_timeout=10s, ordinary SQL succeeds and
    /ready is 200. It does not prove which physical connection the following checkout
    received, so it does not claim that the same connection was reset.
    """
    session_factory = client.app.state.session_factory

    session = session_factory()
    try:
        session.execute(text("SET LOCAL statement_timeout = '50ms'"))
        with pytest.raises(OperationalError) as exc_info:
            session.execute(text("SELECT pg_sleep(1)"))
    finally:
        session.close()

    assert exc_info.value.orig.pgcode == QUERY_CANCELED_SQLSTATE == "57014"
    assert is_query_canceled(exc_info.value)

    session = session_factory()
    try:
        timeout = session.execute(text("SHOW statement_timeout")).scalar_one()
        one = session.execute(text("SELECT 1")).scalar_one()
    finally:
        session.close()

    assert timeout == "10s"
    assert one == 1

    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
