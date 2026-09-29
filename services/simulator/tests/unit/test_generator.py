import json
import math
import random
import re
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from simulator.catalog import (
    ACTIVE_SUPPLY_CURRENT,
    CAN_CYCLE_TIME,
    CATALOG,
    SLEEP_CURRENT,
    UNDERVOLTAGE_RESET,
    WAKEUP_TIME,
    TestSpec,
)
from simulator.generator import (
    TEMPERATURES_C,
    GeneratedResult,
    effective_failure_probability,
    generate_measurement_hundredths,
    generate_result,
    generation_range,
    outside_ranges,
    to_hundredths,
)
from simulator.verdict import evaluate

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
PAYLOAD_FIELDS = {
    "device_id", "test_name", "temperature_c", "measured_value", "unit", "limit_min",
    "limit_max", "verdict", "started_at", "duration_s", "source",
}
MIN_ONLY = TestSpec("MIN_ONLY", "Min Only", "voltage", "V", 5.5, None)
ZERO_MIN = TestSpec("ZERO_MIN", "Zero Min", "current", "mA", 0.0, 0.40)


class ScriptedRng:
    """Fake random generator: returns scripted answers and records every call in order.

    ``choices`` are indexes into the sequence passed to ``choice()``; ``randints`` and
    ``randoms`` are returned as they are. Running out of answers fails the test.
    """

    def __init__(self, choices=(), randints=(), randoms=()):
        self._choices, self._randints, self._randoms = list(choices), list(randints), list(randoms)
        self.calls = []

    def choice(self, seq):
        self.calls.append(("choice", tuple(seq)))
        return seq[self._choices.pop(0)]

    def randint(self, low, high):
        self.calls.append(("randint", low, high))
        value = self._randints.pop(0)
        assert low <= value <= high, "scripted randint outside the requested range"
        return value

    def random(self):
        self.calls.append(("random",))
        return self._randoms.pop(0)


def generate(rng, device_count=20, failure_rate=0.08, now=NOW):
    return generate_result(rng, now, device_count=device_count, failure_rate=failure_rate)


def seeded_run(seed, count=200, **kwargs):
    rng = random.Random(seed)  # seeded once, like the real process
    return [generate(rng, **kwargs) for _ in range(count)]


def on_grid(value, steps_per_unit):
    return round(value * steps_per_unit) / steps_per_unit == value


# --- integer hundredths (floating-point safety) ---

@pytest.mark.parametrize(
    "limit, hundredths",
    [(0.01, 1), (0.40, 40), (80, 8000), (250, 25000), (150, 15000),
     (9.5, 950), (10.5, 1050), (5.5, 550), (6.5, 650)],
)
def test_every_catalog_limit_converts_to_its_intended_hundredths(limit, hundredths):
    assert to_hundredths(limit) == hundredths
    assert hundredths / 100 == limit  # and back again, exactly


def test_the_float_trap_that_to_hundredths_avoids():
    # Binary floats cannot hold most decimals exactly; ceil/int would move a limit one step.
    assert 0.07 * 100 == 7.000000000000001 and math.ceil(0.07 * 100) == 8
    assert 0.29 * 100 == 28.999999999999996 and int(0.29 * 100) == 28
    assert to_hundredths(0.07) == 7
    assert to_hundredths(0.29) == 29


def test_k_40_is_the_real_upper_sleep_current_limit():
    assert 40 / 100 == SLEEP_CURRENT.limit_max
    assert evaluate(40 / 100, SLEEP_CURRENT.limit_min, SLEEP_CURRENT.limit_max) == "PASS"


@pytest.mark.parametrize(
    "spec, inside, below, above",
    [
        (SLEEP_CURRENT, (1, 40), (0, 0), (41, 59)),  # 0.40 + 0.195 -> 0.59, not 0.60
        (ACTIVE_SUPPLY_CURRENT, (8000, 25000), (0, 7999), (25001, 33500)),
        (WAKEUP_TIME, (7500, 15000), None, (15001, 18750)),  # generation minimum 75.00
        (CAN_CYCLE_TIME, (950, 1050), (900, 949), (1051, 1100)),
        (UNDERVOLTAGE_RESET, (550, 650), (500, 549), (651, 700)),
    ],
    ids=lambda value: getattr(value, "key", ""),
)
def test_generation_ranges_in_hundredths(spec, inside, below, above):
    assert generation_range(spec) == inside
    assert outside_ranges(spec) == (below, above)


def test_sleep_current_ranges_never_reach_0_60():
    below, above = outside_ranges(SLEEP_CURRENT)
    assert 60 not in range(above[0], above[1] + 1)
    assert below == (0, 0)


def test_empty_below_range_is_none():
    assert outside_ranges(ZERO_MIN) == (None, (41, 60))  # span 40 -> above up to 0.60


# --- measured value generation (scripted random answers) ---

