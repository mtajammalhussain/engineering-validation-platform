"""Response models of the reports (docs/APP_SPEC.md §7.3).

``from`` is a Python keyword, so the field is called ``from_`` in code and ``from`` in JSON
(alias). ``from``/``to`` are the effective window in UTC; Pydantic writes UTC datetimes with
a ``Z`` suffix.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReportWindow(BaseModel):
    """The effective report window, shared by every report response."""

    # Allow building the model with the Python name from_=...; JSON output uses the alias.
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from")
    to: datetime


class SummaryReport(ReportWindow):
    """``GET /api/v1/reports/summary``."""

    total: int
    passed: int
    failed: int
    pass_rate_percent: float | None


class ByDeviceItem(BaseModel):
    device_id: str
    total: int
    passed: int
    failed: int
    pass_rate_percent: float | None


class ByTestItem(BaseModel):
    test_name: str
    total: int
    passed: int
    failed: int
    pass_rate_percent: float | None


class TimeSeriesItem(BaseModel):
    bucket_start: datetime
    total: int
    passed: int
    failed: int


class ByDeviceReport(ReportWindow):
    """``GET /api/v1/reports/by-device``: sorted by failed desc, then device_id asc."""

    items: list[ByDeviceItem]


class ByTestReport(ReportWindow):
    """``GET /api/v1/reports/by-test``: sorted by failed desc, then test_name asc."""

    items: list[ByTestItem]


class TimeSeriesReport(ReportWindow):
    """``GET /api/v1/reports/timeseries``: sorted by bucket_start asc."""

    items: list[TimeSeriesItem]
