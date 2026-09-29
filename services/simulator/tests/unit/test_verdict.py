import math

import pytest

from simulator.verdict import evaluate

NAN, INF = math.nan, math.inf


# Spec §11, cases 1-12 (boundary value analysis).
@pytest.mark.parametrize(
    "value, limit_min, limit_max, expected",
    [
        (0.40, 0.01, 0.40, "PASS"),    # 1 on upper limit
        (0.401, 0.01, 0.40, "FAIL"),   # 2 just above
        (0.01, 0.01, 0.40, "PASS"),    # 3 on lower limit
        (0.0099, 0.01, 0.40, "FAIL"),  # 4 just below
        (150, None, 150, "PASS"),      # 5 one-sided, on limit
        (150.1, None, 150, "FAIL"),    # 6 one-sided, above
        (7.0, 5.5, 6.5, "FAIL"),       # 7
    ],
)
def test_spec_boundary_cases(value, limit_min, limit_max, expected):
    assert evaluate(value, limit_min, limit_max) == expected


@pytest.mark.parametrize(
    "value, limit_min, limit_max",
    [
        (1.0, None, None),   # 8 no limit at all
        (NAN, 0.01, 0.40),   # 9
        (INF, 0.01, 0.40),   # 10
        (0.20, NAN, 0.40),   # 11 non-finite limit
        (0.20, 0.40, 0.01),  # 12 min > max
    ],
)
def test_spec_invalid_cases(value, limit_min, limit_max):
    with pytest.raises(ValueError):
        evaluate(value, limit_min, limit_max)


@pytest.mark.parametrize(
    "value, expected",
    [(9.49, "FAIL"), (9.5, "PASS"), (10.0, "PASS"), (10.5, "PASS"), (10.51, "FAIL")],
)
def test_two_sided(value, expected):
    assert evaluate(value, 9.5, 10.5) == expected


@pytest.mark.parametrize("value, expected", [(149.99, "PASS"), (150, "PASS"), (150.01, "FAIL")])
def test_maximum_only(value, expected):
    assert evaluate(value, None, 150) == expected


@pytest.mark.parametrize("value, expected", [(5.49, "FAIL"), (5.5, "PASS"), (5.51, "PASS")])
def test_minimum_only(value, expected):
    assert evaluate(value, 5.5, None) == expected


@pytest.mark.parametrize("value", [NAN, INF, -INF])
def test_non_finite_value_is_rejected(value):
    with pytest.raises(ValueError, match="measured_value"):
        evaluate(value, 0.01, 0.40)


@pytest.mark.parametrize("limit", [NAN, INF, -INF])
def test_non_finite_limit_min_is_rejected(limit):
    with pytest.raises(ValueError, match="limit_min"):
        evaluate(0.20, limit, 0.40)


@pytest.mark.parametrize("limit", [NAN, INF, -INF])
def test_non_finite_limit_max_is_rejected(limit):
    with pytest.raises(ValueError, match="limit_max"):
        evaluate(0.20, 0.01, limit)


def test_equal_limits_are_allowed():
    assert evaluate(5.0, 5.0, 5.0) == "PASS"


def test_value_is_not_rounded():
    # 0.4000001 would display as 0.40, but it is above the limit and must fail.
    assert evaluate(0.4000001, 0.01, 0.40) == "FAIL"