def test_inside_value_is_drawn_from_the_inside_range():
    rng = ScriptedRng(randoms=[0.5], randints=[25])

    assert generate_measurement_hundredths(rng, SLEEP_CURRENT, fail=False) == 25
    assert rng.calls == [("random",), ("randint", 1, 40)]


@pytest.mark.parametrize("index, expected", [(0, 1), (1, 40)])
def test_two_sided_boundary_is_min_or_max(index, expected):
    rng = ScriptedRng(randoms=[0.0], choices=[index])

    k = generate_measurement_hundredths(rng, SLEEP_CURRENT, fail=False)

    assert k == expected
    assert rng.calls == [("random",), ("choice", (1, 40))]
    assert evaluate(k / 100, SLEEP_CURRENT.limit_min, SLEEP_CURRENT.limit_max) == "PASS"


def test_wakeup_boundary_is_the_real_150_not_the_generation_minimum():
    rng = ScriptedRng(randoms=[0.0])

    k = generate_measurement_hundredths(rng, WAKEUP_TIME, fail=False)

    assert k == 15000
    assert rng.calls == [("random",)]
    assert evaluate(k / 100, None, 150) == "PASS"


def test_boundary_rate_is_about_two_percent():
    rng = ScriptedRng(randoms=[0.0199], choices=[0])
    assert generate_measurement_hundredths(rng, SLEEP_CURRENT, fail=False) == 1

    rng = ScriptedRng(randoms=[0.02], randints=[20])
    assert generate_measurement_hundredths(rng, SLEEP_CURRENT, fail=False) == 20


@pytest.mark.parametrize(
    "side_draw, expected_range",
    [(0.49, (0, 0)), (0.5, (41, 59))],
    ids=["below", "above"],
)
def test_two_sided_failure_side_is_50_50(side_draw, expected_range):
    rng = ScriptedRng(randoms=[side_draw], randints=[expected_range[0]])

    generate_measurement_hundredths(rng, SLEEP_CURRENT, fail=True)

    assert rng.calls == [("random",), ("randint", *expected_range)]


def test_wakeup_failure_has_no_side_draw_and_uses_the_above_range():
    rng = ScriptedRng(randints=[18750])

    k = generate_measurement_hundredths(rng, WAKEUP_TIME, fail=True)

    assert k == 18750
    assert rng.calls == [("randint", 15001, 18750)]
    assert evaluate(k / 100, None, 150) == "FAIL"


def test_empty_below_range_falls_back_to_above():
    rng = ScriptedRng(randoms=[0.1], randints=[45])  # 0.1 would choose "below"

    assert generate_measurement_hundredths(rng, ZERO_MIN, fail=True) == 45
    assert rng.calls == [("random",), ("randint", 41, 60)]


def test_minimum_only_generation_is_rejected():
    with pytest.raises(ValueError, match="maximum"):
        generate_measurement_hundredths(ScriptedRng(randoms=[0.5]), MIN_ONLY, fail=False)
    with pytest.raises(ValueError):
        outside_ranges(MIN_ONLY)


# --- failure probability (synthetic fault model, spec §8.3.1) ---

@pytest.mark.parametrize("temperature", [-40, -30, -20])
def test_cold_sleep_current_raises_failure_probability(temperature):
    assert effective_failure_probability(SLEEP_CURRENT, temperature, 0.08) == pytest.approx(0.12)


@pytest.mark.parametrize("temperature", [23, 85, 105])
def test_non_cold_sleep_current_uses_baseline(temperature):
    assert effective_failure_probability(SLEEP_CURRENT, temperature, 0.08) == 0.08


@pytest.mark.parametrize("spec", [s for s in CATALOG if s is not SLEEP_CURRENT], ids=str)
def test_other_tests_use_baseline_even_when_cold(spec):
    assert effective_failure_probability(spec, -40, 0.08) == 0.08


def test_failure_probability_is_capped_at_one_and_zero_stays_zero():
    assert effective_failure_probability(SLEEP_CURRENT, -40, 0.8) == 1.0
    assert effective_failure_probability(SLEEP_CURRENT, -40, 1.0) == 1.0
    assert effective_failure_probability(SLEEP_CURRENT, -40, 0.0) == 0.0


# --- draw order of one complete result ---

def test_draw_order_inside_result():
    rng = ScriptedRng(choices=[0, 2], randints=[7, 25, 123], randoms=[0.99, 0.5])

    result = generate(rng)

    assert rng.calls == [
        ("choice", CATALOG),           # 1 test
        ("randint", 1, 20),            # 2 device
        ("choice", TEMPERATURES_C),    # 3 temperature
        ("random",),                   # 4 failure decision
        ("random",),                   # 5 measured value: boundary decision
        ("randint", 1, 40),            # 5 measured value: value
        ("randint", 50, 600),          # 6 duration (last)
    ]
    assert result.spec is SLEEP_CURRENT
    assert result.payload["device_id"] == "ECU-007"
    assert result.payload["temperature_c"] == -20
    assert result.payload["measured_value"] == 0.25
    assert result.payload["duration_s"] == 12.3
    assert result.payload["verdict"] == "PASS"


