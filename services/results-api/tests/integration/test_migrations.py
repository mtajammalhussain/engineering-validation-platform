"""The real Alembic migration against PostgreSQL: upgrade -> downgrade -> upgrade."""

import pytest
from sqlalchemy import Engine, inspect

from tests.integration.support import run_alembic

pytestmark = pytest.mark.integration

EXPECTED_INDEXES = {
    "ix_test_results_device_id",
    "ix_test_results_started_at",
    "ix_test_results_test_name",
    "ix_test_results_verdict",
}
NULLABLE_COLUMNS = {"limit_min", "limit_max"}


def assert_schema_at_head(engine: Engine) -> None:
    inspector = inspect(engine)
    assert inspector.has_table("test_results")

    columns = {c["name"]: c for c in inspector.get_columns("test_results")}
    assert len(columns) == 13
    assert {n for n, c in columns.items() if c["nullable"]} == NULLABLE_COLUMNS
    assert columns["received_at"]["default"] == "now()"
    assert columns["source"]["default"] == "'simulator'::text"
    assert columns["started_at"]["type"].timezone is True

    assert {i["name"] for i in inspector.get_indexes("test_results")} == EXPECTED_INDEXES
    checks = inspector.get_check_constraints("test_results")
    assert [c["name"] for c in checks] == ["ck_test_results_verdict"]


def test_migration_round_trip(migrated_db, engine):
    try:
        assert "(head)" in run_alembic(migrated_db, "current")
        assert_schema_at_head(engine)

        run_alembic(migrated_db, "downgrade", "base")
        assert not inspect(engine).has_table("test_results")
        assert "(head)" not in run_alembic(migrated_db, "current")

        run_alembic(migrated_db, "upgrade", "head")
        assert "(head)" in run_alembic(migrated_db, "current")
        assert_schema_at_head(engine)
    finally:
        # Leave the test database at head even if an assertion above failed.
        run_alembic(migrated_db, "upgrade", "head")
