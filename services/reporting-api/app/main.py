"""Reporting API application (docs/APP_SPEC.md §7).

Start with the app factory, so nothing runs at import time:

    uvicorn --factory app.main:create_app --host 0.0.0.0 --port "$PORT"
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.config import Settings, load_settings
from app.db import create_db_engine, create_session_factory
from app.logging_config import configure_logging
from app.metrics import setup_metrics
from app.routers import health, reports

logger = logging.getLogger(__name__)

# Errors meaning "the database cannot be reached or used right now" -> 503:
#   OperationalError  connection refused/lost, authentication failed, server shutting down
#   InterfaceError    the driver's connection is already closed or broken
#   PoolTimeoutError  no free connection in the pool within the timeout
# Every other SQLAlchemyError (e.g. ProgrammingError, DataError) points to a bug or an
# unexpected state -> 500.
# Starlette picks the handler of the most specific matching class, so these three win over
# the general SQLAlchemyError handler. Details are logged, never sent to the client.
DATABASE_UNAVAILABLE_ERRORS = (OperationalError, InterfaceError, PoolTimeoutError)


async def handle_database_unavailable(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.error(
        "Database unavailable on %s %s: %s",
        request.method, request.url.path, type(exc).__name__, exc_info=exc,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database unavailable"},
    )


async def handle_unexpected_database_error(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    logger.error(
        "Unexpected database error on %s %s: %s",
        request.method, request.url.path, type(exc).__name__, exc_info=exc,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Without ``settings``, they are read from the environment now (fail fast: invalid config
    exits with code 1 before the server starts). Tests pass their own ``settings``.
    """
    if settings is None:
        settings = load_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: prepare the connection pool (connects lazily on the first query).
        engine = create_db_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        logger.info("Reporting API started (env=%s)", settings.app_env)
        try:
            yield
        finally:
            # Shutdown: close all pooled database connections.
            engine.dispose()
            logger.info("Reporting API stopped")

    app = FastAPI(title="EVP Reporting API", lifespan=lifespan)
    app.state.settings = settings
    app.state.metrics_registry = setup_metrics(app)  # also adds GET /metrics
    for error_class in DATABASE_UNAVAILABLE_ERRORS:
        app.add_exception_handler(error_class, handle_database_unavailable)
    app.add_exception_handler(SQLAlchemyError, handle_unexpected_database_error)
    app.include_router(health.router)
    app.include_router(reports.router)
    return app
