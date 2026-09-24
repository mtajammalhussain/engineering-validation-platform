"""Liveness and readiness endpoints (docs/APP_SPEC.md §2.5). Not under /api/v1."""

import logging

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["operations"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is running. Never touches the database."""
    return {"status": "ok"}


@router.get("/ready", responses={503: {"description": "Database not reachable"}})
def ready(db: Session = Depends(get_db)) -> JSONResponse:
    """Readiness: 200 if PostgreSQL answers ``SELECT 1``, otherwise 503."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # Probes run every few seconds: one short warning line, no traceback, no details
        # for the client.
        logger.warning("Readiness check failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "unavailable"}
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})
