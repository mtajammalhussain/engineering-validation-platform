"""Synthetic result generation (docs/APP_SPEC.md §8.3.1).

Pure and deterministic: the random generator and the current time are passed in, nothing
here reads the environment, the clock or the network. The same seeded ``random.Random``
therefore always produces the same results (apart from ``started_at``, which is real time).

Measured values are handled as whole numbers of hundredths ("k"), like a bench with a
resolution of 0.01 of the unit: ``k = 40`` means 0.40 mA. All range arithmetic is done on
these integers, because binary floats cannot hold most decimals exactly
(``0.07 * 100 == 7.000000000000001``).
"""

import random
from dataclasses import dataclass
from datetime import datetime, timezone

from simulator.catalog import CATALOG, SLEEP_CURRENT, TestSpec
from simulator.verdict import evaluate

TEMPERATURES_C = (-40, -30, -20, 23, 85, 105)
BOUNDARY_RATE = 0.02  # share of inside results placed exactly on a limit
COLD_FAILURE_FACTOR = 1.5  # Sleep Current below 0 °C (synthetic fault model, spec §8.3.1)
DURATION_TENTHS = (50, 600)  # duration_s 5.0 ... 60.0 in steps of 0.1
SOURCE = "simulator"


@dataclass(frozen=True)
class GeneratedResult:
    """One generated result.

    ``payload`` is exactly the body for ``POST /api/v1/results`` (spec §5.2). ``spec`` keeps
    the catalog entry for the output line (e.g. its ``quantity``), which is not sent.
    """

    spec: TestSpec
    payload: dict[str, object]


def to_hundredths(limit: float) -> int:
    """Convert a catalog limit to whole hundredths, e.g. 0.40 -> 40.

    ``round`` (not ``int``/``math.ceil``) removes float noise: 0.07 * 100 is
    7.000000000000001 (``math.ceil`` would give 8) and 0.29 * 100 is 28.999999999999996
    (``int`` would give 28).
    """
    return round(limit * 100)


def effective_failure_probability(spec: TestSpec, temperature_c: int, failure_rate: float) -> float:
    """SIM_FAILURE_RATE, raised ×1.5 (max 1.0) for Sleep Current below 0 °C (spec §8.3.1)."""
    if spec.key == SLEEP_CURRENT.key and temperature_c < 0:
        return min(1.0, failure_rate * COLD_FAILURE_FACTOR)
    return failure_rate


def generation_range(spec: TestSpec) -> tuple[int, int]:
    """Inside range ``(k_min, k_max)`` in hundredths, both ends inclusive.

    A maximum-only test uses ``max / 2`` as a minimum for generation only (never sent), rounded
    up to the next hundredth. Minimum-only tests are not in the catalog and not supported.
    """
    if spec.limit_max is None:
        raise ValueError(f"{spec.key}: generation needs a maximum limit")
    k_max = to_hundredths(spec.limit_max)
    if spec.limit_min is None:
        return -(-k_max // 2), k_max  # -(-a // b) is integer division rounded up
    return to_hundredths(spec.limit_min), k_max


def outside_ranges(spec: TestSpec) -> tuple[tuple[int, int] | None, tuple[int, int]]:
    """Failure ranges ``(below, above)`` in hundredths, both ends inclusive.

    ``span / 2`` is rounded inward to the 0.01 grid: ``span // 2`` gives the lower bound of
    "below" rounded up and the upper bound of "above" rounded down. ``below`` is ``None`` for a
    maximum-only test (below its generation minimum a value would still pass) and when the
    range would be empty because it cannot go below 0.
    """
    k_min, k_max = generation_range(spec)
    half_span = (k_max - k_min) // 2
    above = (k_max + 1, k_max + half_span)
    if spec.limit_min is None:
        return None, above
    below_low, below_high = max(0, k_min - half_span), k_min - 1
    below = (below_low, below_high) if below_low <= below_high else None
    return below, above


def generate_measurement_hundredths(rng: random.Random, spec: TestSpec, fail: bool) -> int:
    """Draw a measured value in hundredths: outside the limits if ``fail``, else inside."""
    k_min, k_max = generation_range(spec)
    if fail:
        below, above = outside_ranges(spec)
        low, high = above
        if spec.limit_min is not None:
            use_below = rng.random() < 0.5  # drawn for every two-sided test
            if use_below and below is not None:
                low, high = below
        return rng.randint(low, high)
    if rng.random() < BOUNDARY_RATE:
        if spec.limit_min is None:
            return k_max  # the only real limit (not the generation minimum)
        return rng.choice((k_min, k_max))
    return rng.randint(k_min, k_max)


def generate_result(
    rng: random.Random, now: datetime, *, device_count: int, failure_rate: float
) -> GeneratedResult:
    """Generate one result with its verdict.

    Random numbers are drawn in this fixed order (spec §8.3.1), so a seed gives a stable
    sequence: test, device, temperature, failure decision, measured value, duration.
    ``now`` must be timezone-aware; it is sent as UTC and uses no random numbers.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    started_at = now.astimezone(timezone.utc).isoformat()

    spec = rng.choice(CATALOG)
    device_id = f"ECU-{rng.randint(1, device_count):03}"
    temperature_c = rng.choice(TEMPERATURES_C)
    fail = rng.random() < effective_failure_probability(spec, temperature_c, failure_rate)
    measured_value = generate_measurement_hundredths(rng, spec, fail) / 100
    duration_s = rng.randint(*DURATION_TENTHS) / 10

    payload = {
        "device_id": device_id,
        "test_name": spec.test_name,
        "temperature_c": temperature_c,
        "measured_value": measured_value,
        "unit": spec.unit,
        "limit_min": spec.limit_min,
        "limit_max": spec.limit_max,
        # Decided by evaluate() from the value actually sent, not from the "fail" intention.
        "verdict": evaluate(measured_value, spec.limit_min, spec.limit_max),
        "started_at": started_at,
        "duration_s": duration_s,
        "source": SOURCE,
    }
    return GeneratedResult(spec=spec, payload=payload)
