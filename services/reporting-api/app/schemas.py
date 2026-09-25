"""Response models of the reports (docs/APP_SPEC.md §7.3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SummaryReport(BaseModel):
    """``GET /api/v1/reports/summary``.

    ``from`` is a Python keyword, so the field is called ``from_`` in code and ``from`` in
    JSON (alias). ``from``/``to`` are the effective window in UTC; Pydantic writes UTC
    datetimes with a ``Z`` suffix.
    """

    # Allow building the model with the Python name from_=...; JSON output uses the alias.
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from")
    to: datetime
    total: int
    passed: int
    failed: int
    pass_rate_percent: float | None
