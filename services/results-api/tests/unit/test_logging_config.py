import json
import logging

import pytest

from app.config import Settings
from app.logging_config import JsonFormatter, configure_logging

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def make_settings(**overrides) -> Settings:
    """Build Settings directly (without environment variables) for logging tests."""
    values = {
        "port": 8001,
        "db_host": "localhost",
        "db_port": 5432,
        "db_name": "evp",
        "db_user": "evp_writer",
        "db_password": "secret",
        "results_api_key": "secret",
    }
    values.update(overrides)
    return Settings(**values)


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


@pytest.fixture(autouse=True)
def clean_logging_env(monkeypatch):
    """Remove logging-related variables from the shell, so only the test values count."""
    for name in ("APP_ENV", "LOG_LEVEL", "LOG_FORMAT"):
        monkeypatch.delenv(name, raising=False)


def make_record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, (), None)
    record.__dict__.update(extra)
    return record


def test_json_formatter_writes_required_fields():
    formatter = JsonFormatter(service="results-api", env="dev")

    line = formatter.format(make_record("Result stored"))
    entry = json.loads(line)

    assert entry["level"] == "INFO"
    assert entry["service"] == "results-api"
    assert entry["env"] == "dev"
    assert entry["msg"] == "Result stored"
    assert entry["ts"].endswith("Z")


def test_json_formatter_keeps_extra_fields_separate():
    formatter = JsonFormatter(service="results-api", env="prod")

    line = formatter.format(
        make_record("ECU-001 | Temperature: -30°C", device_id="ECU-001", verdict="PASS")
    )
    entry = json.loads(line)

    assert entry["device_id"] == "ECU-001"
    assert entry["verdict"] == "PASS"
    assert "°C" in line  # readable, not escaped as °


def test_configure_logging_json_writes_one_json_line_to_stdout(capsys):
    configure_logging(make_settings(log_format="json"))

    logging.getLogger("any.module").info("hello")

    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    assert json.loads(output[0])["msg"] == "hello"


def test_configure_logging_text_uses_plain_lines(capsys):
    configure_logging(make_settings(log_format="text"))

    logging.getLogger("any.module").info("hello")

    output = capsys.readouterr().out
    assert "INFO | any.module | hello" in output


def test_configure_logging_respects_level(capsys):
    configure_logging(make_settings(log_level="WARNING"))

    logging.getLogger("any.module").info("hidden")
    logging.getLogger("any.module").warning("shown")

    output = capsys.readouterr().out
    assert "hidden" not in output
    assert "shown" in output


def test_uvicorn_loggers_use_our_format():
    logging.getLogger("uvicorn.access").addHandler(logging.StreamHandler())

    configure_logging(make_settings())

    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is True
