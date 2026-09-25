"""GET /api/v1/reports/summary: wiring of window, filters, counts and errors.

No database, no network: the FakeSession returns pre-made aggregate counts and records the
statement, and "now" is fixed via ``app.dependency_overrides``. These tests prove the HTTP
wiring only. The window and pass-rate rules themselves are tested in test_calculations.py,
the SQL shape in test_queries.py, and real PostgreSQL results in the integration tests.

Datetimes with offsets are always sent via ``params={...}``: the test client URL-encodes
"+" as "%2B". Written raw into a URL, "+" would be read as a space.
"""

import operator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main
import app.routers.reports
from app.dependencies import get_request_time
from app.main import create_app
from tests.unit.fakes import LEAK_MARKERS, UNAVAILABLE_ERRORS, UNEXPECTED_ERRORS, FakeSession

URL = "/api/v1/reports/summary"
NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def application(valid_env, session):
    """A fresh app from the isolated fake environment, fake sessions and a fixed "now"."""
    valid_env.setattr(app.main, "configure_logging", lambda settings: None)
    application = create_app()
    application.state.session_factory = lambda: session
    application.dependency_overrides[get_request_time] = lambda: NOW
    return application


@pytest.fixture
def client(application) -> TestClient:
    return TestClient(application)


def where(session) -> list[tuple[str, object, object]]:
    """WHERE conditions of the one executed statement as (column, operator, value)."""
    (statement,) = session.statements
    return [(c.left.name, c.operator, c.right.value) for c in statement.whereclause.clauses]


def window(session) -> tuple[datetime, datetime]:
    conditions = where(session)
    assert conditions[0][:2] == ("started_at", operator.ge)
    assert conditions[1][:2] == ("started_at", operator.lt)
    return conditions[0][2], conditions[1][2]


# --- response content --------------------------------------------------------------------

def test_summary_returns_counts_and_pass_rate(client, session):
    session.row = SimpleNamespace(total=120, passed=111, failed=9)

    params = {"from": "2026-09-16T00:00:00Z", "to": "2026-09-23T00:00:00Z"}

    response = client.get(URL, params=params)

    assert response.status_code == 200
    assert response.json() == {
        "from": "2026-09-16T00:00:00Z",
        "to": "2026-09-23T00:00:00Z",
        "total": 120,
        "passed": 111,
        "failed": 9,
        "pass_rate_percent": 92.5,
    }
    assert session.closed


def test_response_has_exactly_the_specified_keys(client):
    body = client.get(URL).json()

    assert list(body) == ["from", "to", "total", "passed", "failed", "pass_rate_percent"]
    assert "from_" not in body


def test_zero_counts_give_null_pass_rate(client):
    body = client.get(URL).json()

    assert (body["total"], body["passed"], body["failed"]) == (0, 0, 0)
    assert body["pass_rate_percent"] is None


def test_unknown_filter_value_is_a_normal_empty_report(client):
    response = client.get(URL, params={"device_id": "ECU-999", "test_name": "No Such Test"})

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["pass_rate_percent"] is None


def test_filter_values_are_not_validated_like_ingestion(client, session):
    # Not an ECU-### id: accepted as an exact-match filter, no 422.
    response = client.get(URL, params={"device_id": "bench-7"})

    assert response.status_code == 200
    assert ("device_id", operator.eq, "bench-7") in where(session)


# --- filters reach the query ---------------------------------------------------------------

def test_no_filters_add_no_predicates(client, session):
    client.get(URL)

    assert [column for column, _, _ in where(session)] == ["started_at", "started_at"]


def test_device_id_reaches_the_query(client, session):
    client.get(URL, params={"device_id": "ECU-004"})

    assert where(session)[2:] == [("device_id", operator.eq, "ECU-004")]


def test_test_name_reaches_the_query(client, session):
    client.get(URL, params={"test_name": "Sleep Current"})

    assert where(session)[2:] == [("test_name", operator.eq, "Sleep Current")]


def test_both_filters_reach_the_query_together(client, session):
    client.get(URL, params={"device_id": "ECU-004", "test_name": "Sleep Current"})

    assert where(session)[2:] == [
        ("device_id", operator.eq, "ECU-004"),
        ("test_name", operator.eq, "Sleep Current"),
    ]


def test_empty_device_id_is_passed_on_as_an_empty_string(client, session):
    # No special rule in the spec: "" is an ordinary exact-match value (matches nothing).
    response = client.get(URL, params={"device_id": ""})

    assert response.status_code == 200
    assert ("device_id", operator.eq, "") in where(session)


# --- window wiring -------------------------------------------------------------------------

def test_explicit_from_and_to_are_used(client, session):
    params = {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T06:30:00Z"}

    response = client.get(URL, params=params)

    assert response.status_code == 200
    assert window(session) == (
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc),
    )


