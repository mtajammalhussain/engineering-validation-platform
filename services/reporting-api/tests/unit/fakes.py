"""Shared test helpers: a fake read-only database session and database errors.

No database and no network. The fake session only records what the code asks for; it
proves nothing about real PostgreSQL behaviour (that is what the integration tests do).
It deliberately has no add/commit/refresh methods: the Reporting API never writes, so any
attempt to write would fail a test with AttributeError.
"""

from types import SimpleNamespace

from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

SQL = "SELECT count(*) FROM test_results ..."
DETAIL = Exception("server at db.example.invalid: low-level detail")

# Database cannot be reached/used right now -> 503
UNAVAILABLE_ERRORS = {
    "operational": lambda: OperationalError(SQL, {}, DETAIL),
    "interface": lambda: InterfaceError(SQL, {}, DETAIL),
    "pool-timeout": lambda: PoolTimeoutError("QueuePool limit reached for db.example.invalid"),
}
# Bug or unexpected state -> 500
UNEXPECTED_ERRORS = {
    "integrity": lambda: IntegrityError(SQL, {}, DETAIL),
    "data": lambda: DataError(SQL, {}, DETAIL),
    "programming": lambda: ProgrammingError(SQL, {}, DETAIL),
    "generic": lambda: SQLAlchemyError("unexpected at db.example.invalid"),
}
ALL_ERRORS = {**UNAVAILABLE_ERRORS, **UNEXPECTED_ERRORS}

# Strings that must never reach an HTTP response body.
LEAK_MARKERS = ("db.example.invalid", "SELECT", "test_results", "low-level detail",
                "super-secret-password", "QueuePool")


class FakeResult:
    """Result of one aggregate query: ``.one()`` for summary, ``.all()`` for groups/buckets.

    Both return rows prepared by the test, i.e. already-aggregated counts. There is no
    ``.scalars()`` or iteration over individual test results.
    """

    def __init__(self, row, rows) -> None:
        self._row = row
        self._rows = rows

    def one(self):
        return self._row

    def all(self):
        return list(self._rows)


class FakeSession:
    """Stands in for a SQLAlchemy Session (read-only use) and records every call.

    ``row`` is what the summary query returns (total/passed/failed counts); ``rows`` is
    what a grouped or time-bucket query returns (one aggregate row per group/bucket).
    """

    def __init__(self) -> None:
        self.statements: list = []
        self.closed = False
        self.error: SQLAlchemyError | None = None
        self.row = SimpleNamespace(total=0, passed=0, failed=0)
        self.rows: list = []

    def execute(self, statement):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return FakeResult(self.row, self.rows)

    def close(self) -> None:
        self.closed = True
