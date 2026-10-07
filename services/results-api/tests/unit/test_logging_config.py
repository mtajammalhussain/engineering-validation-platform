import json
import logging
from pathlib import Path

import pytest
import uvicorn

from app.config import Settings
from app.logging_config import (
    UVICORN_ACCESS_FORMAT,
    JsonFormatter,
    ProbeAccessFilter,
    configure_logging,
    is_quiet_probe_access,
)

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


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


# --- uvicorn loggers follow LOG_LEVEL (spec §9) --------------------------------------------

@pytest.mark.parametrize("name", UVICORN_LOGGERS)
def test_log_level_applies_to_uvicorn_loggers(capsys, name):
    logging.getLogger(name).setLevel(logging.INFO)  # as uvicorn's own config does

    configure_logging(make_settings(log_level="WARNING"))

    logger = logging.getLogger(name)
    logger.info("uvicorn info line")
    logger.warning("uvicorn warning line")
    output = capsys.readouterr().out
    assert logger.level == logging.NOTSET
    assert "uvicorn info line" not in output
    assert "uvicorn warning line" in output


@pytest.mark.parametrize("name", UVICORN_LOGGERS)
def test_uvicorn_info_lines_are_written_at_info_level(capsys, name):
    logging.getLogger(name).setLevel(logging.WARNING)

    configure_logging(make_settings(log_level="INFO"))

    logging.getLogger(name).info("uvicorn info line")
    assert "uvicorn info line" in capsys.readouterr().out


# --- probe access lines (spec §9) -----------------------------------------------------------

def access_args(path, status) -> tuple:
    """Arguments in the order uvicorn passes them: client, method, path, HTTP version, status."""
    return ("127.0.0.1:50000", "GET", path, "1.1", status)


def access_record(args, *, name="uvicorn.access", msg=UVICORN_ACCESS_FORMAT) -> logging.LogRecord:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, None)
    record.args = args  # set directly, so LogRecord does not reinterpret odd test values
    return record


@pytest.mark.parametrize(
    "path, status",
    [
        ("/health", 200),
        ("/ready", 200),
        ("/metrics", 200),
        ("/health", 302),
        ("/ready", 307),
        ("/health?x=1", 200),
        ("/metrics?name[]=a", 204),
    ],
)
def test_successful_probe_access_lines_are_suppressed(path, status):
    record = access_record(access_args(path, status))

    assert is_quiet_probe_access(record) is True
    assert ProbeAccessFilter().filter(record) is False


@pytest.mark.parametrize(
    "path, status",
    [
        ("/ready", 503),
        ("/metrics", 500),
        ("/health", 404),
        ("/health", 400),
        ("/api/v1/results", 201),
        ("/healthz", 200),
        ("/api/v1/health", 200),
        ("/health/", 200),
        ("/health/x", 200),
        ("/metrics2", 200),
    ],
)
def test_failed_or_other_access_lines_are_kept(path, status):
    record = access_record(access_args(path, status))

    assert is_quiet_probe_access(record) is False
    assert ProbeAccessFilter().filter(record) is True


@pytest.mark.parametrize(
    "record",
    [
        access_record(None),
        access_record({"path": "/health", "status": 200}),
        access_record(("127.0.0.1:50000", "GET", "/health", 200)),
        access_record(access_args("/health", 200) + ("extra",)),
        access_record(access_args(None, 200)),
        access_record(access_args(b"/health", 200)),
        access_record(access_args("/health", "200")),
        access_record(access_args("/health", None)),
        access_record(access_args("/health", True)),
        access_record(access_args("/health", 200.0)),
        access_record(access_args("/health", 200), msg='%s - "%s %s HTTP/%s" %s'),
        access_record(access_args("/health", 200), name="app.main"),
        access_record(access_args("/health", 200), name="uvicorn.error"),
    ],
    ids=[
        "args-none", "args-dict", "args-4-tuple", "args-6-tuple", "path-none", "path-bytes",
        "status-str", "status-none", "status-bool", "status-float", "other-msg",
        "app-logger", "uvicorn-error-logger",
    ],
)
def test_unrecognised_records_are_kept(record):
    # Fail open: the filter never drops what it cannot interpret, and never raises.
    assert is_quiet_probe_access(record) is False
    assert ProbeAccessFilter().filter(record) is True


def logged_messages(output: str, log_format: str) -> list[str]:
    lines = output.strip().splitlines()
    if log_format == "json":
        return [json.loads(line)["msg"] for line in lines]
    return lines


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_configured_handler_filters_probe_access_lines(capsys, log_format):
    configure_logging(make_settings(log_format=log_format))
    access = logging.getLogger("uvicorn.access")

    access.info(UVICORN_ACCESS_FORMAT, *access_args("/health", 200))
    access.info(UVICORN_ACCESS_FORMAT, *access_args("/metrics?x=1", 200))
    access.info(UVICORN_ACCESS_FORMAT, *access_args("/ready", 503))
    logging.getLogger("app.main").info("application line")

    messages = logged_messages(capsys.readouterr().out, log_format)
    assert len(messages) == 2
    assert '"GET /ready HTTP/1.1" 503' in messages[0]
    assert "application line" in messages[1]


def test_configure_logging_twice_adds_one_probe_filter():
    configure_logging(make_settings())
    configure_logging(make_settings())

    (handler,) = logging.getLogger().handlers
    assert sum(isinstance(f, ProbeAccessFilter) for f in handler.filters) == 1


@pytest.mark.parametrize("module_file", ["h11_impl.py", "httptools_impl.py"])
def test_access_format_matches_installed_uvicorn(module_file):
    # Compatibility sentinel for the pinned uvicorn: if a new version changes the access
    # line, the filter would silently fail open; this test makes the change visible.
    # The files are read as text, not imported (httptools is an optional package).
    source_file = Path(uvicorn.__file__).parent / "protocols" / "http" / module_file

    assert repr(UVICORN_ACCESS_FORMAT) in source_file.read_text(encoding="utf-8")


# --- uvicorn's color_message is not a JSON field (spec §9) ---------------------------------

COLOR_MESSAGE = "Started server process [\x1b[36m%d\x1b[0m]"


def test_json_formatter_omits_only_uvicorn_color_message():
    formatter = JsonFormatter(service="results-api", env="dev")

    entry = json.loads(formatter.format(make_record(
        "Started server process [42]",
        color_message=COLOR_MESSAGE, device_id="ECU-001", test_name="Sleep Current",
        verdict="PASS",
    )))

    assert "color_message" not in entry
    assert entry["msg"] == "Started server process [42]"
    assert (entry["device_id"], entry["test_name"], entry["verdict"]) == (
        "ECU-001", "Sleep Current", "PASS"
    )


def test_uvicorn_startup_json_line_has_no_color_message(capsys):
    configure_logging(make_settings(log_format="json"))

    logging.getLogger("uvicorn.error").info(
        "Started server process [%d]", 42, extra={"color_message": COLOR_MESSAGE}
    )

    (line,) = capsys.readouterr().out.strip().splitlines()
    entry = json.loads(line)
    assert entry["msg"] == "Started server process [42]"
    assert "color_message" not in entry
    assert "\\u001b" not in line
