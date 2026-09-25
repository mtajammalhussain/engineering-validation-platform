"""The integration suite's safety guard and schema checks (no database needed)."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.support import (
    MARKER_PREFIX,
    REQUIRED_VARS,
    IntegrationConfigError,
    check_schema,
    load_test_db_config,
    main_dataset,
    period_not_empty_message,
)

SERVICE_DIR = Path(__file__).resolve().parents[2]
PASSWORD = "test-password-must-not-leak"
VALID = {
    "TEST_DB_HOST": "localhost",
    "TEST_DB_PORT": "5432",
    "TEST_DB_NAME": "evp_test",
    "TEST_DB_USER": "evp_writer",
    "TEST_DB_PASSWORD": PASSWORD,
    "DB_NAME": "evp",
}


# --- TEST_DB_* guard -------------------------------------------------------------------------

def test_valid_configuration_is_accepted():
    config = load_test_db_config(VALID)

    assert (config.host, config.port, config.name, config.user) == (
        "localhost", 5432, "evp_test", "evp_writer",
    )
    assert config.settings().db_name == "evp_test"


def test_db_name_may_be_absent():
    # e.g. in CI, where only TEST_DB_* is set
    environ = {k: v for k, v in VALID.items() if k != "DB_NAME"}

    assert load_test_db_config(environ).name == "evp_test"


@pytest.mark.parametrize("name", REQUIRED_VARS)
@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
def test_missing_or_empty_variable_is_refused(name, value):
    environ = dict(VALID)
    if value is None:
        del environ[name]
    else:
        environ[name] = value

    with pytest.raises(IntegrationConfigError, match=name):
        load_test_db_config(environ)


def test_db_variables_are_never_used_as_fallback():
    environ = {"DB_HOST": "h", "DB_PORT": "5432", "DB_NAME": "evp_test", "DB_USER": "u",
               "DB_PASSWORD": "p"}

    with pytest.raises(IntegrationConfigError, match="Missing or empty"):
        load_test_db_config(environ)


@pytest.mark.parametrize("name", ["evp", "evp_testing", "test_evp", "evp-test", "evp_TEST"])
def test_database_name_without_test_suffix_is_refused(name):
    with pytest.raises(IntegrationConfigError, match="does not end with '_test'"):
        load_test_db_config({**VALID, "TEST_DB_NAME": name})


def test_database_name_equal_to_db_name_is_refused():
    with pytest.raises(IntegrationConfigError, match="same as DB_NAME"):
        load_test_db_config({**VALID, "DB_NAME": "evp_test"})


@pytest.mark.parametrize("port", ["abc", "54 32", "5432.0"])
def test_non_numeric_port_is_refused(port):
    with pytest.raises(IntegrationConfigError, match="TEST_DB_PORT"):
        load_test_db_config({**VALID, "TEST_DB_PORT": port})


@pytest.mark.parametrize(
    "overrides",
    [{"TEST_DB_NAME": "evp"}, {"TEST_DB_HOST": ""}, {"TEST_DB_PORT": "x"},
     {"DB_NAME": "evp_test"}],
)
def test_error_messages_never_contain_the_password(overrides):
    with pytest.raises(IntegrationConfigError) as exc_info:
        load_test_db_config({**VALID, **overrides})

    assert PASSWORD not in str(exc_info.value)


def test_target_description_never_contains_the_password():
    text = load_test_db_config(VALID).describe()

    assert "TEST_DB_NAME=evp_test" in text
    assert PASSWORD not in text


# --- collection-time safety -------------------------------------------------------------------

def test_importing_integration_modules_needs_no_configuration_or_driver():
    # Plain pytest imports these modules while collecting: they must not read TEST_DB_*,
    # connect, or even load the PostgreSQL driver.
    code = (
        "import sys, tests.integration.support, tests.integration.conftest; "
        "print('psycopg2' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=SERVICE_DIR, env={}, capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


# --- schema precondition (with fake information_schema rows) --------------------------------

def column(name, data_type, nullable="NO", default=None):
    return SimpleNamespace(
        column_name=name, data_type=data_type, is_nullable=nullable, column_default=default
    )


CURRENT_SCHEMA = [
    column("id", "bigint", default="nextval('test_results_id_seq'::regclass)"),
    column("device_id", "text"),
    column("test_name", "text"),
    column("temperature_c", "double precision"),
    column("measured_value", "double precision"),
    column("unit", "text"),
    column("limit_min", "double precision", nullable="YES"),
    column("limit_max", "double precision", nullable="YES"),
    column("verdict", "text"),
    column("started_at", "timestamp with time zone"),
    column("duration_s", "double precision"),
    column("received_at", "timestamp with time zone", default="now()"),
    column("source", "text", default="'simulator'::text"),
]


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement):
        return iter(self.rows)


def test_current_results_schema_passes():
    check_schema(FakeConnection(CURRENT_SCHEMA))


def test_missing_table_fails_with_migration_hint():
    with pytest.raises(AssertionError, match="Results API migrations must be applied"):
        check_schema(FakeConnection([]))


def test_wrong_column_type_fails():
    rows = [column("started_at", "timestamp without time zone") if r.column_name == "started_at"
            else r for r in CURRENT_SCHEMA]

    with pytest.raises(AssertionError, match="started_at is 'timestamp without time zone'"):
        check_schema(FakeConnection(rows))


def test_missing_column_fails():
    rows = [r for r in CURRENT_SCHEMA if r.column_name != "verdict"]

    with pytest.raises(AssertionError, match="column verdict is missing"):
        check_schema(FakeConnection(rows))


def test_new_required_column_not_supplied_by_fixtures_fails():
    rows = [*CURRENT_SCHEMA, column("bench_serial", "text")]

    with pytest.raises(AssertionError, match="bench_serial"):
        check_schema(FakeConnection(rows))


# --- fixture data and messages ------------------------------------------------------------

def test_main_dataset_uses_the_marker_and_realistic_device_ids():
    rows = main_dataset(f"{MARKER_PREFIX}abc")

    assert len(rows) == 13
    assert {r["source"] for r in rows} == {f"{MARKER_PREFIX}abc"}
    assert {r["device_id"] for r in rows} == {"ECU-901", "ECU-902", "ECU-903"}
    assert all(r["started_at"].tzinfo is not None for r in rows)


def test_contaminated_period_message_gives_counts_and_manual_cleanup_only():
    message = period_not_empty_message(total=5, marked=3, database="evp_test")

    assert "5 row(s)" in message and "3 of them" in message and "'evp_test'" in message
    assert "MANUAL CLEANUP ONLY - NOT EXECUTED BY THE TEST SUITE" in message
    assert "DELETE FROM test_results WHERE source LIKE 'reporting-it-%';" in message
