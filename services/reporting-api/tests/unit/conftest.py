"""Fixtures shared by the unit tests (pytest loads this file automatically).

Unit tests must not depend on the developer's shell: after ``set -a; source .env; set +a``
the shell contains real DB_* values, which would otherwise change what Settings() reads.
"""

import os

import pytest

from app.config import Settings

# Every variable Settings could read, plus RESULTS_API_KEY (a Results API variable that
# must have no effect here). pydantic-settings matches names case-insensitively, so the
# comparison below is done in lower case.
SETTINGS_VARIABLES = set(Settings.model_fields) | {"results_api_key"}

VALID_ENV = {
    "PORT": "8002",
    "DB_HOST": "db.example.invalid",
    "DB_PORT": "5432",
    "DB_NAME": "evp",
    "DB_USER": "evp_reader",
    "DB_PASSWORD": "super-secret-password",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Remove every settings-related variable from the environment for each test.

    monkeypatch restores the original environment after the test.
    """
    for name in list(os.environ):
        if name.lower() in SETTINGS_VARIABLES:
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def valid_env(isolated_env):
    """A complete, valid environment with fake values; optional variables stay unset."""
    for name, value in VALID_ENV.items():
        isolated_env.setenv(name, value)
    return isolated_env


@pytest.fixture
def make_settings():
    """Build Settings from explicit values (all required fields given), not from the env."""

    def _make(**overrides) -> Settings:
        values = {
            "port": 8002,
            "db_host": "db.example.invalid",
            "db_port": 5432,
            "db_name": "evp",
            "db_user": "evp_reader",
            "db_password": "super-secret-password",
        }
        values.update(overrides)
        return Settings(**values)

    return _make
