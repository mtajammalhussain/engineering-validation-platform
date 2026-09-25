"""Pure reporting calculations (docs/APP_SPEC.md §7.1, §7.2).

No database, no web framework, no clock: every input is passed in, so results depend only
on the arguments. Errors are plain ``ValueError``; turning them into HTTP 422 is the job of
the endpoint layer.
"""

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

# Default report window: a rolling duration, not aligned to calendar days.
DEFAULT_REPORT_WINDOW = timedelta(hours=7 * 24)

_ONE_DECIMAL = Decimal("0.1")


def calculate_pass_rate_percent(passed: int, total: int) -> float | None:
    """``passed / total * 100`` rounded to one decimal, half-up; ``None`` if ``total == 0``.

    The division and rounding use ``Decimal`` (exact decimal arithmetic). With binary
    floats, Python's ``round(6.25, 1)`` gives 6.2 (round-half-to-even); the spec requires
    half-up, so 1/16 = 6.25 % must become 6.3. Only the final result is converted to float.
    """
    if total == 0:
        return None
    percent = Decimal(passed) * 100 / Decimal(total)
    return float(percent.quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP))


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def resolve_report_window(
    from_value: datetime | None,
    to_value: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Return the effective ``(from, to)`` window in UTC (``from <= started_at < to``).

    neither bound -> [now - 7 days, now)
    only from     -> [from, now)
    only to       -> [to - 7 days, to)
    both          -> [from, to)

    ``now`` is passed in by the caller (captured once per request), so both bounds derive
    from the same instant. All datetimes must be timezone-aware. Everything is converted to
    UTC before the 7-day subtraction, so it is always exactly 168 hours, also across
    daylight-saving changes. Raises ``ValueError`` if a datetime is naive or the effective
    ``from >= to``.
    """
    _require_aware("now", now)
    if from_value is not None:
        _require_aware("from", from_value)
    if to_value is not None:
        _require_aware("to", to_value)

    end = (to_value if to_value is not None else now).astimezone(timezone.utc)
    if from_value is not None:
        start = from_value.astimezone(timezone.utc)
    else:
        start = end - DEFAULT_REPORT_WINDOW

    if start >= end:
        raise ValueError("from must be earlier than to")
    return start, end
