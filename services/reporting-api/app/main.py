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

# PostgreSQL SQLSTATE "query_canceled": the statement was cancelled, e.g. because it ran
# longer than statement_timeout (app.db) or by pg_cancel_backend().
QUERY_CANCELED_SQLSTATE = "57014"


def is_query_canceled(exc: SQLAlchemyError) -> bool:
    """True if PostgreSQL cancelled the statement (SQLSTATE 57014).

    psycopg2 reports this as an OperationalError, which SQLAlchemy wraps; the driver's
    original exception is ``exc.orig`` and carries the SQLSTATE in ``pgcode``. The
    isinstance check matters because the pool TimeoutError, handled by the same handler,
    has no ``orig``.
    """
    return (
        isinstance(exc, OperationalError)
        and getattr(exc.orig, "pgcode", None) == QUERY_CANCELED_SQLSTATE
    )


async def handle_database_unavailable(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    if is_query_canceled(exc):
        # One warning naming SQLSTATE and route only (docs/APP_SPEC.md §7.3): no SQL,
        # parameters, connection details, exception text or traceback. "e.g." because a
        # cancellation can also have other causes than the statement timeout.
        logger.warning(
            "Report query cancelled (SQLSTATE 57014, e.g. statement_timeout) on %s %s",
            request.method, request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Database query timed out"},
        )
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


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Any other exception -> generic 500 JSON (docs/APP_SPEC.md §7.3).

    Starlette sends this response and then re-raises ``exc``; uvicorn then logs the
    traceback ("Exception in ASGI application"). No ``exc_info`` here, so the traceback is
    logged only once. This line adds the route and the exception class, not its text.
    """
    logger.error(
        "Unexpected error on %s %s: %s",
        request.method, request.url.path, type(exc).__name__,
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
    # FastAPI hands the Exception handler to Starlette's outermost ServerErrorMiddleware,
    # so it only sees exceptions that none of the handlers above has handled.
    app.add_exception_handler(Exception, handle_unexpected_error)
    app.include_router(health.router)
    app.include_router(reports.router)
    return app
