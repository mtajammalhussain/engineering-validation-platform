"""Fixtures for the PostgreSQL integration tests.

Order of every step that touches the database (enforced by fixture dependencies):
  1. test_db_config  - reads TEST_DB_* and applies the safety guard (fails, never skips)
  2. writer_engine   - test-only connection; checks current_database() before anything else
  3. schema_checked  - the Results-owned table exists with the expected column types
  4. seeded          - the isolated report period is empty (ALL rows), then fixture rows
                       with this session's marker are inserted; afterwards exactly those
                       rows are deleted again (fixture finalisation, also after failures)
  5. client          - the real Reporting API (create_app + lifespan + its own engine)

The writer connection only prepares and removes fixture rows. The application under test
builds its own engine via app.db (read-only transactions by default); it is never
replaced by the writer engine.

Must not run concurrently with the Results API integration suite: that suite TRUNCATEs
test_results in the same test database.

Nothing runs at import time, so plain ``pytest`` (which deselects these tests) never
touches a database.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.main import create_app
from tests.integration.support import (
    COUNT_IN_PERIOD_SQL,
    COUNT_MARKER_SQL,
    DELETE_MARKER_SQL,
    INSERT_SQL,
    MAIN_FROM,
    MAIN_TO,
    MARKER_PREFIX,
    IntegrationConfigError,
    IntegrationDbConfig,
    check_schema,
    load_test_db_config,
    main_dataset,
    period_not_empty_message,
)


@pytest.fixture(scope="session")
def test_db_config(request) -> IntegrationDbConfig:
    try:
        config = load_test_db_config(os.environ)
    except IntegrationConfigError as exc:
        pytest.fail(str(exc), pytrace=False)
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\nReporting integration target: {config.describe()}")
    return config


@pytest.fixture(scope="session")
def writer_engine(test_db_config) -> Engine:
    """Test-only writer connection (no read-only default). Verified before any write."""
    engine = create_engine(test_db_config.url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            current = connection.execute(text("SELECT current_database()")).scalar_one()
        if current != test_db_config.name or not current.endswith("_test"):
            pytest.fail(
                f"Refusing to run: connected to {current!r}, expected {test_db_config.name!r}",
                pytrace=False,
            )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def schema_checked(writer_engine) -> None:
    with writer_engine.connect() as connection:
        try:
            check_schema(connection)
        except AssertionError as exc:
            pytest.fail(str(exc), pytrace=False)


@pytest.fixture(scope="session")
def session_marker() -> str:
    return f"{MARKER_PREFIX}{uuid.uuid4()}"


@pytest.fixture(scope="session")
def seeded(writer_engine, schema_checked, test_db_config, session_marker) -> str:
    """Insert the main dataset once per session; delete exactly those rows afterwards."""
    with writer_engine.connect() as connection:
        found = connection.execute(
            COUNT_IN_PERIOD_SQL,
            {"prefix": f"{MARKER_PREFIX}%", "start": MAIN_FROM, "end": MAIN_TO},
        ).one()
    if found.total:
        pytest.fail(
            period_not_empty_message(found.total, found.marked, test_db_config.name),
            pytrace=False,
        )

    try:
        with writer_engine.begin() as connection:
            connection.execute(INSERT_SQL, main_dataset(session_marker))
        yield session_marker
    finally:
        with writer_engine.begin() as connection:
            connection.execute(DELETE_MARKER_SQL, {"marker": session_marker})
        with writer_engine.connect() as connection:
            left = connection.execute(COUNT_MARKER_SQL, {"marker": session_marker}).scalar_one()
        assert left == 0, f"cleanup left {left} fixture row(s) with source={session_marker!r}"


@pytest.fixture
def client(seeded, test_db_config) -> TestClient:
    """The real application against the test database; ``with`` runs startup and shutdown."""
    with TestClient(create_app(test_db_config.settings())) as test_client:
        yield test_client
