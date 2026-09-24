"""Endpoints under /api/v1/results (docs/APP_SPEC.md §5.1–§5.3, §6)."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import AwareDatetime
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.dependencies import get_db, require_api_key
from app.models import Result
from app.schemas import ResultCreate, ResultList, ResultRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/results", tags=["results"])

# Largest value of a PostgreSQL bigint. Bigger IDs/offsets are rejected with 422
# instead of causing a database error.
MAX_BIGINT = 2**63 - 1


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ResultRead,
    dependencies=[Depends(require_api_key)],
)
def create_result(payload: ResultCreate, db: Session = Depends(get_db)) -> Result:
    """Store one result. The verdict is stored as sent; the API does not judge it."""
    # exclude_unset=True: fields the client did not send (e.g. "source") are left out of
    # the INSERT, so PostgreSQL applies its server-side default.
    row = Result(**payload.model_dump(exclude_unset=True))
    db.add(row)
    try:
        db.commit()
        db.refresh(row)  # load database-generated values: id, received_at, source
    except SQLAlchemyError:
        db.rollback()
        raise
    logger.info(
        "Result stored: id=%s %s %s %s",
        row.id, row.device_id, row.test_name, row.verdict,
        extra={"device_id": row.device_id, "test_name": row.test_name, "verdict": row.verdict},
    )
    return row


def build_filters(
    device_id: str | None,
    test_name: str | None,
    verdict: str | None,
    from_: AwareDatetime | None,
    to: AwareDatetime | None,
) -> list[ColumnElement[bool]]:
    """Translate the query parameters into SQL WHERE conditions (only for given ones)."""
    conditions: list[ColumnElement[bool]] = []
    if device_id is not None:
        conditions.append(Result.device_id == device_id)
    if test_name is not None:
        conditions.append(Result.test_name == test_name)
    if verdict is not None:
        conditions.append(Result.verdict == verdict)
    if from_ is not None:
        conditions.append(Result.started_at >= from_)
    if to is not None:
        conditions.append(Result.started_at < to)
    return conditions


@router.get("", response_model=ResultList)
def list_results(
    device_id: str | None = None,
    test_name: str | None = None,
    verdict: Literal["PASS", "FAIL"] | None = None,
    from_: AwareDatetime | None = Query(
        default=None, alias="from", description="started_at >= from (tz-aware ISO 8601)"
    ),
    to: AwareDatetime | None = Query(
        default=None, description="started_at < to (tz-aware ISO 8601)"
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=MAX_BIGINT),
    db: Session = Depends(get_db),
) -> ResultList:
    """List results, newest ``started_at`` first, with optional filters and pagination."""
    conditions = build_filters(device_id, test_name, verdict, from_, to)

    total = db.scalar(select(func.count()).select_from(Result).where(*conditions))
    rows = db.scalars(
        select(Result)
        .where(*conditions)
        # id as tie-breaker: rows with the same started_at keep a stable order across pages
        .order_by(Result.started_at.desc(), Result.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return ResultList(
        items=[ResultRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{result_id}", response_model=ResultRead)
def get_result(
    result_id: int = Path(ge=1, le=MAX_BIGINT),
    db: Session = Depends(get_db),
) -> Result:
    row = db.get(Result, result_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Result not found")
    return row
