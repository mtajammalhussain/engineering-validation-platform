"""Verdict logic of the test bench (docs/APP_SPEC.md §8.2). A pure function, no I/O."""

import math

PASS = "PASS"
FAIL = "FAIL"


def evaluate(measured_value: float, limit_min: float | None, limit_max: float | None) -> str:
    """Return "PASS" if the value lies within the limits (inclusive), otherwise "FAIL".

    ``None`` means no limit on that side. The value is evaluated exactly as given (no rounding).
    Raises ``ValueError`` if both limits are missing, if the value or a given limit is not a
    finite number (NaN, ±inf), or if ``limit_min > limit_max``.
    """
    if limit_min is None and limit_max is None:
        raise ValueError("at least one limit is required")
    for name, number in (
        ("measured_value", measured_value), ("limit_min", limit_min), ("limit_max", limit_max)
    ):
        if number is not None and not math.isfinite(number):
            raise ValueError(f"{name} must be a finite number")
    if limit_min is not None and limit_max is not None and limit_min > limit_max:
        raise ValueError("limit_min must not be greater than limit_max")

    if limit_min is not None and measured_value < limit_min:
        return FAIL
    if limit_max is not None and measured_value > limit_max:
        return FAIL
    return PASS
