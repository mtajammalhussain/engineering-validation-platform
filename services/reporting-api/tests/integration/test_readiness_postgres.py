"""Readiness and the read-only transaction default against real PostgreSQL.

default_transaction_read_only=on is an application connection defence: it stops ordinary
application code from writing by accident. It is NOT the database authorization boundary
(a session could switch it off); the SELECT-only evp_reader role is proven in Stage 8.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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
