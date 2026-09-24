"""Shared test helpers: settings, request bodies and a fake database session.

No database and no network. The fake session only records what the code asks for; it
proves nothing about real PostgreSQL behaviour (that is what the integration tests do).
"""

from datetime import datetime, timedelta, timezone
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

from app.config import Settings
from app.models import Result

API_KEY = "test-api-key"
URL = "/api/v1/results"

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


class FakeSession:
    """Stands in for a SQLAlchemy Session and records every call."""

    def __init__(self) -> None:
        self.added: list[Result] = []
        self.written: list[dict] = []
        self.statements: list = []
        self.get_calls: list[int] = []
        self.commits = self.rollbacks = 0
        self.closed = False
        self.error: SQLAlchemyError | None = None
        self.total = 0
        self.rows: list[Result] = []
        self.by_id: dict[int, Result] = {}

    def add(self, obj: Result) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        if self.error is not None:
            raise self.error
        self.commits += 1
        # Attributes set on each object at commit time = the columns SQLAlchemy would INSERT.
        self.written = [
            {k: v for k, v in inspect(obj).dict.items() if not k.startswith("_")}
            for obj in self.added
        ]

    def refresh(self, obj: Result) -> None:
        # Pretend the database generated these values; the real ones are integration-tested.
        obj.id = 1
        obj.received_at = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        if "source" not in inspect(obj).dict:
            obj.source = "value-from-fake-refresh"

    def rollback(self) -> None:
        self.rollbacks += 1

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


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def valid_body(**overrides) -> dict:
    body = {
        "device_id": "ECU-003",
        "test_name": "Sleep Current",
        "temperature_c": -30,
        "measured_value": 0.22,
        "unit": "mA",
        "limit_min": 0.01,
        "limit_max": 0.40,
        "verdict": "PASS",
        "started_at": iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
        "duration_s": 42.0,
    }
    body.update(overrides)
    return body


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
