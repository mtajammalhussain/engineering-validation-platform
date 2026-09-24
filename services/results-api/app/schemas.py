"""Request and response models of the Results API (docs/APP_SPEC.md §5.2, §5.3).

Only data-quality checks, no limit logic: the test bench decides the verdict.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

MAX_FUTURE = timedelta(minutes=5)


class ResultCreate(BaseModel):
    """Body of ``POST /api/v1/results``. Every rule below is from spec §5.2."""

    model_config = ConfigDict(extra="forbid")

    # [0-9] instead of \d: \d would also accept non-ASCII digits such as "ECU-١٢٣".
    device_id: str = Field(pattern=r"^ECU-[0-9]{3}$", examples=["ECU-003"])
    test_name: str = Field(min_length=1, max_length=100, examples=["Sleep Current"])
    # The range also rejects NaN and ±Infinity (they are not between -40 and 125).
    temperature_c: float = Field(ge=-40, le=125)
    measured_value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=10, examples=["mA"])
    limit_min: float | None = Field(default=None, allow_inf_nan=False)
    limit_max: float | None = Field(default=None, allow_inf_nan=False)
    verdict: Literal["PASS", "FAIL"]
    # AwareDatetime rejects timestamps without a timezone ("naive" timestamps).
    started_at: AwareDatetime
    duration_s: float = Field(gt=0, allow_inf_nan=False)
    # May be omitted: then the database default 'simulator' applies (no second default here).
    source: str | None = None

    @field_validator("started_at")
    @classmethod
    def started_at_not_in_future(cls, value: datetime) -> datetime:
        if value > datetime.now(timezone.utc) + MAX_FUTURE:
            raise ValueError("must not be more than 5 minutes in the future")
        return value

    @field_validator("source")
    @classmethod
    def source_not_null(cls, value: str | None) -> str:
        # Runs only when "source" is sent. Omitting it is fine; an explicit null is not,
        # because the database column is NOT NULL.
        if value is None:
            raise ValueError("may be omitted, but must not be null")
        return value


class ResultRead(BaseModel):
    """One stored result, as returned by the API (all columns of spec §4)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    test_name: str
    temperature_c: float
    measured_value: float
    unit: str
    limit_min: float | None
    limit_max: float | None
    verdict: Literal["PASS", "FAIL"]
    started_at: datetime
    duration_s: float
    received_at: datetime
    source: str


class ResultList(BaseModel):
    """Response of ``GET /api/v1/results`` (spec §5.3)."""

    items: list[ResultRead]
    total: int
    limit: int
    offset: int
