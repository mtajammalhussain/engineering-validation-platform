"""The four reports against real PostgreSQL, using the session's main dataset.

Expected numbers are derived by hand from MAIN_ROWS in support.py (see the table there).
Every test uses explicit from/to inside the isolated period, so results do not depend on
the clock, on other data in the database, or on the order in which tests run. No test
here inserts rows.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app

pytestmark = pytest.mark.integration

SUMMARY = "/api/v1/reports/summary"
BY_DEVICE = "/api/v1/reports/by-device"
BY_TEST = "/api/v1/reports/by-test"
TIMESERIES = "/api/v1/reports/timeseries"

MAIN = {"from": "2001-06-20T00:00:00Z", "to": "2001-06-22T00:00:00Z"}
DAY_1 = {"from": "2001-06-20T00:00:00Z", "to": "2001-06-21T00:00:00Z"}


def get(client, url, **params):
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- summary ---------------------------------------------------------------------------------

def test_summary_counts_the_whole_period(client):
    assert get(client, SUMMARY, **MAIN) == {
        "from": "2001-06-20T00:00:00Z",
        "to": "2001-06-22T00:00:00Z",
        "total": 13,
        "passed": 9,
        "failed": 4,
        "pass_rate_percent": 69.2,
    }


@pytest.mark.parametrize(
    "filters, counts",
    [
        ({"device_id": "ECU-901"}, (4, 2, 2, 50.0)),
        ({"test_name": "Sleep Current"}, (6, 4, 2, 66.7)),
        ({"device_id": "ECU-901", "test_name": "Sleep Current"}, (2, 1, 1, 50.0)),
    ],
    ids=["device", "test", "both"],
)
def test_summary_exact_filters(client, filters, counts):
    body = get(client, SUMMARY, **MAIN, **filters)

    assert (body["total"], body["passed"], body["failed"], body["pass_rate_percent"]) == counts


def test_summary_without_matches_is_zero_with_null_pass_rate(client):
    body = get(client, SUMMARY, **MAIN, device_id="ECU-999")

    assert (body["total"], body["passed"], body["failed"]) == (0, 0, 0)
    assert body["pass_rate_percent"] is None


def test_window_is_from_inclusive_to_exclusive(client):
    # Existing rows on 06-20: 10:00 (== from), 10:15, 10:30, 10:45, 11:00 (== to).
    body = get(client, SUMMARY, **{"from": "2001-06-20T10:00:00Z", "to": "2001-06-20T11:00:00Z"})

    # 10:00 FAIL, 10:15 PASS, 10:30 PASS, 10:45 FAIL included; 11:00 FAIL excluded.
    assert (body["total"], body["passed"], body["failed"]) == (4, 2, 2)


# --- by-device -------------------------------------------------------------------------------

def test_by_device_groups_counts_and_orders(client):
    body = get(client, BY_DEVICE, **MAIN)

    assert (body["from"], body["to"]) == (MAIN["from"], MAIN["to"])
    assert body["items"] == [
        {"device_id": "ECU-901", "total": 4, "passed": 2, "failed": 2, "pass_rate_percent": 50.0},
        # Tie on failed=1: ECU-902 first only because of device_id ASC (ECU-903 has more
        # results and a higher pass rate, and its rows were inserted first).
        {"device_id": "ECU-902", "total": 4, "passed": 3, "failed": 1, "pass_rate_percent": 75.0},
        {"device_id": "ECU-903", "total": 5, "passed": 4, "failed": 1, "pass_rate_percent": 80.0},
    ]


def test_by_device_with_test_filter(client):
    body = get(client, BY_DEVICE, **MAIN, test_name="Wake-up Time")

    assert body["items"] == [
        {"device_id": "ECU-902", "total": 1, "passed": 0, "failed": 1, "pass_rate_percent": 0.0},
        {"device_id": "ECU-901", "total": 1, "passed": 1, "failed": 0,
         "pass_rate_percent": 100.0},
        {"device_id": "ECU-903", "total": 2, "passed": 2, "failed": 0,
         "pass_rate_percent": 100.0},
    ]


def test_by_device_with_device_filter(client):
    body = get(client, BY_DEVICE, **MAIN, device_id="ECU-903")

    assert body["items"] == [
        {"device_id": "ECU-903", "total": 5, "passed": 4, "failed": 1, "pass_rate_percent": 80.0},
    ]


# --- by-test ---------------------------------------------------------------------------------

def test_by_test_groups_counts_and_orders(client):
    body = get(client, BY_TEST, **MAIN)

    assert body["items"] == [
        {"test_name": "Sleep Current", "total": 6, "passed": 4, "failed": 2,
         "pass_rate_percent": 66.7},
        # Tie on failed=1: "CAN Cycle Time" first only because of test_name ASC.
        {"test_name": "CAN Cycle Time", "total": 3, "passed": 2, "failed": 1,
         "pass_rate_percent": 66.7},
        {"test_name": "Wake-up Time", "total": 4, "passed": 3, "failed": 1,
         "pass_rate_percent": 75.0},
    ]


def test_by_test_unknown_filter_gives_empty_items(client):
    assert get(client, BY_TEST, **MAIN, test_name="No Such Test") == {
        "from": MAIN["from"], "to": MAIN["to"], "items": [],
    }


# --- timeseries ------------------------------------------------------------------------------

def test_hourly_timeseries_runs_date_trunc_group_by_in_postgresql(client):
    # Regression test for the PostgreSQL-only GROUP BY expression problem (Stage 6).
    body = get(client, TIMESERIES, interval="hour", **DAY_1)

    assert body["items"] == [
        {"bucket_start": "2001-06-20T10:00:00Z", "total": 4, "passed": 2, "failed": 2},
        {"bucket_start": "2001-06-20T11:00:00Z", "total": 3, "passed": 1, "failed": 2},
        {"bucket_start": "2001-06-20T23:00:00Z", "total": 1, "passed": 1, "failed": 0},
    ]


def test_daily_timeseries_combines_hours_into_utc_days(client):
    body = get(client, TIMESERIES, interval="day", **MAIN)

    assert body["items"] == [
        {"bucket_start": "2001-06-20T00:00:00Z", "total": 8, "passed": 4, "failed": 4},
        {"bucket_start": "2001-06-21T00:00:00Z", "total": 5, "passed": 5, "failed": 0},
    ]


def test_timeseries_with_filter(client):
    body = get(client, TIMESERIES, interval="day", device_id="ECU-902", **MAIN)

    assert body["items"] == [
        {"bucket_start": "2001-06-20T00:00:00Z", "total": 3, "passed": 2, "failed": 1},
        {"bucket_start": "2001-06-21T00:00:00Z", "total": 1, "passed": 1, "failed": 0},
    ]


def test_daily_buckets_are_utc_days_even_with_a_berlin_session(
    seeded, test_db_config, monkeypatch
):
    # libpq reads PGTZ when it opens a connection and sets the session time zone.
    monkeypatch.setenv("PGTZ", "Europe/Berlin")

    with TestClient(create_app(test_db_config.settings())) as client:
        with client.app.state.engine.connect() as connection:
            session_zone = connection.execute(text("SHOW timezone")).scalar_one()
            # Control: without the 'UTC' argument, the day of 23:30Z starts at
            # 22:00Z (Berlin midnight), i.e. the session zone really changes date_trunc.
            two_argument_day = connection.execute(
                text("SELECT date_trunc('day', TIMESTAMPTZ '2001-06-20 23:30:00+00') "
                     "AT TIME ZONE 'UTC'")
            ).scalar_one()
        # Without a real non-UTC session this test would prove nothing: fail, not continue.
        assert session_zone == "Europe/Berlin"
        assert two_argument_day.isoformat() == "2001-06-20T22:00:00"

        body = get(client, TIMESERIES, interval="day", **MAIN)

    # 2001-06-20T23:30Z is 2001-06-21 01:30 in Berlin, but belongs to the UTC day 06-20.
    assert body["items"] == [
        {"bucket_start": "2001-06-20T00:00:00Z", "total": 8, "passed": 4, "failed": 4},
        {"bucket_start": "2001-06-21T00:00:00Z", "total": 5, "passed": 5, "failed": 0},
    ]
