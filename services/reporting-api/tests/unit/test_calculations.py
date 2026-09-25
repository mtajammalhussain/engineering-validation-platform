"""Pass rate and report window (docs/APP_SPEC.md §7.1, §7.2). Pure functions, no I/O."""

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from app.calculations import (
    DEFAULT_REPORT_WINDOW,
    calculate_pass_rate_percent,
    resolve_report_window,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
PLUS_2 = timezone(timedelta(hours=2))
MINUS_5 = timezone(timedelta(hours=-5))


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class NoOffset(tzinfo):
    """A tzinfo that does not know its UTC offset: tzinfo is set, but utcoffset() is None.

    Python treats such a datetime as naive, so it must be rejected like one.
    """

    def utcoffset(self, dt):
        return None


def no_offset(*args) -> datetime:
    return datetime(*args, tzinfo=NoOffset())


# --- pass rate ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "passed, total, expected",
    [
        (0, 5, 0.0),
        (5, 5, 100.0),
        (120, 120, 100.0),
        (1, 16, 6.3),      # 6.25 -> half-up
        (111, 120, 92.5),  # spec §11
        (2, 3, 66.7),      # 66.666...
        (1, 3, 33.3),      # 33.333...
        (1182, 1250, 94.6),  # spec §7 example summary
        (58, 64, 90.6),      # spec §7 example by-device (90.625)
    ],
)
def test_pass_rate_percent(passed, total, expected):
    result = calculate_pass_rate_percent(passed, total)

    assert result == expected
    assert type(result) is float


def test_pass_rate_is_none_when_total_is_zero():
    assert calculate_pass_rate_percent(0, 0) is None


def test_half_up_differs_from_pythons_builtin_round():
    # Built-in round() on a binary float rounds half to even: 6.25 -> 6.2.
    assert round(6.25, 1) == 6.2
    # The spec requires half-up, computed with Decimal: 1/16 = 6.25 % -> 6.3.
    assert calculate_pass_rate_percent(1, 16) == 6.3


# --- report window: the four cases ---------------------------------------------------------

def test_neither_bound_gives_last_seven_days():
    assert resolve_report_window(None, None, NOW) == (utc(2026, 9, 18, 10), NOW)


def test_only_from_ends_now():
    from_ = datetime(2026, 9, 20, 12, 0, tzinfo=PLUS_2)

    assert resolve_report_window(from_, None, NOW) == (utc(2026, 9, 20, 10), NOW)


def test_only_to_starts_seven_days_earlier():
    to = datetime(2026, 9, 23, 18, 0, tzinfo=PLUS_2)

    assert resolve_report_window(None, to, NOW) == (utc(2026, 9, 16, 16), utc(2026, 9, 23, 16))


def test_both_bounds_are_used_as_given():
    from_, to = utc(2026, 9, 1), utc(2026, 9, 2, 6, 30)

    assert resolve_report_window(from_, to, NOW) == (from_, to)


def test_both_bounds_may_lie_outside_the_default_window():
    from_, to = utc(2025, 1, 1), utc(2026, 12, 31)  # long ago and in the future

    assert resolve_report_window(from_, to, NOW) == (from_, to)


# --- report window: UTC normalisation --------------------------------------------------------

def test_positive_offset_is_normalised_to_utc():
    start, end = resolve_report_window(
        datetime(2026, 9, 20, 2, 0, tzinfo=PLUS_2),
        datetime(2026, 9, 21, 2, 0, tzinfo=PLUS_2),
        NOW,
    )

    assert (start, end) == (utc(2026, 9, 20, 0), utc(2026, 9, 21, 0))


def test_negative_offset_is_normalised_to_utc():
    start, end = resolve_report_window(
        datetime(2026, 9, 19, 20, 0, tzinfo=MINUS_5),
        datetime(2026, 9, 20, 20, 0, tzinfo=MINUS_5),
        NOW,
    )

    assert (start, end) == (utc(2026, 9, 20, 1), utc(2026, 9, 21, 1))


def test_non_utc_now_is_normalised_too():
    now_plus_2 = datetime(2026, 9, 25, 12, 0, tzinfo=PLUS_2)  # same instant as NOW

    assert resolve_report_window(None, None, now_plus_2) == (utc(2026, 9, 18, 10), NOW)