def test_draw_order_failing_result():
    rng = ScriptedRng(choices=[0, 0], randints=[1, 52, 600], randoms=[0.0, 0.9])

    result = generate(rng)

    assert rng.calls == [
        ("choice", CATALOG),
        ("randint", 1, 20),
        ("choice", TEMPERATURES_C),
        ("random",),                   # 4 failure decision -> fail
        ("random",),                   # 5 side -> above
        ("randint", 41, 59),           # 5 value
        ("randint", 50, 600),          # 6 duration
    ]
    assert result.payload["measured_value"] == 0.52
    assert result.payload["verdict"] == "FAIL"
    assert result.payload["duration_s"] == 60.0


def test_started_at_uses_no_random_numbers():
    rng = ScriptedRng()

    with pytest.raises(ValueError, match="timezone-aware"):
        generate(rng, now=datetime(2026, 9, 25, 10, 0))  # naive

    assert rng.calls == []


# --- payload ---

def test_payload_has_exactly_the_results_api_fields():
    result = generate(random.Random(1))

    assert set(result.payload) == PAYLOAD_FIELDS
    assert "quantity" not in result.payload
    assert "key" not in result.payload
    assert result.payload["source"] == "simulator"


def test_payload_carries_the_real_catalog_limits():
    rng = ScriptedRng(choices=[2, 3], randints=[1, 9000, 50], randoms=[0.99, 0.5])

    result = generate(rng)

    assert result.spec is WAKEUP_TIME
    assert result.payload["limit_min"] is None  # generation minimum 75 is never sent
    assert result.payload["limit_max"] == 150
    assert result.payload["test_name"] == "Wake-up Time"
    assert result.payload["unit"] == "ms"


def test_payload_is_json_serialisable():
    result = generate(random.Random(1))

    assert json.loads(json.dumps(result.payload)) == result.payload


def test_generated_result_is_immutable():
    result = generate(random.Random(1))

    with pytest.raises(FrozenInstanceError):
        result.spec = CATALOG[0]
    assert isinstance(result, GeneratedResult)
    assert result.spec in CATALOG


def test_started_at_is_utc_iso_string_with_offset():
    assert generate(random.Random(1)).payload["started_at"] == "2026-09-25T10:00:00+00:00"


def test_non_utc_time_is_normalised_to_the_same_instant_in_utc():
    local = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))

    started_at = generate(random.Random(1), now=local).payload["started_at"]

    assert started_at == "2026-09-25T10:00:00+00:00"
    assert datetime.fromisoformat(started_at) == local


# --- determinism ---

def test_same_seed_gives_the_same_sequence():
    assert seeded_run(42) == seeded_run(42)


def test_different_seeds_give_different_sequences():
    assert seeded_run(1) != seeded_run(2)


# --- broad invariants over many seeded results (no statistical assertions) ---

@pytest.mark.parametrize("failure_rate", [0.0, 0.08, 0.5, 1.0])
def test_invariants_hold_for_many_results(failure_rate):
    for result in seeded_run(2026, count=2000, device_count=12, failure_rate=failure_rate):
        p, spec = result.payload, result.spec
        assert re.fullmatch(r"ECU-(0(0[1-9]|1[0-2]))", p["device_id"])
        assert p["temperature_c"] in TEMPERATURES_C
        assert math.isfinite(p["measured_value"]) and p["measured_value"] >= 0
        assert on_grid(p["measured_value"], 100)
        assert 5.0 <= p["duration_s"] <= 60.0 and on_grid(p["duration_s"], 10)
        assert p["verdict"] == evaluate(p["measured_value"], spec.limit_min, spec.limit_max)
        assert (p["test_name"], p["unit"], p["limit_min"], p["limit_max"]) == (
            spec.test_name, spec.unit, spec.limit_min, spec.limit_max,
        )
        if failure_rate == 0.0:
            assert p["verdict"] == "PASS"
        if failure_rate == 1.0:
            assert p["verdict"] == "FAIL"
        if spec is SLEEP_CURRENT and p["verdict"] == "FAIL":
            assert p["measured_value"] == 0.0 or 0.41 <= p["measured_value"] <= 0.59
        if spec is WAKEUP_TIME and p["verdict"] == "FAIL":
            assert 150.01 <= p["measured_value"] <= 187.50


def test_device_ids_stay_within_the_configured_count():
    ids = {r.payload["device_id"] for r in seeded_run(3, count=300, device_count=3)}

    assert ids == {"ECU-001", "ECU-002", "ECU-003"}


def test_highest_device_count_keeps_three_digits():
    ids = {r.payload["device_id"] for r in seeded_run(5, count=500, device_count=999)}

    assert all(re.fullmatch(r"ECU-[0-9]{3}", device_id) for device_id in ids)
