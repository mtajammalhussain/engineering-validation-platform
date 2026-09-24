"""Helpers for the PostgreSQL integration tests (docs/APP_SPEC.md §11).

Nothing here connects to a database at import time. ``load_test_db_config`` is the safety
guard: every destructive step (Alembic, TRUNCATE, direct SQL) only ever receives a
configuration that has passed it.
"""

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

SERVICE_DIR = Path(__file__).resolve().parents[2]
REQUIRED_VARS = ("TEST_DB_HOST", "TEST_DB_PORT", "TEST_DB_NAME", "TEST_DB_USER", "TEST_DB_PASSWORD")

# Only used inside the test process / Alembic subprocess, never a real secret.
INTEGRATION_API_KEY = "integration-test-api-key"


class IntegrationConfigError(Exception):
    """TEST_DB_* is missing, invalid or points to a database that must not be touched."""


@dataclass(frozen=True)
class IntegrationDbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str

    def settings(self) -> Settings:
        """Application settings pointing at the test database (reuses app.config)."""
        return Settings(
            app_env="dev",
            log_level="WARNING",
            log_format="text",
            port=8001,
            db_host=self.host,
            db_port=self.port,
            db_name=self.name,
            db_user=self.user,
            db_password=self.password,
            results_api_key=INTEGRATION_API_KEY,
        )


def load_test_db_config(environ: Mapping[str, str]) -> IntegrationDbConfig:
    """Read TEST_DB_* and apply the destructive-test safety guard.

    Refuses unless TEST_DB_NAME ends with "_test" and differs from DB_NAME, so a
    development database such as "evp" can never be migrated or truncated by the tests.
    Messages name variables and database names, never the password.
    """
    missing = [name for name in REQUIRED_VARS if not environ.get(name)]
    if missing:
        raise IntegrationConfigError(
            "Integration tests need a real PostgreSQL database. Missing or empty: "
            + ", ".join(missing)
        )

    name = environ["TEST_DB_NAME"]
    if not name.endswith("_test"):
        raise IntegrationConfigError(
            f"Refusing to run: TEST_DB_NAME={name!r} does not end with '_test'."
        )
    if name == environ.get("DB_NAME"):
        raise IntegrationConfigError(
            f"Refusing to run: TEST_DB_NAME={name!r} is the same as DB_NAME."
        )

    try:
        port = int(environ["TEST_DB_PORT"])
    except ValueError:
        raise IntegrationConfigError("TEST_DB_PORT must be a number.") from None

    return IntegrationDbConfig(
        host=environ["TEST_DB_HOST"],
        port=port,
        name=name,
        user=environ["TEST_DB_USER"],
        password=environ["TEST_DB_PASSWORD"],
    )


def alembic_env(config: IntegrationDbConfig) -> dict[str, str]:
    """Environment for an Alembic subprocess: the validated TEST_DB_* values as DB_*.

    Only this child process sees the mapping; the developer's shell is not changed.
    PORT and RESULTS_API_KEY are required by Settings (known M4 item) and get test values.
    """
    env = dict(os.environ)
    env.update(
        DB_HOST=config.host,
        DB_PORT=str(config.port),
        DB_NAME=config.name,
        DB_USER=config.user,
        DB_PASSWORD=config.password,
        PORT="8001",
        RESULTS_API_KEY=INTEGRATION_API_KEY,
        APP_ENV="dev",
        LOG_LEVEL="WARNING",
        LOG_FORMAT="text",
    )
    return env


def run_alembic(config: IntegrationDbConfig, *args: str) -> str:
    """Run ``alembic <args>`` against the (already validated) test database."""
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVICE_DIR,
        env=alembic_env(config),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed (exit {completed.returncode}):\n"
            + completed.stdout
            + completed.stderr
        )
    return completed.stdout + completed.stderr