@pytest.mark.parametrize(
    "from_, to",
    [(None, None), (utc(2026, 9, 20), None), (None, utc(2026, 9, 23)),
     (datetime(2026, 9, 1, tzinfo=MINUS_5), datetime(2026, 9, 2, tzinfo=PLUS_2))],
    ids=["neither", "only-from", "only-to", "both"],
)
def test_returned_bounds_are_utc_aware(from_, to):
    for bound in resolve_report_window(from_, to, NOW):
        assert bound.tzinfo is UTC
        assert bound.utcoffset() == timedelta(0)


# --- report window: exactly seven rolling 24-hour periods ------------------------------------

def test_default_window_is_exactly_168_hours():
    assert DEFAULT_REPORT_WINDOW == timedelta(hours=168)

    start, end = resolve_report_window(None, None, NOW)

    assert end - start == timedelta(hours=168)


def test_default_window_is_not_aligned_to_calendar_days():
    now = utc(2026, 9, 25, 13, 47, 5, 123456)

    start, end = resolve_report_window(None, None, now)

    assert start == utc(2026, 9, 18, 13, 47, 5, 123456)
    assert end == now


def test_default_window_is_168_hours_across_a_daylight_saving_change():
    # Central Europe leaves summer time on 2026-10-25. Seven *calendar* days back in local
    # wall-clock time would be 169 hours; the report window stays exactly 168 hours.
    berlin = ZoneInfo("Europe/Berlin")
    to = datetime(2026, 10, 28, 12, 0, tzinfo=berlin)

    start, end = resolve_report_window(None, to, NOW)

    assert end - start == timedelta(hours=168)
    assert end == utc(2026, 10, 28, 11)
    assert start == utc(2026, 10, 21, 11)


# --- report window: rejected input ---------------------------------------------------------

def test_from_equal_to_is_rejected():
    with pytest.raises(ValueError):
        resolve_report_window(utc(2026, 9, 20), utc(2026, 9, 20), NOW)


def test_same_instant_in_different_offsets_is_rejected():
    with pytest.raises(ValueError):
        resolve_report_window(
            datetime(2026, 9, 20, 2, 0, tzinfo=PLUS_2), utc(2026, 9, 20, 0), NOW
        )


def test_from_after_to_is_rejected():
    with pytest.raises(ValueError):
        resolve_report_window(utc(2026, 9, 21), utc(2026, 9, 20), NOW)


def test_future_from_without_to_is_rejected():
    # Effective window would be [2026-09-26, now=2026-09-25): from >= to.
    with pytest.raises(ValueError):
        resolve_report_window(utc(2026, 9, 26), None, NOW)


def test_from_equal_to_now_without_to_is_rejected():
    with pytest.raises(ValueError):
        resolve_report_window(NOW, None, NOW)


@pytest.mark.parametrize(
    "from_, to, now, name",
    [
        (datetime(2026, 9, 20), None, NOW, "from"),
        (None, datetime(2026, 9, 23), NOW, "to"),
        (None, None, datetime(2026, 9, 25, 10), "now"),
        (utc(2026, 9, 20), utc(2026, 9, 21), datetime(2026, 9, 25, 10), "now"),
        # tzinfo is set but utcoffset() is None; every other argument is valid and aware.
        (no_offset(2026, 9, 20), utc(2026, 9, 21), NOW, "from"),
        (utc(2026, 9, 20), no_offset(2026, 9, 21), NOW, "to"),
        (utc(2026, 9, 20), utc(2026, 9, 21), no_offset(2026, 9, 25, 10), "now"),
    ],
    ids=["naive-from", "naive-to", "naive-now", "naive-now-with-both-bounds",
         "no-offset-from", "no-offset-to", "no-offset-now"],
)
def test_naive_datetimes_are_rejected(from_, to, now, name):
    # Full-message match, so "from must be earlier than to" cannot satisfy the "from" case.
    with pytest.raises(ValueError, match=f"^{name} must be timezone-aware$"):
        resolve_report_window(from_, to, now)
