"""FastAPI dependencies: objects that endpoints receive per request via ``Depends(...)``.

Settings and the session factory are created once at startup (app.main) and stored on
``app.state``; these functions hand them to the endpoints.
"""

import logging
import secrets
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.config import Settings

logger = logging.getLogger(__name__)

# auto_error=False: a missing header gives None, so we can answer with our own 401.
# Declaring it as APIKeyHeader also adds an "Authorize" button to Swagger UI.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    """One database session per request, always closed afterwards (also on errors)."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def require_api_key(
    api_key: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject the request with 401 unless the X-API-Key header matches RESULTS_API_KEY.

    ``secrets.compare_digest`` takes the same time whether the first or the last character
    differs, so response timing reveals nothing about the key. Both sides are compared as
    UTF-8 bytes, because the str version fails on non-ASCII input. The key is never logged.
    """
    expected = settings.results_api_key.get_secret_value().encode("utf-8")
    if api_key is None or not secrets.compare_digest(api_key.encode("utf-8"), expected):
        reason = "missing" if api_key is None else "invalid"
        logger.warning("Rejected request: %s API key", reason)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key"
        )
