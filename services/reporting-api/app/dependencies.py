"""FastAPI dependencies: objects that endpoints receive per request via ``Depends(...)``.

Settings are stored on ``app.state`` by ``create_app``; the session factory by the lifespan
(app.main). These functions hand them to the endpoints. There is no API key: the
Reporting API only reads (docs/APP_SPEC.md §6, §7).
"""

from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    """One database session per request, always closed afterwards (also on errors)."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_request_time() -> datetime:
    """The current time in UTC, read once per request.

    Endpoints receive it as a dependency instead of calling ``datetime.now()`` themselves,
    so both bounds of a default report window derive from the same instant, and tests can
    replace it via ``app.dependency_overrides``.
    """
    return datetime.now(timezone.utc)
