"""Validation rules of docs/APP_SPEC.md §5.2, one test group per rule."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models import Result
from app.schemas import ResultCreate, ResultList, ResultRead

NAN, INF = float("nan"), float("inf")


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def valid_payload(**overrides) -> dict:
    """A valid POST body like the spec example; started_at one minute in the past."""
    payload = {
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
        "source": "simulator",
    }
    payload.update(overrides)
    return payload


def assert_rejected(payload: dict, field: str) -> None:
    """The payload must fail validation, and the error must name ``field``."""
    with pytest.raises(ValidationError) as exc_info:
        ResultCreate.model_validate(payload)
    assert field in {error["loc"][0] for error in exc_info.value.errors()}


# --- valid payloads ---------------------------------------------------------

def test_valid_payload_is_accepted():
    result = ResultCreate.model_validate(valid_payload())

    assert result.device_id == "ECU-003"
    assert result.verdict == "PASS"
    assert result.started_at.tzinfo is not None


def test_minimal_payload_without_optional_fields_is_accepted():
    payload = valid_payload()
    for name in ("limit_min", "limit_max", "source"):
        del payload[name]

    result = ResultCreate.model_validate(payload)

    assert result.limit_min is None and result.limit_max is None


@pytest.mark.parametrize("field", list(valid_payload()))
def test_required_fields(field):
    payload = valid_payload()
    del payload[field]

    if field in ("limit_min", "limit_max", "source"):
        ResultCreate.model_validate(payload)
    else:
        assert_rejected(payload, field)


def test_unknown_field_is_rejected():
    assert_rejected(valid_payload(operator="me"), "operator")


# --- device_id --------------------------------------------------------------

@pytest.mark.parametrize("device_id", ["ECU-000", "ECU-001", "ECU-999"])
def test_device_id_valid(device_id):
    ResultCreate.model_validate(valid_payload(device_id=device_id))


@pytest.mark.parametrize(
    "device_id",
    [
        "", "ECU-01", "ECU-0001", "ecu-001", "ECU001", "ECU-00A", "XECU-001", "ECU-001\n",
        "ECU 001",
        "ECU-١٢٣",  # Arabic-Indic digits
        "ECU-１２３",  # full-width digits
    ],
)
def test_device_id_invalid(device_id):
    assert_rejected(valid_payload(device_id=device_id), "device_id")


# --- test_name and unit lengths ------------------------------------------------

@pytest.mark.parametrize("field, max_len", [("test_name", 100), ("unit", 10)])
def test_text_length_boundaries(field, max_len):
    ResultCreate.model_validate(valid_payload(**{field: "x"}))
    ResultCreate.model_validate(valid_payload(**{field: "x" * max_len}))
    assert_rejected(valid_payload(**{field: ""}), field)
    assert_rejected(valid_payload(**{field: "x" * (max_len + 1)}), field)


# --- temperature_c ----------------------------------------------------------

@pytest.mark.parametrize("value", [-40, -40.0, 23, 125, 125.0])
def test_temperature_valid(value):
    ResultCreate.model_validate(valid_payload(temperature_c=value))


@pytest.mark.parametrize("value", [-40.01, -41, 125.01, 126, NAN, INF, -INF])
def test_temperature_invalid(value):
    assert_rejected(valid_payload(temperature_c=value), "temperature_c")


# --- finite numbers: measured_value, limit_min, limit_max ----------------------

@pytest.mark.parametrize("field", ["measured_value", "limit_min", "limit_max"])
@pytest.mark.parametrize("value", [NAN, INF, -INF])
def test_non_finite_numbers_are_rejected(field, value):
    assert_rejected(valid_payload(**{field: value}), field)


@pytest.mark.parametrize("field", ["measured_value", "limit_min", "limit_max"])
@pytest.mark.parametrize("value", [0, -5.5, 137.0, 1e9])
def test_finite_numbers_are_accepted(field, value):
    ResultCreate.model_validate(valid_payload(**{field: value}))


@pytest.mark.parametrize("field", ["limit_min", "limit_max"])
def test_limits_may_be_null(field):
    result = ResultCreate.model_validate(valid_payload(**{field: None}))

    assert getattr(result, field) is None


def test_no_limit_logic_in_the_api():
    # Out of limits but reported as PASS: the API stores what the bench decided.
    result = ResultCreate.model_validate(valid_payload(measured_value=999, verdict="PASS"))

    assert result.verdict == "PASS"


# --- verdict ----------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_verdict_valid(verdict):
    ResultCreate.model_validate(valid_payload(verdict=verdict))


@pytest.mark.parametrize("verdict", ["OK", "pass", "Fail", "", None, 1])
def test_verdict_invalid(verdict):
    assert_rejected(valid_payload(verdict=verdict), "verdict")


# --- duration_s -------------------------------------------------------------

@pytest.mark.parametrize("value", [1e-9, 0.001, 42.0])
def test_duration_valid(value):
    ResultCreate.model_validate(valid_payload(duration_s=value))


@pytest.mark.parametrize("value", [0, 0.0, -0.001, -1, NAN, INF, -INF, "inf", "Infinity"])
def test_duration_invalid(value):
    assert_rejected(valid_payload(duration_s=value), "duration_s")


# --- started_at -------------------------------------------------------------
# The 5-minute boundary moves with the clock, so tests stay one minute away from it
# on either side (4 and 6 minutes) instead of testing exactly on the boundary.

def test_started_at_naive_string_is_rejected():
    assert_rejected(valid_payload(started_at="2026-09-23T19:30:00"), "started_at")


def test_started_at_naive_datetime_is_rejected():
    naive = datetime.now() - timedelta(minutes=1)

    assert_rejected(valid_payload(started_at=naive), "started_at")


@pytest.mark.parametrize("started_at", ["2026-09-23T19:30:00Z", "2026-09-23T21:30:00+02:00"])
def test_started_at_past_with_timezone_is_accepted(started_at):
    result = ResultCreate.model_validate(valid_payload(started_at=started_at))

    assert result.started_at == datetime(2026, 9, 23, 19, 30, tzinfo=timezone.utc)


def test_started_at_slightly_in_future_is_accepted():
    near_future = datetime.now(timezone.utc) + timedelta(minutes=4)

    ResultCreate.model_validate(valid_payload(started_at=iso(near_future)))


def test_started_at_too_far_in_future_is_rejected():
    far_future = datetime.now(timezone.utc) + timedelta(minutes=6)

    assert_rejected(valid_payload(started_at=iso(far_future)), "started_at")


def test_future_check_respects_timezone_offset():
    # 6 minutes ahead, written in UTC+02:00: still too far in the future.
    far_future = datetime.now(timezone(timedelta(hours=2))) + timedelta(minutes=6)

    assert_rejected(valid_payload(started_at=far_future.isoformat()), "started_at")


def test_started_at_not_a_date_is_rejected():
    assert_rejected(valid_payload(started_at="yesterday"), "started_at")


# --- source -----------------------------------------------------------------

def test_source_omitted_is_not_set():
    payload = valid_payload()
    del payload["source"]

    result = ResultCreate.model_validate(payload)

    # exclude_unset=True leaves "source" out, so PostgreSQL can apply its default.
    assert "source" not in result.model_dump(exclude_unset=True)


def test_source_given_is_kept():
    result = ResultCreate.model_validate(valid_payload(source="HIL-bench-7"))

    assert result.model_dump(exclude_unset=True)["source"] == "HIL-bench-7"


def test_source_explicit_null_is_rejected():
    assert_rejected(valid_payload(source=None), "source")


# --- response models --------------------------------------------------------

def test_result_read_from_database_object():
    row = Result(
        id=1,
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

    read = ResultRead.model_validate(row)

    assert read.id == 1
    assert read.source == "simulator"
    assert set(ResultRead.model_fields) == set(Result.__table__.columns.keys())


def test_result_list_shape():
    body = ResultList(items=[], total=0, limit=50, offset=0).model_dump()

    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}
