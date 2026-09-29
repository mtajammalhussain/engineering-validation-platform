from dataclasses import FrozenInstanceError

import pytest

from simulator.catalog import (
    ACTIVE_SUPPLY_CURRENT,
    CAN_CYCLE_TIME,
    SLEEP_CURRENT,
    UNDERVOLTAGE_RESET,
    WAKEUP_TIME,
    TestSpec,
)
from simulator.client import DeliveryResult
from simulator.output import (
    BatchSummary,
    format_limits,
    format_rejected_line,
    format_result_line,
    format_summary_line,
    format_undelivered_line,
)
from tests.unit.fakes import API_KEY, EXCEPTION_CANARY, SERVER_CANARY, make_generated

CAN = make_generated(CAN_CYCLE_TIME, "ECU-005", 23, 10.0, "PASS")


# --- result lines (spec §8.4 examples, exact) ---

@pytest.mark.parametrize(
    "generated, expected",
    [
        (make_generated(SLEEP_CURRENT, "ECU-001", -30, 0.18, "PASS"),
         "ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA"
         " | Result: PASS"),
        (make_generated(SLEEP_CURRENT, "ECU-002", -40, 0.52, "FAIL"),
         "ECU-002 | Test: Sleep Current | Temperature: -40°C | Measured current: 0.52 mA"
         " | Result: FAIL (limits: 0.01–0.40 mA)"),
        (make_generated(WAKEUP_TIME, "ECU-007", 85, 97.0, "PASS"),
         "ECU-007 | Test: Wake-up Time | Temperature: 85°C | Measured time: 97.00 ms"
         " | Result: PASS"),
        (make_generated(WAKEUP_TIME, "ECU-004", 105, 163.25, "FAIL"),
         "ECU-004 | Test: Wake-up Time | Temperature: 105°C | Measured time: 163.25 ms"
         " | Result: FAIL (limits: ≤ 150 ms)"),
    ],
)
def test_result_line_matches_spec_examples(generated, expected):
    assert format_result_line(generated) == expected


@pytest.mark.parametrize("value, shown", [(0.0, "0.00"), (0.4, "0.40"), (250.0, "250.00"),
                                          (9.5, "9.50"), (187.5, "187.50")])
def test_measured_value_always_has_two_decimals(value, shown):
    line = format_result_line(make_generated(ACTIVE_SUPPLY_CURRENT, "ECU-001", 23, value, "PASS"))

    assert f"Measured current: {shown} mA |" in line


@pytest.mark.parametrize("temperature, shown", [(-40, "-40°C"), (23, "23°C"), (105, "105°C")])
def test_temperature_is_an_integer_with_degree_sign(temperature, shown):
    line = format_result_line(make_generated(SLEEP_CURRENT, "ECU-001", temperature, 0.2, "PASS"))

    assert f"| Temperature: {shown} |" in line


def test_quantity_and_unit_come_from_the_test_spec():
    line = format_result_line(make_generated(UNDERVOLTAGE_RESET, "ECU-009", 23, 7.0, "FAIL"))

    assert line == (
        "ECU-009 | Test: Undervoltage Reset | Temperature: 23°C | Measured voltage: 7.00 V"
        " | Result: FAIL (limits: 5.5–6.5 V)"
    )


def test_pass_line_has_no_limits():
    assert "limits" not in format_result_line(CAN)


# --- limits ---

@pytest.mark.parametrize(
    "spec, expected",
    [
        (SLEEP_CURRENT, "0.01–0.40 mA"),
        (ACTIVE_SUPPLY_CURRENT, "80–250 mA"),
        (WAKEUP_TIME, "≤ 150 ms"),
        (CAN_CYCLE_TIME, "9.5–10.5 ms"),
        (UNDERVOLTAGE_RESET, "5.5–6.5 V"),
    ],
    ids=lambda value: getattr(value, "key", ""),
)
def test_catalog_limits(spec, expected):
    assert format_limits(spec.limit_min, spec.limit_max, spec.unit) == expected


def test_minimum_only_limit():
    assert format_limits(5.5, None, "V") == "≥ 5.5 V"


def test_two_sided_uses_an_en_dash_not_a_hyphen():
    text = format_limits(0.01, 0.40, "mA")

    assert "–" in text and "-" not in text


def test_both_limits_missing_is_rejected():
    with pytest.raises(ValueError):
        format_limits(None, None, "mA")


@pytest.mark.parametrize(
    "limit_min, limit_max, expected",
    [
        (None, 80, "≤ 80 mA"),          # int -> 0 decimals
        (None, 80.0, "≤ 80 mA"),        # float without fraction -> 0 decimals
        (80.0, 250.0, "80–250 mA"),
        (None, 9.5, "≤ 9.5 mA"),        # 1 decimal
        (0.01, 0.40, "0.01–0.40 mA"),   # shared 2 decimals, not "0.01–0.4"
        (0.40, 0.5, "0.4–0.5 mA"),      # both fit in 1 decimal
        (5, 6.5, "5.0–6.5 mA"),         # shared precision: 5 is shown as 5.0
    ],
)
def test_limit_precision_is_the_fewest_decimals_shared_by_both_limits(
    limit_min, limit_max, expected
):
    assert format_limits(limit_min, limit_max, "mA") == expected


