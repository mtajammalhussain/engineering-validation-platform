"""Test helpers shared by the unit and the integration tests.

Only plain request data: no fakes, no database, no imports from tests.unit or
tests.integration.
"""

from datetime import datetime, timedelta, timezone

URL = "/api/v1/results"


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
