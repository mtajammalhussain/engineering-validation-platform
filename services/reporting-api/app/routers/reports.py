"""Endpoints under /api/v1/reports (docs/APP_SPEC.md §7). Read-only."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from app.calculations import calculate_pass_rate_percent, resolve_report_window
from app.dependencies import get_db, get_request_time
from app.queries import fetch_summary_counts
from app.schemas import SummaryReport

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


@router.get("/summary", response_model=SummaryReport)
def get_summary(
    from_: AwareDatetime | None = Query(
        default=None, alias="from", description="started_at >= from (tz-aware ISO 8601)"
    ),
    to: AwareDatetime | None = Query(
        default=None, description="started_at < to (tz-aware ISO 8601)"
    ),
    device_id: str | None = Query(default=None, description="Exact match"),
    test_name: str | None = Query(default=None, description="Exact match"),
    now: datetime = Depends(get_request_time),
    db: Session = Depends(get_db),
) -> SummaryReport:
    """Total, passed, failed and pass rate in the effective window (default: last 7 days)."""
    # Only the window resolution can reject user input here; any other ValueError is a
    # bug and must stay a server error, so the try block contains nothing else.
    try:
        start, end = resolve_report_window(from_, to, now)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    counts = fetch_summary_counts(db, start, end, device_id=device_id, test_name=test_name)
    return SummaryReport(
        from_=start,
        to=end,
        total=counts.total,
        passed=counts.passed,
        failed=counts.failed,
        pass_rate_percent=calculate_pass_rate_percent(counts.passed, counts.total),
    )
