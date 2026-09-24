"""Checks the table definition against docs/APP_SPEC.md §4 without a database.

The table is compiled to PostgreSQL SQL text in memory and inspected.
"""

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models import Result

TABLE = Result.__table__

# column -> (PostgreSQL type, nullable), exactly as in the spec table
EXPECTED_COLUMNS = {
    "id": ("BIGSERIAL", False),
    "device_id": ("TEXT", False),
    "test_name": ("TEXT", False),
    "temperature_c": ("DOUBLE PRECISION", False),
    "measured_value": ("DOUBLE PRECISION", False),
    "unit": ("TEXT", False),
    "limit_min": ("DOUBLE PRECISION", True),
    "limit_max": ("DOUBLE PRECISION", True),
    "verdict": ("TEXT", False),
    "started_at": ("TIMESTAMP WITH TIME ZONE", False),
    "duration_s": ("DOUBLE PRECISION", False),
    "received_at": ("TIMESTAMP WITH TIME ZONE", False),
    "source": ("TEXT", False),
}


@pytest.fixture(scope="module")
def ddl() -> str:
    """The CREATE TABLE statement PostgreSQL would receive."""
    return str(CreateTable(TABLE).compile(dialect=postgresql.dialect()))


def test_table_name():
    assert TABLE.name == "test_results"


def test_exactly_the_specified_columns():
    assert [column.name for column in TABLE.columns] == list(EXPECTED_COLUMNS)


@pytest.mark.parametrize("name, expected", EXPECTED_COLUMNS.items())
def test_column_type_and_nullability(ddl, name, expected):
    pg_type, nullable = expected
    # The DDL line of this column, e.g. "source TEXT DEFAULT 'simulator' NOT NULL"
    line = next(
        text.strip().rstrip(",") for text in ddl.splitlines() if text.startswith(f"\t{name} ")
    )

    assert TABLE.columns[name].nullable is nullable
    assert line.startswith(f"{name} {pg_type}")
    assert line.endswith("NOT NULL") is not nullable


def test_id_is_primary_key(ddl):
    assert [column.name for column in TABLE.primary_key] == ["id"]
    assert "PRIMARY KEY (id)" in ddl


def test_timestamps_are_timezone_aware():
    assert TABLE.columns["started_at"].type.timezone is True
    assert TABLE.columns["received_at"].type.timezone is True


def test_server_side_defaults(ddl):
    assert "received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl
    assert "source TEXT DEFAULT 'simulator' NOT NULL" in ddl


def test_no_other_defaults():
    with_default = {c.name for c in TABLE.columns if c.server_default is not None}
    assert with_default == {"received_at", "source"}


def test_verdict_check_constraint(ddl):
    checks = [c for c in TABLE.constraints if isinstance(c, CheckConstraint)]

    assert len(checks) == 1
    assert checks[0].name == "ck_test_results_verdict"
    assert "CHECK (verdict IN ('PASS', 'FAIL'))" in ddl


def test_exactly_the_four_required_indexes():
    indexes = {index.name: [c.name for c in index.columns] for index in TABLE.indexes}

    assert indexes == {
        "ix_test_results_started_at": ["started_at"],
        "ix_test_results_device_id": ["device_id"],
        "ix_test_results_test_name": ["test_name"],
        "ix_test_results_verdict": ["verdict"],
    }
