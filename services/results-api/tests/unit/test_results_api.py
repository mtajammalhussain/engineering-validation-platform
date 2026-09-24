"""Endpoint tests with FastAPI's TestClient and a fake database session.

No database and no network. The fake session only records what the endpoints ask for,
so these tests prove request validation, authentication, status codes and which query is
built. They do NOT prove PostgreSQL behaviour (real INSERTs, server defaults, filter
results, ordering, pagination over rows, constraints): that is covered by the
integration tests against real PostgreSQL.
"""

import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

import app.main
from app.config import Settings
from app.main import create_app
from app.models import Result

API_KEY = "test-api-key"
URL = "/api/v1/results"
SERVICE_DIR = Path(__file__).resolve().parents[2]


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
ALL_ERRORS = [
    pytest.param(make, 503, "Database unavailable", id=name)
    for name, make in UNAVAILABLE_ERRORS.items()
] + [
    pytest.param(make, 500, "Internal server error", id=name)
    for name, make in UNEXPECTED_ERRORS.items()
]


def assert_generic_error(response, status_code: int, detail: str) -> None:
    """Right status, generic body, and no SQL, host or credential in the response."""
    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    for secret in ("db.example.invalid", "INSERT", "db-password-must-not-leak", "detail:"):
        assert secret not in response.text


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


def sql(statement) -> str:
    """The SQL text PostgreSQL would receive for this statement, with values filled in."""
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


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


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(session, monkeypatch) -> TestClient:
    """TestClient for an app whose sessions come from the FakeSession.

    Used without ``with``, so the lifespan (real engine) does not run. The real get_db
    dependency still runs, so closing the session is tested too.
    """
    monkeypatch.setattr(app.main, "configure_logging", lambda settings: None)
    application = create_app(make_settings())
    application.state.session_factory = lambda: session
    return TestClient(application)


# --- POST /api/v1/results ---------------------------------------------------------

def test_post_valid_returns_201_with_stored_values(client, session):
    response = client.post(URL, json=valid_body(), headers={"X-API-Key": API_KEY})

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["received_at"] == "2026-09-24T12:00:00Z"
    assert body["device_id"] == "ECU-003"
    assert session.commits == 1
    assert session.closed


def test_post_omitted_source_is_not_written(client, session):
    client.post(URL, json=valid_body(), headers={"X-API-Key": API_KEY})

    # SQLAlchemy only INSERTs attributes that were set; "source" was never set.
    written = session.written[0]
    assert "source" not in written
    assert written["device_id"] == "ECU-003"


def test_post_given_source_is_written(client, session):
    client.post(URL, json=valid_body(source="HIL-7"), headers={"X-API-Key": API_KEY})

    assert session.written[0]["source"] == "HIL-7"


def test_post_omitted_limits_are_not_written(client, session):
    body = valid_body()
    del body["limit_min"], body["limit_max"]

    response = client.post(URL, json=body, headers={"X-API-Key": API_KEY})

    assert response.status_code == 201
    assert not {"limit_min", "limit_max"} & set(session.written[0])


@pytest.mark.parametrize(
    "overrides",
    [{"verdict": "OK"}, {"device_id": "ECU-1"}, {"started_at": "2026-09-23T19:30:00"},
     {"unknown": 1}, {"source": None}],
)
def test_post_invalid_body_returns_422(client, session, overrides):
    response = client.post(URL, json=valid_body(**overrides), headers={"X-API-Key": API_KEY})

    assert response.status_code == 422
    assert session.added == []


def test_post_non_json_body_returns_422(client, session):
    response = client.post(
        URL, content="not json", headers={"X-API-Key": API_KEY, "Content-Type": "application/json"}
    )

    assert response.status_code == 422


@pytest.mark.parametrize("make_error, status_code, detail", ALL_ERRORS)
def test_post_database_failure_is_mapped_and_rolled_back(
    client, session, make_error, status_code, detail
):
    session.error = make_error()

    response = client.post(URL, json=valid_body(), headers={"X-API-Key": API_KEY})

    assert_generic_error(response, status_code, detail)
    assert session.rollbacks == 1
    assert session.commits == 0
    assert session.closed


@pytest.mark.parametrize("make_error, status_code, detail", ALL_ERRORS)
def test_database_failure_is_logged_server_side(
    client, session, caplog, make_error, status_code, detail
):
    session.error = make_error()

    client.post(URL, json=valid_body(), headers={"X-API-Key": API_KEY})

    assert type(session.error).__name__ in caplog.text
    assert "db.example.invalid" in caplog.text  # full details stay in the server log


# --- API key -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong-key"}, {"X-API-Key": API_KEY + "x"},
     {"X-API-Key": API_KEY.upper()}],
    ids=["missing", "empty", "wrong", "longer", "wrong-case"],
)
def test_post_without_correct_api_key_returns_401(client, session, headers):
    response = client.post(URL, json=valid_body(), headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}
    assert session.added == []


def test_non_ascii_api_key_returns_401_not_500(client):
    headers = {"X-API-Key": "schlüssel".encode("latin-1")}

    response = client.post(URL, json=valid_body(), headers=headers)

    assert response.status_code == 401


def test_auth_is_checked_before_body_validation(client):
    # Unauthenticated clients must not learn anything about the expected body.
    response = client.post(URL, json={"verdict": "OK"})

    assert response.status_code == 401


def test_api_key_is_never_logged_or_returned(client, caplog):
    caplog.set_level(logging.DEBUG)

    ok = client.post(URL, json=valid_body(), headers={"X-API-Key": API_KEY})
    bad = client.post(URL, json=valid_body(), headers={"X-API-Key": "attacker-guess"})

    assert API_KEY not in caplog.text and "attacker-guess" not in caplog.text
    assert API_KEY not in ok.text and API_KEY not in bad.text
    assert "Rejected request: invalid API key" in caplog.text


