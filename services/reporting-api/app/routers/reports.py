"""Endpoints under /api/v1/reports (docs/APP_SPEC.md §7). Read-only."""

from datetime import datetime, timezone
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from app.calculations import calculate_pass_rate_percent, resolve_report_window
from app.dependencies import get_db, get_request_time
from app.queries import (
    Interval,
    build_grouped_statement,
    build_timeseries_statement,
    fetch_rows,
    fetch_summary_counts,
)
from app.schemas import (
    ByDeviceItem,
    ByDeviceReport,
    ByTestItem,
    ByTestReport,
    SummaryReport,
    TimeSeriesItem,
    TimeSeriesReport,
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


class ReportParams(NamedTuple):
    """The common report parameters, with the window already resolved to UTC."""

    start: datetime
    end: datetime
    device_id: str | None
    test_name: str | None


def report_params(
    from_: AwareDatetime | None = Query(
        default=None, alias="from", description="started_at >= from (tz-aware ISO 8601)"
    ),
    to: AwareDatetime | None = Query(
        default=None, description="started_at < to (tz-aware ISO 8601)"
    ),
    device_id: str | None = Query(default=None, description="Exact match"),
    test_name: str | None = Query(default=None, description="Exact match"),
    now: datetime = Depends(get_request_time),
) -> ReportParams:
    """Common query parameters of every report (dependency, docs/APP_SPEC.md §7.1)."""
    # Only the window resolution can reject user input here; any other ValueError is a
    # bug and must stay a server error, so the try block contains nothing else.
    try:
        start, end = resolve_report_window(from_, to, now)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ReportParams(start, end, device_id, test_name)


@router.get("/summary", response_model=SummaryReport)
def get_summary(
    params: ReportParams = Depends(report_params),
    db: Session = Depends(get_db),
) -> SummaryReport:
    """Total, passed, failed and pass rate in the effective window (default: last 7 days)."""
    counts = fetch_summary_counts(
        db, params.start, params.end, device_id=params.device_id, test_name=params.test_name
    )
    return SummaryReport(
        from_=params.start,
        to=params.end,
        total=counts.total,
        passed=counts.passed,
        failed=counts.failed,
        pass_rate_percent=calculate_pass_rate_percent(counts.passed, counts.total),
    )


@router.get("/by-device", response_model=ByDeviceReport)
def get_by_device(
    params: ReportParams = Depends(report_params),
    db: Session = Depends(get_db),
) -> ByDeviceReport:
    """Counts per device, most failures first (ties: device_id ascending)."""
    rows = fetch_rows(db, build_grouped_statement("device_id", *params))
    # One row per device, already counted by PostgreSQL; Python only builds the items.
    items = [
        ByDeviceItem(
            device_id=row.device_id,
            total=row.total,
            passed=row.passed,
            failed=row.failed,
            pass_rate_percent=calculate_pass_rate_percent(row.passed, row.total),
        )
        for row in rows
    ]
    return ByDeviceReport(from_=params.start, to=params.end, items=items)


@router.get("/by-test", response_model=ByTestReport)
def get_by_test(
    params: ReportParams = Depends(report_params),
    db: Session = Depends(get_db),
) -> ByTestReport:
    """Counts per test, most failures first (ties: test_name ascending)."""
    rows = fetch_rows(db, build_grouped_statement("test_name", *params))
    items = [
        ByTestItem(
            test_name=row.test_name,
            total=row.total,
            passed=row.passed,
            failed=row.failed,
            pass_rate_percent=calculate_pass_rate_percent(row.passed, row.total),
        )
        for row in rows
    ]
    return ByTestReport(from_=params.start, to=params.end, items=items)


@router.get("/timeseries", response_model=TimeSeriesReport)
def get_timeseries(
    interval: Interval = Query(description="Bucket size: hour or day (UTC)"),
    params: ReportParams = Depends(report_params),
    db: Session = Depends(get_db),
) -> TimeSeriesReport:
    """Counts per UTC hour or day; only buckets that contain results, oldest first."""
    rows = fetch_rows(db, build_timeseries_statement(interval, *params))
    items = [
        TimeSeriesItem(
            # timestamptz may come back in another offset; same instant, shown in UTC.
            bucket_start=row.bucket_start.astimezone(timezone.utc),
            total=row.total,
            passed=row.passed,
            failed=row.failed,
        )
        for row in rows
    ]
    return TimeSeriesReport(from_=params.start, to=params.end, items=items)