def test_no_bounds_use_injected_now_and_168_hours(client, session):
    body = client.get(URL).json()

    assert window(session) == (NOW - timedelta(hours=168), NOW)
    assert body["from"] == "2026-09-18T10:00:00Z"
    assert body["to"] == "2026-09-25T10:00:00Z"


def test_only_from_ends_at_injected_now(client, session):
    body = client.get(URL, params={"from": "2026-09-20T00:00:00Z"}).json()

    assert window(session)[1] == NOW
    assert body["to"] == "2026-09-25T10:00:00Z"


def test_only_to_gives_exactly_168_hours(client, session):
    body = client.get(URL, params={"to": "2026-09-23T00:00:00Z"}).json()

    start, end = window(session)
    assert end - start == timedelta(hours=168)
    assert (body["from"], body["to"]) == ("2026-09-16T00:00:00Z", "2026-09-23T00:00:00Z")


def test_plus_two_hours_offset_via_params_is_accepted_and_normalised(client, session):
    response = client.get(URL, params={"from": "2026-09-20T12:00:00+02:00"})

    assert response.status_code == 200
    assert response.json()["from"] == "2026-09-20T10:00:00Z"
    assert window(session)[0] == datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    assert window(session)[0].tzinfo == timezone.utc


def test_negative_offset_is_normalised_to_utc(client):
    body = client.get(
        URL, params={"from": "2026-09-19T20:00:00-05:00", "to": "2026-09-20T20:00:00-05:00"}
    ).json()

    assert (body["from"], body["to"]) == ("2026-09-20T01:00:00Z", "2026-09-21T01:00:00Z")


def test_request_time_is_read_once_per_request(application, client):
    calls = []
    application.dependency_overrides[get_request_time] = lambda: calls.append(1) or NOW

    client.get(URL)

    assert calls == [1]


def test_default_request_time_is_current_utc():
    before = datetime.now(timezone.utc)
    now = get_request_time()
    after = datetime.now(timezone.utc)

    assert now.tzinfo == timezone.utc
    assert before <= now <= after


# --- 422 -----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "params, detail",
    [
        ({"from": "2026-09-20T00:00:00Z", "to": "2026-09-20T00:00:00Z"},
         "from must be earlier than to"),
        ({"from": "2026-09-21T00:00:00Z", "to": "2026-09-20T00:00:00Z"},
         "from must be earlier than to"),
        ({"from": "2026-09-20T02:00:00+02:00", "to": "2026-09-20T00:00:00Z"},
         "from must be earlier than to"),
        ({"from": "2026-09-26T00:00:00Z"}, "from must be earlier than to"),
    ],
    ids=["from-equals-to", "from-after-to", "same-instant-other-offset", "future-from-only"],
)
def test_invalid_window_order_returns_422(client, session, params, detail):
    response = client.get(URL, params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert session.statements == []


@pytest.mark.parametrize("name", ["from", "to"])
def test_naive_bound_returns_422(client, session, name):
    response = client.get(URL, params={name: "2026-09-20T00:00:00"})

    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["type"] == "timezone_aware"
    assert error["loc"] == ["query", name]
    assert session.statements == []


@pytest.mark.parametrize("value", ["yesterday", "2026-13-01T00:00:00Z", ""])
def test_malformed_datetime_returns_422(client, session, value):
    response = client.get(URL, params={"from": value})

    assert response.status_code == 422
    assert session.statements == []


def test_other_value_errors_are_not_turned_into_422(application, monkeypatch):
    # Only the window resolution maps ValueError to 422; a bug elsewhere stays a 500.
    def broken(passed, total):
        raise ValueError("internal bug")

    monkeypatch.setattr(app.routers.reports, "calculate_pass_rate_percent", broken)

    response = TestClient(application, raise_server_exceptions=False).get(URL)

    assert response.status_code == 500
    assert "internal bug" not in response.text


# --- database errors reach the Stage 2 handlers ----------------------------------------------

@pytest.mark.parametrize("make_error", UNAVAILABLE_ERRORS.values(), ids=UNAVAILABLE_ERRORS.keys())
def test_database_unavailable_returns_503(client, session, make_error):
    session.error = make_error()

    response = client.get(URL)

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    for marker in LEAK_MARKERS:
        assert marker not in response.text
    assert session.closed


@pytest.mark.parametrize("make_error", UNEXPECTED_ERRORS.values(), ids=UNEXPECTED_ERRORS.keys())
def test_other_database_errors_return_500(client, session, make_error):
    session.error = make_error()

    response = client.get(URL)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    for marker in LEAK_MARKERS:
        assert marker not in response.text
    assert session.closed


# --- read-only, no authentication ----------------------------------------------------------

def test_summary_needs_no_api_key_and_other_methods_are_not_allowed(client, session):
    assert client.get(URL).status_code == 200
    for method in ("post", "put", "delete"):
        assert getattr(client, method)(URL).status_code == 405
