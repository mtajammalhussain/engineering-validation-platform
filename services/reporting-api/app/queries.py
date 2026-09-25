"""SQL for the reports (docs/APP_SPEC.md §7): SELECT statements only.

All counting happens in PostgreSQL (``COUNT``, ``FILTER``, ``GROUP BY``, ``date_trunc``).
Python receives one row per summary, group or time bucket, never individual test results.
Database errors are not caught here; the application's global handlers turn them into
503/500.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, NamedTuple

from sqlalchemy import ColumnElement, Label, Select, func, select
from sqlalchemy.orm import Session

from app.models import test_results

Interval = Literal["hour", "day"]

# Grouping keys chosen by application code, never taken from the request.
GROUP_COLUMNS = {
    "device_id": test_results.c.device_id,
    "test_name": test_results.c.test_name,
}

BUCKET_LABEL = "bucket_start"


class SummaryCounts(NamedTuple):
    total: int
    passed: int
    failed: int


def build_filters(
    start: datetime,
    end: datetime,
    device_id: str | None,
    test_name: str | None,
) -> list[ColumnElement[bool]]:
    """WHERE conditions: the report window always, the exact-match filters only if given.

    The window is ``start <= started_at < end`` (from inclusive, to exclusive).
    """
    conditions: list[ColumnElement[bool]] = [
        test_results.c.started_at >= start,
        test_results.c.started_at < end,
    ]
    if device_id is not None:
        conditions.append(test_results.c.device_id == device_id)
    if test_name is not None:
        conditions.append(test_results.c.test_name == test_name)
    return conditions


def count_columns() -> list[Label]:
    """total = count(*), passed/failed = count(*) FILTER (WHERE verdict = 'PASS'/'FAIL')."""
    return [
        func.count().label("total"),
        func.count().filter(test_results.c.verdict == "PASS").label("passed"),
        func.count().filter(test_results.c.verdict == "FAIL").label("failed"),
    ]


def build_summary_statement(
    start: datetime,
    end: datetime,
    device_id: str | None = None,
    test_name: str | None = None,
) -> Select:
    """One aggregate SELECT; always returns exactly one row (zeros if nothing matches).

    SELECT count(*) AS total,
           count(*) FILTER (WHERE verdict = 'PASS') AS passed,
           count(*) FILTER (WHERE verdict = 'FAIL') AS failed
    FROM test_results WHERE <filters>
    """
    return select(*count_columns()).where(*build_filters(start, end, device_id, test_name))


def fetch_summary_counts(
    db: Session,
    start: datetime,
    end: datetime,
    device_id: str | None = None,
    test_name: str | None = None,
) -> SummaryCounts:
    """Run the summary statement and return its single row of counts."""
    row = db.execute(build_summary_statement(start, end, device_id, test_name)).one()
    return SummaryCounts(total=row.total, passed=row.passed, failed=row.failed)


def build_grouped_statement(
    key: Literal["device_id", "test_name"],
    start: datetime,
    end: datetime,
    device_id: str | None = None,
    test_name: str | None = None,
) -> Select:
    """One row per device or per test, most failures first (docs/APP_SPEC.md §7.3).

    SELECT <key>, <counts> FROM test_results WHERE <filters>
    GROUP BY <key> ORDER BY failed DESC, <key> ASC

    The second sort key makes the order deterministic when failed counts are equal.
    Only groups with at least one matching result exist, so no rows means no groups.
    """
    column = GROUP_COLUMNS[key]
    counts = count_columns()
    failed = counts[2]
    return (
        select(column, *counts)
        .where(*build_filters(start, end, device_id, test_name))
        .group_by(column)
        .order_by(failed.desc(), column.asc())
    )


def build_timeseries_statement(
    interval: Interval,
    start: datetime,
    end: datetime,
    device_id: str | None = None,
    test_name: str | None = None,
) -> Select:
    """One row per hour or day bucket that contains results, oldest first.

    SELECT date_trunc(<interval>, started_at, 'UTC') AS bucket_start, <counts>
    FROM test_results WHERE <filters>
    GROUP BY bucket_start ORDER BY bucket_start ASC

    The three-argument ``date_trunc`` cuts in UTC, whatever the session time zone is.
    GROUP BY and ORDER BY refer to the output column *name* ``bucket_start``: PostgreSQL
    then groups by exactly the selected expression. Writing the expression a second time
    in GROUP BY could give it separate bound parameters, and PostgreSQL would reject the
    query ("must appear in the GROUP BY clause").
    Empty buckets are not generated; the first bucket may start before ``start``.
    """
    if interval not in ("hour", "day"):
        raise ValueError(f"unsupported interval: {interval!r}")
    bucket = func.date_trunc(interval, test_results.c.started_at, "UTC").label(BUCKET_LABEL)
    return (
        select(bucket, *count_columns())
        .where(*build_filters(start, end, device_id, test_name))
        .group_by(BUCKET_LABEL)
        .order_by(BUCKET_LABEL)
    )


def fetch_rows(db: Session, statement: Select) -> Sequence[Any]:
    """Run a grouped statement and return its aggregate rows (one per group/bucket)."""
    return db.execute(statement).all()
