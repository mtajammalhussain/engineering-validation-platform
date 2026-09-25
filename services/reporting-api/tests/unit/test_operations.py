"""App factory, lifespan, /health, /ready, /metrics, /docs and DB error mapping.

No database, no network, no real .env: the environment is isolated by the autouse fixture
in conftest.py, and sessions come from a FakeSession. These tests prove application
behaviour only, not that real PostgreSQL answers, that transactions are read-only or that
the database user is SELECT-only (that is what the integration tests are for).

Stage 2 has no report endpoint yet, so some tests add test-only routes to the test's own
app instance. Production code contains no such route.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from uvicorn.importer import import_from_string

import app.main
from app.config import Settings
from app.dependencies import get_db
from app.main import create_app
from tests.unit.fakes import (
    ALL_ERRORS,
    LEAK_MARKERS,
    UNAVAILABLE_ERRORS,
    UNEXPECTED_ERRORS,
    FakeSession,
)

SERVICE_DIR = Path(__file__).resolve().parents[2]
OK_ROUTE = "/test-only/ok"
ITEM_ROUTE = "/test-only/items/{item_id}"
DB_ROUTE = "/test-only/db"
ALLOWED_LABEL_NAMES = {"handler", "method", "status", "le"}


def add_test_only_routes(application: FastAPI) -> None:
    """Ordinary routes for this test app only (hidden from the OpenAPI schema)."""

    def ok() -> dict[str, str]:
        return {"status": "ok"}

    def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    def uses_db(db: Session = Depends(get_db)) -> dict[str, str]:
        db.execute(text("SELECT count(*) FROM test_results"))
        return {"status": "ok"}

    application.add_api_route(OK_ROUTE, ok, include_in_schema=False)
    application.add_api_route(ITEM_ROUTE, item, include_in_schema=False)
    application.add_api_route(DB_ROUTE, uses_db, include_in_schema=False)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(valid_env, session) -> TestClient:
    """TestClient for a fresh app built from the isolated fake environment.

    ``create_app()`` is called without arguments, so settings really come from the env.
    Used without ``with``, so the lifespan (real engine) does not run; sessions come from
    the FakeSession. The real get_db dependency still runs, so closing is tested too.
    """
    valid_env.setattr(app.main, "configure_logging", lambda settings: None)
    application = create_app()
    application.state.session_factory = lambda: session
    add_test_only_routes(application)
    return TestClient(application)


@pytest.fixture
def restore_loggers():
    """Put the root and uvicorn loggers back after a test that runs the real logging setup."""
    names = (None, "uvicorn", "uvicorn.error", "uvicorn.access")
    loggers = [logging.getLogger(name) for name in names]
    saved = [(lg, lg.handlers[:], lg.level, lg.propagate) for lg in loggers]
    yield
    for lg, handlers, level, propagate in saved:
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


def fake_engine() -> SimpleNamespace:
    engine = SimpleNamespace(disposed=False)
    engine.dispose = lambda: setattr(engine, "disposed", True)
    return engine


def samples(client):
    registry = client.app.state.metrics_registry
    return [sample for metric in registry.collect() for sample in metric.samples]


def value(client, name: str, **labels) -> float:
    return client.app.state.metrics_registry.get_sample_value(name, labels) or 0.0


def http_handlers(client) -> set[str]:
    return {s.labels["handler"] for s in samples(client) if s.name == "http_requests_total"}


def assert_no_leak(response) -> None:
    for marker in LEAK_MARKERS:
        assert marker not in response.text, marker


# --- import and factory -------------------------------------------------------------------

def test_importing_main_has_no_side_effects():
    # A fresh Python process with an empty environment: importing must not read settings.
    completed = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=SERVICE_DIR, env={}, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_main_module_holds_no_app_engine_or_settings():
    for value_ in vars(app.main).values():
        assert not isinstance(value_, (FastAPI, Engine, Settings))


def test_factory_is_importable_the_way_uvicorn_factory_loads_it():
    factory = import_from_string("app.main:create_app")

    assert factory is create_app


def test_create_app_loads_settings_from_environment(valid_env):
    valid_env.setattr(app.main, "configure_logging", lambda settings: None)

    application = create_app()

    assert isinstance(application, FastAPI)
    assert application.state.settings.port == 8002
    assert application.state.settings.db_user == "evp_reader"


def test_create_app_loads_settings_before_configuring_logging(monkeypatch, make_settings):
    calls = []
    monkeypatch.setattr(app.main, "load_settings", lambda: calls.append("load") or make_settings())
    monkeypatch.setattr(app.main, "configure_logging", lambda settings: calls.append("log"))

    create_app()

    assert calls == ["load", "log"]


def test_create_app_without_configuration_fails_fast(capsys):
    # The autouse isolated_env fixture has removed every settings variable.
    with pytest.raises(SystemExit) as exc_info:
        create_app()

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    for name in ("PORT", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        assert name in output


def test_create_app_does_not_create_an_engine(valid_env):
    valid_env.setattr(app.main, "configure_logging", lambda settings: None)
    valid_env.setattr(
        app.main, "create_db_engine", lambda settings: pytest.fail("engine created too early")
    )

    application = create_app()

    assert not hasattr(application.state, "engine")
    assert not hasattr(application.state, "session_factory")


# --- lifespan -----------------------------------------------------------------------------

def test_lifespan_creates_engine_and_session_factory_and_disposes_engine(valid_env):
    engine = fake_engine()
    valid_env.setattr(app.main, "configure_logging", lambda settings: None)
    valid_env.setattr(app.main, "create_db_engine", lambda settings: engine)
    application = create_app()

    with TestClient(application):
        assert application.state.engine is engine
        assert application.state.session_factory.kw["bind"] is engine
        assert not engine.disposed

    assert engine.disposed


def test_lifespan_with_real_engine_starts_and_stops_without_connecting(valid_env):
    # DB_HOST=db.example.invalid does not exist: this only works because nothing connects.
    valid_env.setattr(app.main, "configure_logging", lambda settings: None)
    application = create_app()

    with TestClient(application) as test_client:
        assert isinstance(application.state.engine, Engine)
        assert test_client.get("/health").status_code == 200


def test_startup_log_identifies_reporting_api(valid_env, restore_loggers, capsys):
    valid_env.setenv("LOG_FORMAT", "json")
    valid_env.setattr(app.main, "create_db_engine", lambda settings: fake_engine())
    application = create_app()

    with TestClient(application):
        pass

    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    started = [e for e in entries if e["msg"].startswith("Reporting API started")]
    assert len(started) == 1
    assert started[0]["service"] == "reporting-api"
    assert any(e["msg"] == "Reporting API stopped" for e in entries)


# --- /health ------------------------------------------------------------------------------

def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_touch_the_database(client):
    client.app.state.session_factory = lambda: pytest.fail("/health opened a DB session")

    assert client.get("/health").status_code == 200


# --- /ready -------------------------------------------------------------------------------

def test_ready_returns_ok_when_select_1_succeeds(client, session):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert [str(statement) for statement in session.statements] == ["SELECT 1"]
    assert session.closed


@pytest.mark.parametrize("make_error", ALL_ERRORS.values(), ids=ALL_ERRORS.keys())
def test_ready_returns_503_for_any_database_error(client, session, caplog, make_error):
    session.error = make_error()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert_no_leak(response)
    # One short warning with the error class only; details stay out of the log line.
    assert f"Readiness check failed: {type(session.error).__name__}" in caplog.text
    assert "db.example.invalid" not in caplog.text
    assert session.closed


# --- database error mapping (future report endpoints) --------------------------------------

@pytest.mark.parametrize("make_error", UNAVAILABLE_ERRORS.values(), ids=UNAVAILABLE_ERRORS.keys())
def test_database_unavailable_maps_to_generic_503(client, session, make_error):
    session.error = make_error()

    response = client.get(DB_ROUTE)

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert_no_leak(response)
    assert session.closed


@pytest.mark.parametrize("make_error", UNEXPECTED_ERRORS.values(), ids=UNEXPECTED_ERRORS.keys())
def test_other_database_errors_map_to_generic_500(client, session, make_error):
    session.error = make_error()

    response = client.get(DB_ROUTE)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert_no_leak(response)
    assert session.closed


def test_database_error_details_are_logged_server_side(client, session, caplog):
    session.error = UNAVAILABLE_ERRORS["operational"]()

    client.get(DB_ROUTE)

    assert "Database unavailable on GET /test-only/db: OperationalError" in caplog.text


def test_session_is_closed_after_successful_request(client, session):
    assert client.get(DB_ROUTE).status_code == 200
    assert session.closed


# --- /metrics -----------------------------------------------------------------------------

def test_metrics_endpoint_uses_prometheus_format(client):
    client.get(OK_ROUTE)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "# TYPE http_requests_total counter" in response.text


def test_ordinary_route_produces_standard_http_metrics(client):
    client.get(OK_ROUTE)

    assert value(client, "http_requests_total", handler=OK_ROUTE, method="GET", status="200") == 1
    assert value(
        client, "http_request_duration_seconds_count", handler=OK_ROUTE, method="GET"
    ) == 1


def test_status_codes_are_kept_apart(client, session):
    session.error = UNAVAILABLE_ERRORS["operational"]()

    client.get(DB_ROUTE)

    assert value(client, "http_requests_total", handler=DB_ROUTE, method="GET", status="503") == 1


def test_probe_and_scrape_endpoints_are_excluded_from_http_metrics(client):
    for _ in range(3):
        client.get("/health")
        client.get("/ready")
        client.get("/metrics")
    client.get(OK_ROUTE)

    assert http_handlers(client) == {OK_ROUTE}
    # The latency histogram without a handler label only counts the one real request.
    assert value(client, "http_request_duration_highr_seconds_count") == 1


def test_labels_are_bounded_route_templates(client):
    client.get("/test-only/items/12345", params={"device_id": "ECU-042"})
    client.get(OK_ROUTE, params={"test_name": "Sleep Current"})
    client.get("/some/random/path/67890")

    all_samples = samples(client)
    label_names = {name for s in all_samples for name in s.labels}
    label_values = {v for s in all_samples for v in s.labels.values()}

    assert label_names <= ALLOWED_LABEL_NAMES
    for forbidden in ("12345", "67890", "/some/random/path", "ECU-042", "Sleep Current", "?"):
        assert not any(forbidden in label for label in label_values), forbidden
    assert ITEM_ROUTE in http_handlers(client)


def test_each_app_has_its_own_registry(valid_env):
    valid_env.setattr(app.main, "configure_logging", lambda settings: None)

    first, second = create_app(), create_app()

    assert first.state.metrics_registry is not second.state.metrics_registry
    assert first.state.metrics_registry is not REGISTRY


def test_no_reporting_specific_custom_metrics(client):
    client.get(OK_ROUTE)

    names = {s.name for s in samples(client)}
    assert names
    assert all(name.startswith("http_") for name in names), names


# --- /docs and authentication --------------------------------------------------------------

def test_swagger_ui_is_enabled(client):
    response = client.get("/docs")

    assert response.status_code == 200
    assert "swagger" in response.text.lower()


def test_openapi_lists_only_operational_routes_and_no_security(client):
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]) == {"/health", "/ready", "/metrics"}
    assert "securitySchemes" not in spec.get("components", {})
    assert all("security" not in op for path in spec["paths"].values() for op in path.values())


def test_no_api_key_is_needed(client):
    for path in ("/health", "/ready", "/metrics", "/docs"):
        assert client.get(path).status_code == 200, path
