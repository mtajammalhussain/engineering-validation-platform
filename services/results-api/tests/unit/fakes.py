"""Unit-test helpers: settings, stored rows and a fake database session.

Request bodies shared with the integration tests live in tests/common.py.

No database and no network. The fake session only records what the code asks for; it
proves nothing about real PostgreSQL behaviour (that is what the integration tests do).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import inspect
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, make_transient_to_detached

from app.config import Settings
from app.models import Result

API_KEY = "test-api-key"

SQL = "INSERT INTO test_results ..."
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


def make_settings() -> Settings:
    return Settings(
        app_env="dev",
        log_level="INFO",
        log_format="text",
        port=8001,
        db_host="db.example.invalid",
        db_port=5432,
        db_name="evp",
        db_user="evp_writer",
        db_password="db-password-must-not-leak",
        results_api_key=API_KEY,
    )


def make_unreadable_like_commit(row: Result) -> None:
    """Make every column of ``row`` unreadable, as a real commit would for later reads.

    A real commit expires the row (``expire_on_commit``): reading it afterwards runs a new
    SELECT, i.e. database work after the commit. Here the row is expired and detached instead,
    so such a read raises ``DetachedInstanceError`` and the test fails loudly.

    Public SQLAlchemy API only. The temporary Session has no database (no bind), so it can
    never run SQL. If this helper itself breaks (e.g. after a SQLAlchemy upgrade), it raises
    RuntimeError, which no application error handler catches, so the failure is reported as
    a test-infrastructure problem and not as a 500 from the application.
    """
    try:
        make_transient_to_detached(row)  # treat it as an existing row (it has an id)
        with Session() as session:
            session.add(row)
            session.expire(row)  # forget all loaded column values
            session.expunge(row)  # no session left that could reload them
    except Exception as exc:
        raise RuntimeError(
            "FakeSession test helper failed (test infrastructure, not application code)"
        ) from exc


class FakeSession:
    """Stands in for a SQLAlchemy Session and records every call.

    ``calls`` lists the write-path calls in order (add, flush, refresh, commit, rollback).
    If ``error`` is set, the write step named by ``write_error_at`` raises it; the read
    methods (execute, scalar, get) raise it directly.
    """

    def __init__(self) -> None:
        self.added: list[Result] = []
        self.written: list[dict] = []
        self.refreshed: list[Result] = []
        self.calls: list[str] = []
        self.statements: list = []
        self.get_calls: list[int] = []
        self.commits = self.rollbacks = 0
        self.closed = False
        self.error: SQLAlchemyError | None = None
        self.write_error_at = "flush"
        self.total = 0
        self.rows: list[Result] = []
        self.by_id: dict[int, Result] = {}

    def _write_step(self, name: str) -> None:
        self.calls.append(name)
        if self.error is not None and self.write_error_at == name:
            raise self.error

    def add(self, obj: Result) -> None:
        self.calls.append("add")
        self.added.append(obj)

    def flush(self) -> None:
        self._write_step("flush")
        # Attributes set on each object at flush time = the columns SQLAlchemy would INSERT.
        self.written = [
            {k: v for k, v in inspect(obj).dict.items() if not k.startswith("_")}
            for obj in self.added
        ]

    def refresh(self, obj: Result) -> None:
        self._write_step("refresh")
        # Pretend the database generated these values; the real ones are integration-tested.
        obj.id = 1
        obj.received_at = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        if "source" not in inspect(obj).dict:
            obj.source = "value-from-fake-refresh"
        self.refreshed.append(obj)

    def commit(self) -> None:
        self._write_step("commit")
        self.commits += 1
        for obj in self.refreshed:
            make_unreadable_like_commit(obj)
        self.refreshed = []

    def rollback(self) -> None:
        self.calls.append("rollback")
        self.rollbacks += 1
        self.refreshed = []

    def close(self) -> None:
        self.closed = True

    def execute(self, statement):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error

    def scalar(self, statement):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return self.total

    def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: self.rows)

    def get(self, model, ident):
        self.get_calls.append(ident)
        if self.error is not None:
            raise self.error
        return self.by_id.get(ident)


def stored_row(result_id: int = 7) -> Result:
    return Result(
        id=result_id,
        device_id="ECU-001",
        test_name="Sleep Current",
        temperature_c=-30.0,
        measured_value=0.18,
        unit="mA",
        limit_min=0.01,
        limit_max=0.40,
        verdict="PASS",
        started_at=datetime(2026, 9, 23, 19, 30, tzinfo=timezone.utc),
        duration_s=42.0,
        received_at=datetime(2026, 9, 23, 19, 31, tzinfo=timezone.utc),
        source="simulator",
    )
