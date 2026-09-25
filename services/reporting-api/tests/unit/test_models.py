"""The read-only ``test_results`` description (no database, no network, no configuration).

These tests check the contract as encoded in the Reporting API. They do not import the
Results API. Whether it matches the real, migrated table is proven by the integration tests.
"""

import inspect
import subprocess
import sys
from pathlib import Path

from sqlalchemy import DateTime, Engine, MetaData, Table, Text, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase

import app.models
from app.models import metadata, test_results

SERVICE_DIR = Path(__file__).resolve().parents[2]
EXPECTED_COLUMNS = {
    "device_id": Text,
    "test_name": Text,
    "verdict": Text,
    "started_at": DateTime,
}


def test_importing_models_needs_no_config_database_or_driver():
    # Fresh process, empty environment. psycopg2 (the PostgreSQL driver) is only loaded
    # when an engine is created, so it must not be in sys.modules after the import.
    code = "import sys, app.models; print('psycopg2' in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=SERVICE_DIR, env={}, capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"
    assert completed.stderr == ""


def test_table_name_is_test_results():
    assert test_results.name == "test_results"


def test_is_a_core_table_not_an_orm_model():
    assert isinstance(test_results, Table)
    assert not hasattr(test_results, "__mapper__")
    for value in vars(app.models).values():
        assert not (isinstance(value, type) and issubclass(value, DeclarativeBase))


def test_contains_exactly_the_four_report_columns():
    assert list(test_results.columns.keys()) == list(EXPECTED_COLUMNS)


def test_column_types_match_the_local_contract():
    for name, expected_type in EXPECTED_COLUMNS.items():
        column_type = test_results.c[name].type
        assert type(column_type) is expected_type, name


def test_started_at_is_timezone_aware():
    assert test_results.c.started_at.type.timezone is True


def test_no_constraints_indexes_or_keys_are_declared():
    assert test_results.indexes == set()
    assert list(test_results.primary_key.columns) == []
    assert all(not column.foreign_keys for column in test_results.columns)
    # Only the implicit (empty) primary key constraint that every Core Table has.
    assert {type(c).__name__ for c in test_results.constraints} == {"PrimaryKeyConstraint"}


def test_metadata_describes_only_this_table():
    assert isinstance(metadata, MetaData)
    assert test_results.metadata is metadata
    assert list(metadata.tables) == ["test_results"]


def test_module_has_no_engine_functions_or_write_helpers():
    public = {name for name in vars(app.models) if not name.startswith("_")}
    own_functions = [
        name for name, value in vars(app.models).items()
        if inspect.isfunction(value) and value.__module__ == "app.models"
    ]

    assert own_functions == []
    assert not any(isinstance(value, Engine) for value in vars(app.models).values())
    assert {"metadata", "test_results"} <= public


def test_source_has_no_reflection_schema_creation_or_sibling_imports():
    source = inspect.getsource(app.models)

    for forbidden in (
        "autoload", "reflect", "create_all", "drop_all", "create_engine", "alembic",
        "results-api", "results_api", "sys.path", "insert(", "update(", "delete(",
    ):
        assert forbidden not in source, forbidden


def test_table_supports_core_aggregation_queries():
    # Only compiled to SQL text here, never executed: proves the description is usable.
    passed = func.count().filter(test_results.c.verdict == "PASS")
    bucket = func.date_trunc("hour", test_results.c.started_at)
    statement = (
        select(test_results.c.device_id, passed, bucket)
        .group_by(test_results.c.device_id, bucket)
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FROM test_results" in sql
    assert "count(*) FILTER (WHERE test_results.verdict" in sql
    assert "date_trunc(" in sql
    assert "GROUP BY test_results.device_id" in sql
