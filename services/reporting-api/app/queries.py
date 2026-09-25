"""SQL for the reports (docs/APP_SPEC.md §7): SELECT statements only.

All counting happens in PostgreSQL (``COUNT``, ``FILTER``). Python receives exactly one row
of already-aggregated numbers, never individual test results. Database errors are not
caught here; the application's global handlers turn them into 503/500.
"""

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from app.models import test_results


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
    return select(
        func.count().label("total"),
        func.count().filter(test_results.c.verdict == "PASS").label("passed"),
        func.count().filter(test_results.c.verdict == "FAIL").label("failed"),
    ).where(*build_filters(start, end, device_id, test_name))


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
