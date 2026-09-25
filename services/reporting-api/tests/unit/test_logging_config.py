import json
import logging

import pytest

from app.db import build_database_url
from app.logging_config import SERVICE_NAME, JsonFormatter, configure_logging

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
SECRET = "super-secret-password"


@pytest.fixture(autouse=True)
def restore_loggers():
    """Put the root and uvicorn loggers back as they were, so tests do not affect each other."""
    loggers = [logging.getLogger(name) for name in (None, *UVICORN_LOGGERS)]
    saved = [(lg, lg.handlers[:], lg.level, lg.propagate) for lg in loggers]
    yield
    for lg, handlers, level, propagate in saved:
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


def make_record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, (), None)
    record.__dict__.update(extra)
    return record


def test_service_name_is_reporting_api():
    assert SERVICE_NAME == "reporting-api"


def test_json_formatter_writes_required_fields():
    formatter = JsonFormatter(service=SERVICE_NAME, env="dev")

    entry = json.loads(formatter.format(make_record("Report served")))

    assert entry["level"] == "INFO"
    assert entry["service"] == "reporting-api"
    assert entry["env"] == "dev"
    assert entry["msg"] == "Report served"
    assert entry["ts"].endswith("Z")


def test_json_formatter_keeps_extra_fields_separate():
    formatter = JsonFormatter(service=SERVICE_NAME, env="prod")

    line = formatter.format(make_record("Temperature: -30°C", device_id="ECU-001"))
    entry = json.loads(line)

    assert entry["device_id"] == "ECU-001"
    assert "°C" in line  # readable, not escaped as °


def test_configure_logging_json_writes_one_json_line_to_stdout(capsys, make_settings):
    configure_logging(make_settings(log_format="json", app_env="prod"))

    logging.getLogger("any.module").info("hello")

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["msg"] == "hello"
    assert entry["service"] == "reporting-api"
    assert entry["env"] == "prod"
    assert captured.err == ""


def test_configure_logging_text_uses_plain_lines_on_stdout(capsys, make_settings):
    configure_logging(make_settings(log_format="text"))

    logging.getLogger("any.module").info("hello")

    captured = capsys.readouterr()
    assert "INFO | any.module | hello" in captured.out
    assert captured.err == ""


def test_configure_logging_respects_level(capsys, make_settings):
    configure_logging(make_settings(log_level="WARNING"))

    logging.getLogger("any.module").info("hidden")
    logging.getLogger("any.module").warning("shown")

    output = capsys.readouterr().out
    assert "hidden" not in output
    assert "shown" in output


def test_uvicorn_loggers_use_our_format(make_settings):
    logging.getLogger("uvicorn.access").addHandler(logging.StreamHandler())

    configure_logging(make_settings())

    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is True


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_logging_settings_and_db_url_never_shows_the_password(capsys, make_settings, log_format):
    settings = make_settings(db_password=SECRET, log_format=log_format)
    configure_logging(settings)

    logger = logging.getLogger("any.module")
    logger.info("settings: %s", settings)
    logger.info("settings repr: %r", settings)
    logger.info("database: %s", build_database_url(settings))
    logger.info("with extra", extra={"db_password": settings.db_password})

    output = capsys.readouterr().out
    assert "database:" in output
    assert SECRET not in output