@pytest.mark.parametrize("limit_min, limit_max", [(None, 0.005), (0.005, 0.4), (1.234, None)])
def test_limits_needing_more_than_two_decimals_are_rejected(limit_min, limit_max):
    with pytest.raises(ValueError):
        format_limits(limit_min, limit_max, "mA")


def test_minimum_only_spec_formats_in_a_fail_line():
    spec = TestSpec("MIN_ONLY", "Min Only", "voltage", "V", 5.5, None)

    line = format_result_line(make_generated(spec, "ECU-001", 23, 5.0, "FAIL"))

    assert line.endswith("| Result: FAIL (limits: ≥ 5.5 V)")


# --- REJECTED ---

@pytest.mark.parametrize(
    "status, reason",
    [
        (401, "invalid or missing API key"),
        (422, "payload failed validation"),
        (200, "unexpected response"),
        (302, "unexpected response"),
        (404, "unexpected response"),
        (429, "unexpected response"),
    ],
)
def test_rejected_line(status, reason):
    delivery = DeliveryResult("rejected", 1, status_code=status, detail="http")

    line = format_rejected_line(CAN, delivery)

    assert line == f"ECU-005 | Test: CAN Cycle Time | REJECTED: HTTP {status} ({reason})"
    for secret in (SERVER_CANARY, EXCEPTION_CANARY, API_KEY):
        assert secret not in line


def test_rejected_line_example_from_the_spec():
    sleep = make_generated(SLEEP_CURRENT, "ECU-001", -30, 0.18, "PASS")

    assert format_rejected_line(sleep, DeliveryResult("rejected", 1, 401, detail="http")) == (
        "ECU-001 | Test: Sleep Current | REJECTED: HTTP 401 (invalid or missing API key)"
    )


@pytest.mark.parametrize(
    "delivery",
    [
        DeliveryResult("rejected", 1, status_code=None, detail="http"),
        DeliveryResult("undelivered", 3, status_code=503, detail="http"),
    ],
)
def test_rejected_line_needs_a_rejected_delivery_with_status(delivery):
    with pytest.raises(ValueError):
        format_rejected_line(CAN, delivery)


# --- UNDELIVERED ---

@pytest.mark.parametrize(
    "delivery, reason",
    [
        (DeliveryResult("undelivered", 3, status_code=503, detail="http"),
         "HTTP 503 after 3 attempts"),
        (DeliveryResult("undelivered", 3, exception_name="ConnectError", detail="connection"),
         "ConnectError after 3 attempts"),
        (DeliveryResult("undelivered", 1, exception_name="ReadTimeout", detail="after_send"),
         "ReadTimeout, not retried (may have been stored)"),
        (DeliveryResult("undelivered", 2, exception_name="ReadTimeout", detail="after_send"),
         "ReadTimeout, not retried (may have been stored)"),
        (DeliveryResult("undelivered", 1, status_code=503, detail="shutdown"),
         "shutdown requested before retry"),
        (DeliveryResult("undelivered", 1, detail="shutdown"),
         "shutdown requested before retry"),
    ],
)
def test_undelivered_line(delivery, reason):
    assert format_undelivered_line(CAN, delivery) == (
        f"ECU-005 | Test: CAN Cycle Time | UNDELIVERED: {reason}"
    )


@pytest.mark.parametrize(
    "delivery",
    [
        DeliveryResult("undelivered", 3, status_code=None, detail="http"),          # HTTP None
        DeliveryResult("undelivered", 3, exception_name=None, detail="connection"),  # None after
        DeliveryResult("undelivered", 1, exception_name=None, detail="after_send"),
        DeliveryResult("undelivered", 3, status_code=503, detail=None),
        DeliveryResult("rejected", 1, status_code=401, detail="http"),
    ],
)
def test_undelivered_line_rejects_incomplete_metadata(delivery):
    with pytest.raises(ValueError):
        format_undelivered_line(CAN, delivery)


# --- summary ---

def test_summary_line():
    summary = BatchSummary(sent=10, accepted=8, rejected=1, undelivered=1, passed=7, failed=1)

    assert format_summary_line(summary) == (
        "Batch done: 10 sent | 8 accepted | 1 rejected | 1 undelivered | 7 PASS | 1 FAIL"
    )


def test_summary_line_shows_zeros():
    assert format_summary_line(BatchSummary(0, 0, 0, 0, 0, 0)) == (
        "Batch done: 0 sent | 0 accepted | 0 rejected | 0 undelivered | 0 PASS | 0 FAIL"
    )


@pytest.mark.parametrize(
    "counts",
    [
        (1, 1, 0, 0, 2, -1),   # negative
        (-1, 0, 0, -1, 0, 0),  # negative
        (3, 1, 1, 0, 1, 0),    # sent != accepted + rejected + undelivered
        (2, 2, 0, 0, 1, 0),    # accepted != passed + failed
    ],
)
def test_summary_rejects_impossible_counts(counts):
    with pytest.raises(ValueError):
        BatchSummary(*counts)


def test_summary_is_frozen():
    with pytest.raises(FrozenInstanceError):
        BatchSummary(0, 0, 0, 0, 0, 0).sent = 1
