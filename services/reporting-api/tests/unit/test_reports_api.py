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
from sqlalchemy.dialects import postgresql

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


# === by-device / by-test ====================================================================

BY_DEVICE = "/api/v1/reports/by-device"
BY_TEST = "/api/v1/reports/by-test"
GROUPED = [(BY_DEVICE, "device_id"), (BY_TEST, "test_name")]


def group_row(key: str, value: str, total: int, passed: int, failed: int):
    return SimpleNamespace(**{key: value}, total=total, passed=passed, failed=failed)


@pytest.mark.parametrize("url, key", GROUPED)
def test_grouped_report_maps_rows_in_database_order(client, session, url, key):
    # Rows as PostgreSQL returns them: failed DESC, then key ASC. The API keeps that order.
    session.rows = [
        group_row(key, "B", 120, 111, 9),
        group_row(key, "A", 5, 3, 2),
        group_row(key, "C", 5, 3, 2),
        group_row(key, "D", 4, 4, 0),
    ]
    params = {"from": "2026-09-16T00:00:00Z", "to": "2026-09-23T00:00:00Z"}

    response = client.get(url, params=params)

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["from", "to", "items"]
    assert (body["from"], body["to"]) == ("2026-09-16T00:00:00Z", "2026-09-23T00:00:00Z")
    assert body["items"][0] == {
        key: "B", "total": 120, "passed": 111, "failed": 9, "pass_rate_percent": 92.5,
    }
    assert [item[key] for item in body["items"]] == ["B", "A", "C", "D"]
    assert [item["pass_rate_percent"] for item in body["items"]] == [92.5, 60.0, 60.0, 100.0]
    assert session.closed


@pytest.mark.parametrize("url, key", GROUPED)
def test_grouped_report_asks_the_database_for_failed_desc_then_key_asc(client, session, url, key):
    client.get(url)

    (statement,) = session.statements
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert f"GROUP BY test_results.{key} ORDER BY failed DESC, test_results.{key} ASC" in sql


@pytest.mark.parametrize("url, key", GROUPED)
def test_grouped_report_without_rows_returns_empty_items(client, session, url, key):
    response = client.get(url, params={key: "unknown"})

    assert response.status_code == 200
    assert response.json() == {
        "from": "2026-09-18T10:00:00Z", "to": "2026-09-25T10:00:00Z", "items": [],
    }


@pytest.mark.parametrize("url, key", GROUPED)
def test_grouped_report_passes_both_filters_to_the_query(client, session, url, key):
    client.get(url, params={"device_id": "ECU-004", "test_name": "Sleep Current"})

    assert where(session) == [
        ("started_at", operator.ge, NOW - timedelta(hours=168)),
        ("started_at", operator.lt, NOW),
        ("device_id", operator.eq, "ECU-004"),
        ("test_name", operator.eq, "Sleep Current"),
    ]


@pytest.mark.parametrize("url, key", GROUPED)
def test_grouped_report_normalises_offsets_to_utc(client, session, url, key):
    body = client.get(url, params={"from": "2026-09-20T12:00:00+02:00"}).json()

    assert (body["from"], body["to"]) == ("2026-09-20T10:00:00Z", "2026-09-25T10:00:00Z")
    assert window(session) == (datetime(2026, 9, 20, 10, tzinfo=timezone.utc), NOW)


