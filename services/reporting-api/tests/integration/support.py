"""Helpers for the PostgreSQL integration tests (docs/APP_SPEC.md §11).

Nothing here connects to a database at import time. ``load_test_db_config`` is the safety
guard: the test-only writer connection is only ever built from a configuration that has
passed it, and it writes nothing before ``current_database()`` has been checked too.

Schema ownership: the ``test_results`` table belongs to the Results API and its Alembic
migrations (services/results-api/migrations/). These tests never create, migrate or alter
it; they only check that it exists with the expected column types, and fail otherwise.

Fixture rows are marked with a per-session ``source`` value (``reporting-it-<uuid>``) and
lie in a fixed historical period (June 2001), so they neither disturb nor are disturbed by
other data in the test database. Only rows with the current marker are ever deleted.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import URL, Connection, text

from app.config import Settings

REQUIRED_VARS = ("TEST_DB_HOST", "TEST_DB_PORT", "TEST_DB_NAME", "TEST_DB_USER", "TEST_DB_PASSWORD")

MARKER_PREFIX = "reporting-it-"

# The main report period. Every normal report test queries only inside it; it must be
# empty (of ALL rows, not only ours) before seeding, or totals would be falsified.
MAIN_FROM = datetime(2001, 6, 20, tzinfo=timezone.utc)
MAIN_TO = datetime(2001, 6, 22, tzinfo=timezone.utc)

# A separate period that no report test queries: used only by the read-only INSERT attempt.
READONLY_ATTEMPT_AT = datetime(2001, 7, 15, 12, 0, tzinfo=timezone.utc)

# Column contract of the Reporting API's app/models.py, as PostgreSQL reports data types.
EXPECTED_COLUMN_TYPES = {
    "device_id": "text",
    "test_name": "text",
    "verdict": "text",
    "started_at": "timestamp with time zone",
}

MISSING_SCHEMA_HINT = (
    "Reporting integration-test precondition failed: Results API migrations must be "
    "applied to TEST_DB_NAME before running these tests "
    "(e.g. run the Results API integration suite, or 'alembic upgrade head' in "
    "services/results-api with DB_* pointing at the test database)."
)


class IntegrationConfigError(Exception):
    """TEST_DB_* is missing, invalid or points to a database that must not be touched."""


@dataclass(frozen=True)
class IntegrationDbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str

    def url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg2",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
        )

    def settings(self) -> Settings:
        """Reporting API settings pointing at the test database (built explicitly)."""
        return Settings(
            app_env="dev",
            log_level="WARNING",
            log_format="text",
            port=8002,
            db_host=self.host,
            db_port=self.port,
            db_name=self.name,
            db_user=self.user,
            db_password=self.password,
        )

    def describe(self) -> str:
        """Target description for the terminal; never contains the password."""
        return (
            f"TEST_DB_HOST={self.host} TEST_DB_PORT={self.port} "
            f"TEST_DB_NAME={self.name} TEST_DB_USER={self.user}"
        )


def load_test_db_config(environ: Mapping[str, str]) -> IntegrationDbConfig:
    """Read TEST_DB_* (never DB_*) and apply the test-database safety guard.

    Refuses unless every TEST_DB_* variable is set and non-empty, TEST_DB_NAME ends with
    "_test" and differs from DB_NAME, and TEST_DB_PORT is a number. Messages name
    variables and database names, never the password.
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


# --- test-only SQL (parameterised; values are always bound, never formatted in) -----------

COLUMN_INFO_SQL = text(
    "SELECT column_name, data_type, is_nullable, column_default "
    "FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'test_results'"
)

COUNT_IN_PERIOD_SQL = text(
    "SELECT count(*) AS total, "
    "count(*) FILTER (WHERE source LIKE :prefix) AS marked "
    "FROM test_results WHERE started_at >= :start AND started_at < :end"
)

INSERT_SQL = text(
    "INSERT INTO test_results (device_id, test_name, temperature_c, measured_value, unit, "
    "limit_min, limit_max, verdict, started_at, duration_s, source) "
    "VALUES (:device_id, :test_name, :temperature_c, :measured_value, :unit, "
    ":limit_min, :limit_max, :verdict, :started_at, :duration_s, :source)"
)

COUNT_MARKER_SQL = text("SELECT count(*) FROM test_results WHERE source = :marker")

DELETE_MARKER_SQL = text("DELETE FROM test_results WHERE source = :marker")

# Columns this suite supplies in INSERT_SQL (all others must be nullable or have a default).
INSERTED_COLUMNS = {
    "device_id", "test_name", "temperature_c", "measured_value", "unit", "limit_min",
    "limit_max", "verdict", "started_at", "duration_s", "source",
}


def check_schema(connection: Connection) -> None:
    """Fail unless public.test_results exists with the column types Reporting relies on.

    Also checks that every NOT NULL column without a default is supplied by the fixture
    INSERT, so a changed Results schema fails here with a clear message.
    """
    columns = {row.column_name: row for row in connection.execute(COLUMN_INFO_SQL)}
    if not columns:
        raise AssertionError(f"public.test_results does not exist. {MISSING_SCHEMA_HINT}")

    problems = []
    for name, expected in EXPECTED_COLUMN_TYPES.items():
        if name not in columns:
            problems.append(f"column {name} is missing")
        elif columns[name].data_type != expected:
            problems.append(f"column {name} is {columns[name].data_type!r}, expected {expected!r}")
    required = {
        name for name, row in columns.items()
        if row.is_nullable == "NO" and row.column_default is None
    }
    unsupplied = sorted(required - INSERTED_COLUMNS)
    if unsupplied:
        problems.append(f"required columns not supplied by the fixture INSERT: {unsupplied}")
    if problems:
        raise AssertionError(
            "Reporting integration-test precondition failed: "
            + "; ".join(problems) + f". {MISSING_SCHEMA_HINT}"
        )


