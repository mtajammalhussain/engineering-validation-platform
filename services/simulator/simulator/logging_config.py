"""Logging to stdout in ``text`` or ``json`` format (docs/APP_SPEC.md §2, §9)."""

import json
import logging
import sys
from datetime import datetime, timezone

from simulator.config import Settings

SERVICE_NAME = "simulator"

# Logger for the human-readable result, REJECTED, UNDELIVERED and summary lines (spec §8.4).
RESULTS_LOGGER = "simulator.results"

# Loggers of the HTTP client library. Their INFO lines ("HTTP Request: POST ...") would add
# one extra line per result, so they only log from WARNING (or a stricter global level) up.
_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")

_TEXT_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

# Attributes every LogRecord has. Anything else on a record was passed via ``extra=``
# and is written as a separate structured field in JSON output.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


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
            if key not in _STANDARD_ATTRS:
                entry[key] = value
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        # ensure_ascii=False: °, –, ≤, ≥ are written as they are, not as \u escapes.
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """Send all log output to stdout, in the format and level from the settings.

    Safe to call more than once: handlers are replaced, never added twice.
    """
    if settings.log_format == "json":
        formatter: logging.Formatter = JsonFormatter(service=SERVICE_NAME, env=settings.app_env)
        results_formatter = formatter
    else:
        formatter = logging.Formatter(_TEXT_FORMAT)
        # Result lines are printed bare, exactly as in spec §8.4 (no time/level/logger prefix).
        results_formatter = logging.Formatter("%(message)s")

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_stdout_handler(formatter))
    root.setLevel(settings.log_level)

    # The results logger has its own handler and does not pass its records on to the root
    # logger; otherwise each line would appear twice (once bare, once with prefix).
    # Its level stays unset, so the global LOG_LEVEL still applies to it.
    results = logging.getLogger(RESULTS_LOGGER)
    results.handlers.clear()
    results.addHandler(_stdout_handler(results_formatter))
    results.propagate = False
    results.setLevel(logging.NOTSET)

    # WARNING or the global level, whichever is stricter (e.g. ERROR stays ERROR).
    http_level = max(logging.WARNING, root.level)
    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(http_level)


def _stdout_handler(formatter: logging.Formatter) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    return handler