@pytest.mark.parametrize("url, key", GROUPED)
def test_grouped_report_rejects_invalid_window(client, session, url, key):
    params = {"from": "2026-09-21T00:00:00Z", "to": "2026-09-20T00:00:00Z"}

    response = client.get(url, params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": "from must be earlier than to"}
    assert session.statements == []


# === timeseries =============================================================================

TIMESERIES = "/api/v1/reports/timeseries"


def bucket_row(bucket_start: datetime, total: int, passed: int, failed: int):
    return SimpleNamespace(bucket_start=bucket_start, total=total, passed=passed, failed=failed)


def test_timeseries_requires_interval(client, session):
    response = client.get(TIMESERIES)

    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["query", "interval"]
    assert error["type"] == "missing"
    assert session.statements == []


@pytest.mark.parametrize("interval", ["hour", "day"])
def test_timeseries_accepts_hour_and_day(client, session, interval):
    response = client.get(TIMESERIES, params={"interval": interval})

    assert response.status_code == 200
    assert response.json() == {
        "from": "2026-09-18T10:00:00Z", "to": "2026-09-25T10:00:00Z", "items": [],
    }
    (statement,) = session.statements
    assert interval in statement.compile(dialect=postgresql.dialect()).params.values()


@pytest.mark.parametrize("interval", ["minute", "HOUR", "week", "", "hour'; DROP TABLE x;--"])
def test_timeseries_rejects_other_intervals(client, session, interval):
    response = client.get(TIMESERIES, params={"interval": interval})

    assert response.status_code == 422
    assert session.statements == []


def test_timeseries_maps_bucket_rows(client, session):
    session.rows = [
        bucket_row(datetime(2026, 9, 16, 10, tzinfo=timezone.utc), 15, 14, 1),
        bucket_row(datetime(2026, 9, 16, 12, tzinfo=timezone.utc), 3, 3, 0),
    ]
    params = {"interval": "hour", "from": "2026-09-16T00:00:00Z", "to": "2026-09-23T00:00:00Z"}

    body = client.get(TIMESERIES, params=params).json()

    assert body == {
        "from": "2026-09-16T00:00:00Z",
        "to": "2026-09-23T00:00:00Z",
        "items": [
            {"bucket_start": "2026-09-16T10:00:00Z", "total": 15, "passed": 14, "failed": 1},
            {"bucket_start": "2026-09-16T12:00:00Z", "total": 3, "passed": 3, "failed": 0},
        ],
    }
    # Only buckets returned by the database; the missing 11:00 bucket is not invented.
    assert "pass_rate_percent" not in body["items"][0]


def test_timeseries_bucket_start_with_other_offset_is_shown_in_utc(client, session):
    plus_2 = timezone(timedelta(hours=2))
    session.rows = [bucket_row(datetime(2026, 9, 20, 12, 0, tzinfo=plus_2), 1, 1, 0)]

    body = client.get(TIMESERIES, params={"interval": "hour"}).json()

    assert body["items"][0]["bucket_start"] == "2026-09-20T10:00:00Z"


def test_timeseries_bucket_may_start_before_from(client, session):
    # from=10:37, results at 10:45 and 10:50 -> PostgreSQL returns the 10:00 bucket.
    session.rows = [bucket_row(datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc), 2, 2, 0)]
    params = {"interval": "hour", "from": "2026-09-20T10:37:00Z", "to": "2026-09-20T11:00:00Z"}

    body = client.get(TIMESERIES, params=params).json()

    assert body["from"] == "2026-09-20T10:37:00Z"
    assert body["items"] == [
        {"bucket_start": "2026-09-20T10:00:00Z", "total": 2, "passed": 2, "failed": 0},
    ]
    assert window(session)[0] == datetime(2026, 9, 20, 10, 37, tzinfo=timezone.utc)


def test_timeseries_passes_window_and_filters_to_the_query(client, session):
    params = {"interval": "day", "from": "2026-09-20T12:00:00+02:00",
              "device_id": "ECU-004", "test_name": "Sleep Current"}

    body = client.get(TIMESERIES, params=params).json()

    assert body["from"] == "2026-09-20T10:00:00Z"
    assert where(session) == [
        ("started_at", operator.ge, datetime(2026, 9, 20, 10, tzinfo=timezone.utc)),
        ("started_at", operator.lt, NOW),
        ("device_id", operator.eq, "ECU-004"),
        ("test_name", operator.eq, "Sleep Current"),
    ]


def test_timeseries_rejects_invalid_window(client, session):
    params = {"interval": "hour", "from": "2026-09-20T00:00:00Z", "to": "2026-09-20T00:00:00Z"}

    response = client.get(TIMESERIES, params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": "from must be earlier than to"}


# === shared behaviour of the new report endpoints =============================================

NEW_REPORTS = [BY_DEVICE, BY_TEST, TIMESERIES + "?interval=hour"]


@pytest.mark.parametrize("url", NEW_REPORTS)
@pytest.mark.parametrize("make_error", UNAVAILABLE_ERRORS.values(), ids=UNAVAILABLE_ERRORS.keys())
def test_new_reports_database_unavailable_returns_503(client, session, url, make_error):
    session.error = make_error()

    response = client.get(url)

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert session.closed


@pytest.mark.parametrize("url", NEW_REPORTS)
@pytest.mark.parametrize("make_error", UNEXPECTED_ERRORS.values(), ids=UNEXPECTED_ERRORS.keys())
def test_new_reports_other_database_errors_return_500(client, session, url, make_error):
    session.error = make_error()

    response = client.get(url)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    for marker in LEAK_MARKERS:
        assert marker not in response.text
    assert session.closed


@pytest.mark.parametrize("url", NEW_REPORTS)
def test_new_reports_need_no_api_key_and_only_allow_get(client, url):
    assert client.get(url).status_code == 200
    for method in ("post", "put", "delete"):
        assert getattr(client, method)(url).status_code == 405