def test_get_endpoints_need_no_api_key(client):
    assert client.get(URL).status_code == 200


# --- GET /api/v1/results -----------------------------------------------------------

def test_list_uses_default_pagination_and_order(client, session):
    session.total = 1
    session.rows = [stored_row()]

    response = client.get(URL)

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 50 and body["offset"] == 0 and body["total"] == 1
    assert [item["id"] for item in body["items"]] == [7]

    count_sql, rows_sql = (sql(s) for s in session.statements)
    assert "count(*)" in count_sql and "WHERE" not in count_sql
    assert "WHERE" not in rows_sql
    assert "ORDER BY test_results.started_at DESC, test_results.id DESC" in rows_sql
    assert "LIMIT 50 OFFSET 0" in rows_sql
    assert session.closed


def test_list_limit_500_is_accepted(client, session):
    response = client.get(URL, params={"limit": 500, "offset": 1000})

    assert response.status_code == 200
    assert "LIMIT 500 OFFSET 1000" in sql(session.statements[1])


@pytest.mark.parametrize(
    "params",
    [{"limit": 501}, {"limit": 0}, {"limit": -1}, {"limit": "abc"},
     {"offset": -1}, {"offset": "abc"}, {"offset": 2**63}],
)
def test_list_invalid_pagination_returns_422(client, session, params):
    response = client.get(URL, params=params)

    assert response.status_code == 422
    assert session.statements == []


def test_list_passes_all_filters_into_the_query(client, session):
    params = {
        "device_id": "ECU-004",
        "test_name": "Sleep Current",
        "verdict": "FAIL",
        "from": "2026-09-16T00:00:00Z",
        "to": "2026-09-23T02:00:00+02:00",
    }

    response = client.get(URL, params=params)

    assert response.status_code == 200
    for statement in session.statements:  # the count and the rows query use the same filters
        text = sql(statement)
        assert "test_results.device_id = 'ECU-004'" in text
        assert "test_results.test_name = 'Sleep Current'" in text
        assert "test_results.verdict = 'FAIL'" in text
        assert "test_results.started_at >= '2026-09-16 00:00:00+00:00'" in text
        assert "test_results.started_at < '2026-09-23 02:00:00+02:00'" in text


def test_list_only_given_filters_are_applied(client, session):
    client.get(URL, params={"verdict": "PASS"})

    text = sql(session.statements[1])
    assert "test_results.verdict = 'PASS'" in text
    assert "device_id =" not in text and "started_at >=" not in text


@pytest.mark.parametrize(
    "params",
    [{"verdict": "OK"}, {"verdict": "pass"}, {"from": "2026-09-16T00:00:00"},
     {"to": "2026-09-23T00:00:00"}, {"from": "yesterday"}],
)
def test_list_invalid_filters_return_422(client, session, params):
    response = client.get(URL, params=params)

    assert response.status_code == 422
    assert session.statements == []


@pytest.mark.parametrize("make_error, status_code, detail", ALL_ERRORS)
def test_list_database_failure_is_mapped(client, session, make_error, status_code, detail):
    session.error = make_error()

    response = client.get(URL)

    assert_generic_error(response, status_code, detail)
    assert session.closed


# --- GET /api/v1/results/{id} ------------------------------------------------------

def test_get_existing_result(client, session):
    session.by_id[7] = stored_row(7)

    response = client.get(f"{URL}/7")

    assert response.status_code == 200
    assert response.json()["id"] == 7
    assert session.get_calls == [7]


def test_get_unknown_result_returns_404(client, session):
    response = client.get(f"{URL}/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Result not found"}


@pytest.mark.parametrize("result_id", ["abc", "0", "-1", str(2**63)])
def test_get_invalid_id_returns_422(client, session, result_id):
    response = client.get(f"{URL}/{result_id}")

    assert response.status_code == 422
    assert session.get_calls == []


@pytest.mark.parametrize("make_error, status_code, detail", ALL_ERRORS)
def test_get_database_failure_is_mapped(client, session, make_error, status_code, detail):
    session.error = make_error()

    response = client.get(f"{URL}/7")

    assert_generic_error(response, status_code, detail)
    assert session.closed


# --- application wiring ------------------------------------------------------------

def test_swagger_ui_and_routes_are_exposed(client):
    assert client.get("/docs").status_code == 200

    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {URL, URL + "/{result_id}"}
    assert spec["paths"][URL]["post"]["security"] == [{"APIKeyHeader": []}]


def test_lifespan_creates_session_factory_and_disposes_engine(monkeypatch):
    engine = SimpleNamespace(disposed=False)
    engine.dispose = lambda: setattr(engine, "disposed", True)
    monkeypatch.setattr(app.main, "configure_logging", lambda settings: None)
    monkeypatch.setattr(app.main, "create_db_engine", lambda settings: engine)
    application = create_app(make_settings())

    with TestClient(application):
        assert application.state.session_factory.kw["bind"] is engine
        assert not engine.disposed

    assert engine.disposed


def test_create_app_without_settings_loads_them_from_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(app.main, "load_settings", lambda: calls.append(1) or make_settings())
    monkeypatch.setattr(app.main, "configure_logging", lambda settings: calls.append(2))

    create_app()

    assert calls == [1, 2]


def test_importing_main_has_no_side_effects():
    # A fresh Python process with an empty environment: importing must not read settings.
    completed = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=SERVICE_DIR, env={}, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == ""