def period_not_empty_message(total: int, marked: int, database: str) -> str:
    return (
        f"Reporting integration-test precondition failed: the isolated report period "
        f"[{MAIN_FROM.isoformat()}, {MAIN_TO.isoformat()}) in database {database!r} already "
        f"contains {total} row(s); {marked} of them have source LIKE '{MARKER_PREFIX}%' "
        f"(probably left over from an interrupted run). Nothing was seeded or deleted.\n"
        f"MANUAL CLEANUP ONLY - NOT EXECUTED BY THE TEST SUITE:\n"
        f"    DELETE FROM test_results WHERE source LIKE '{MARKER_PREFIX}%';\n"
        f"Rows without that marker must be investigated before deleting anything."
    )


# --- the main dataset ------------------------------------------------------------------------
#
# 13 rows, all inside [MAIN_FROM, MAIN_TO). Expected results (derived by hand):
#
#   summary          total 13, passed 9, failed 4, pass rate 69.2
#   by-device        ECU-901 4/2/2 50.0 | ECU-902 4/3/1 75.0 | ECU-903 5/4/1 80.0
#                    order: ECU-901 (2 failed), then the tie ECU-902, ECU-903 (1 failed each)
#                    ECU-903 has more results and a higher pass rate, so only
#                    "device_id ASC" explains ECU-902 before ECU-903.
#   by-test          Sleep Current 6/4/2 66.7 | CAN Cycle Time 3/2/1 66.7 |
#                    Wake-up Time 4/3/1 75.0; order: Sleep Current, then the tie
#                    CAN Cycle Time before Wake-up Time (fewer results: only name ASC).
#   hourly 06-20     10:00 4/2/2 | 11:00 3/1/2 | 23:00 1/1/0
#   daily            06-20 8/4/4 | 06-21 5/5/0
#   boundary         [10:00, 11:00) on 06-20: rows at 10:00 (included), 10:15/10:30/10:45
#                    (included), 11:00 (excluded) -> 4/2/2
#   23:30Z row       01:30 on 06-21 in Europe/Berlin, but in the UTC day 06-20.

TESTS = {
    # test_name: (unit, limit_min, limit_max, passing value, failing value)
    "Sleep Current": ("mA", 0.01, 0.40, 0.18, 0.55),
    "Wake-up Time": ("ms", None, 150.0, 97.0, 181.0),
    "CAN Cycle Time": ("ms", 9.5, 10.5, 10.0, 11.2),
}

MAIN_ROWS = [
    # (started_at UTC, device_id, test_name, verdict); ECU-903 rows come first on purpose,
    # so insertion order cannot explain the tie-break.
    ("2001-06-20T10:15:00", "ECU-903", "Wake-up Time", "PASS"),
    ("2001-06-20T11:00:00", "ECU-903", "Sleep Current", "FAIL"),
    ("2001-06-21T08:00:00", "ECU-903", "CAN Cycle Time", "PASS"),
    ("2001-06-21T09:30:00", "ECU-903", "Wake-up Time", "PASS"),
    ("2001-06-21T10:00:00", "ECU-903", "Sleep Current", "PASS"),
    ("2001-06-20T10:00:00", "ECU-901", "Sleep Current", "FAIL"),
    ("2001-06-20T10:45:00", "ECU-901", "CAN Cycle Time", "FAIL"),
    ("2001-06-20T11:40:00", "ECU-901", "Wake-up Time", "PASS"),
    ("2001-06-21T08:30:00", "ECU-901", "Sleep Current", "PASS"),
    ("2001-06-20T10:30:00", "ECU-902", "Sleep Current", "PASS"),
    ("2001-06-20T11:20:00", "ECU-902", "Wake-up Time", "FAIL"),
    ("2001-06-20T23:30:00", "ECU-902", "CAN Cycle Time", "PASS"),
    ("2001-06-21T09:00:00", "ECU-902", "Sleep Current", "PASS"),
]


def result_row(started_at: datetime, device_id: str, test_name: str, verdict: str,
               source: str) -> dict:
    """One complete, realistic test_results row (all NOT NULL columns supplied)."""
    unit, limit_min, limit_max, good, bad = TESTS[test_name]
    return {
        "device_id": device_id,
        "test_name": test_name,
        "temperature_c": -30.0,
        "measured_value": good if verdict == "PASS" else bad,
        "unit": unit,
        "limit_min": limit_min,
        "limit_max": limit_max,
        "verdict": verdict,
        "started_at": started_at,
        "duration_s": 12.5,
        "source": source,
    }


def main_dataset(marker: str) -> list[dict]:
    return [
        result_row(
            datetime.fromisoformat(ts).replace(tzinfo=timezone.utc), device, test, verdict, marker
        )
        for ts, device, test, verdict in MAIN_ROWS
    ]
