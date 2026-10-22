"""migrations/env.py needs only the migration settings (docs/APP_SPEC.md §2 rule 7, §9).

The Alembic checks run ``alembic upgrade head --sql`` (offline mode): Alembic only renders
the migration SQL and never opens a database connection. The child process gets a minimal
environment, so the developer's shell (PORT, RESULTS_API_KEY, a real .env) cannot leak in.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[2]
ENV_PY = SERVICE_DIR / "migrations" / "env.py"
HEADER = "Invalid configuration, service cannot start:"
PASSWORD = "offline-password-must-not-leak"
DB_ENV = {
    "DB_HOST": "db.example.invalid",
    "DB_PORT": "5432",
    "DB_NAME": "evp",
    "DB_USER": "evp_writer",
    "DB_PASSWORD": PASSWORD,
}


def run_offline_upgrade(db_env: dict[str, str]) -> subprocess.CompletedProcess:
    """Render the migration SQL; no PORT, no RESULTS_API_KEY, no database connection."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=SERVICE_DIR,
        env={"PATH": os.environ.get("PATH", ""), **db_env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_env_py_loads_only_the_migration_settings():
    tree = ast.parse(ENV_PY.read_text())

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.config"
        for alias in node.names
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_migration_settings"
    ]

    assert imported == {"load_migration_settings"}
    assert "Settings" not in names
    assert "load_settings" not in names
    assert len(calls) == 1


def test_offline_migration_works_without_api_only_variables():
    completed = run_offline_upgrade(DB_ENV)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "CREATE TABLE test_results" in completed.stdout
    assert "alembic_version" in completed.stdout
    assert PASSWORD not in completed.stdout + completed.stderr


@pytest.mark.parametrize("missing", list(DB_ENV))
def test_offline_migration_fails_fast_without_a_db_variable(missing):
    completed = run_offline_upgrade({k: v for k, v in DB_ENV.items() if k != missing})

    assert completed.returncode == 1
    lines = completed.stdout.splitlines()
    assert lines[0] == HEADER
    assert lines[1:] == [f"  {missing}: Field required"]
    assert PASSWORD not in completed.stdout + completed.stderr
