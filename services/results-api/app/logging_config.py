"""Logging to stdout in ``text`` or ``json`` format (docs/APP_SPEC.md §2, §9)."""

import json
import logging
import sys
from datetime import datetime, timezone

from app.config import MigrationSettings

SERVICE_NAME = "results-api"

# Attributes every LogRecord has. Anything else on a record was passed via ``extra=``
# and is written as a separate structured field in JSON output.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}

# uvicorn's coloured copy of some messages (``extra={"color_message": ...}``). It is only for
# uvicorn's own console formatter, not a structured field, so JSON output leaves it out.
_UVICORN_DISPLAY_ATTRS = {"color_message"}

# Loggers of the uvicorn web server. uvicorn gives them their own handlers and levels before
# our app factory runs; we remove both, so that uvicorn's lines use our format and LOG_LEVEL
# (docs/APP_SPEC.md §9). "uvicorn.asgi" is only used by uvicorn's ASGI message logger.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")

# Probe and scrape paths whose access lines are not written when the status is below 400
# (docs/APP_SPEC.md §9). Exact paths; a query string is ignored.
QUIET_ACCESS_PATHS = frozenset({"/health", "/ready", "/metrics"})

# The access-line format of uvicorn 0.53 (uvicorn/protocols/http/h11_impl.py and
# httptools_impl.py), logged with args (client, method, path_with_query, http_version, status).
UVICORN_ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def is_quiet_probe_access(record: logging.LogRecord) -> bool:
    """True only for a recognised uvicorn access line for /health, /ready or /metrics
    with a status below 400.

    Anything that is not recognised returns False and is therefore written ("fail open").
    Every value is type-checked before it is used, so this function cannot raise.
    """
    if record.name != "uvicorn.access" or record.msg != UVICORN_ACCESS_FORMAT:
        return False
    args = record.args
    if not isinstance(args, tuple) or len(args) != 5:
        return False
    path, status = args[2], args[4]
    if not isinstance(path, str) or not isinstance(status, int) or isinstance(status, bool):
        return False
    return path.split("?", 1)[0] in QUIET_ACCESS_PATHS and status < 400


class ProbeAccessFilter(logging.Filter):
    """Drops successful probe/scrape access lines; every other record passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not is_quiet_probe_access(record)


class JsonFormatter(logging.Formatter):
    """Formats each log record as one JSON object on one line."""

    def __init__(self, service: str, env: str) -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "level": record.levelname,
            "service": self.service,
            "env": self.env,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in _UVICORN_DISPLAY_ATTRS:
                entry[key] = value
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(settings: MigrationSettings) -> None:
    """Send all log output to stdout, in the format and level from the settings."""
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter(service=SERVICE_NAME, env=settings.app_env))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
    # On the handler (new on every call), not on a logger, so filters never pile up.
    handler.addFilter(ProbeAccessFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.setLevel(logging.NOTSET)  # follow the root level, i.e. LOG_LEVEL
        uvicorn_logger.propagate = True
