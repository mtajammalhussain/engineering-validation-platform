"""Fixtures for the PostgreSQL integration tests.

Order of every destructive step (enforced by fixture dependencies):
  1. test_db_config  – reads TEST_DB_* and applies the safety guard (fails, never skips)
  2. migrated_db     – ``alembic upgrade head`` on the test database (once per run)
  3. clean_db        – checks current_database() again, then TRUNCATE (before each test)

Nothing runs at import time, so plain ``pytest`` (which deselects these tests) never
touches a database.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.config import Settings
from app.db import create_db_engine
from app.main import create_app
from tests.integration.support import (
    IntegrationConfigError,
    IntegrationDbConfig,
    load_test_db_config,
    run_alembic,
)


@pytest.fixture(scope="session")
def test_db_config() -> IntegrationDbConfig:
    try:
        return load_test_db_config(os.environ)
    except IntegrationConfigError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture(scope="session")
def test_settings(test_db_config) -> Settings:
    return test_db_config.settings()


@pytest.fixture(scope="session")
def migrated_db(test_db_config) -> IntegrationDbConfig:
    """Bring the test database schema to head with the real migrations."""
    run_alembic(test_db_config, "upgrade", "head")
    return test_db_config


@pytest.fixture(scope="session")
def engine(migrated_db, test_settings) -> Engine:
    """A real engine for direct SQL in tests (built by app.db, like the application's)."""
    db_engine = create_db_engine(test_settings)
    yield db_engine
    db_engine.dispose()


@pytest.fixture
def clean_db(migrated_db, engine) -> Engine:
    """Empty test_results and reset the id sequence, so every test starts identically."""
    with engine.begin() as connection:
        current = connection.execute(text("SELECT current_database()")).scalar_one()
        if current != migrated_db.name or not current.endswith("_test"):
            pytest.fail(f"Refusing to TRUNCATE: connected to {current!r}", pytrace=False)
        connection.execute(text("TRUNCATE test_results RESTART IDENTITY"))
    return engine


@pytest.fixture
def client(clean_db, test_settings) -> TestClient:
    """The real application against the test database; ``with`` runs startup and shutdown."""
    with TestClient(create_app(test_settings)) as test_client:
        yield test_client
