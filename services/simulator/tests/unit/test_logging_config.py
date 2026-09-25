import json
import logging
import sys

import pytest

from simulator.logging_config import RESULTS_LOGGER, JsonFormatter, configure_logging
from tests.unit.fakes import make_settings

TOUCHED_LOGGERS = (None, RESULTS_LOGGER, "httpx", "httpcore")
RESULT_LINE = (
    "ECU-004 | Test: Wake-up Time | Temperature: 105°C | Measured time: 163.25 ms "
    "| Result: FAIL (limits: ≤ 150 ms)"
)


@pytest.fixture(autouse=True)
def restore_loggers():
    """Put every logger we change back as it was, so tests do not affect each other."""
    loggers = [logging.getLogger(name) for name in TOUCHED_LOGGERS]
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


def output_lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


# --- JSON formatter ---

def test_json_formatter_writes_required_fields():
    entry = json.loads(JsonFormatter(service="simulator", env="dev").format(make_record("hi")))

    assert entry["level"] == "INFO"
    assert entry["service"] == "simulator"
    assert entry["env"] == "dev"
    assert entry["msg"] == "hi"
    assert entry["ts"].endswith("Z")


def test_json_formatter_keeps_extra_fields_separate():
    formatter = JsonFormatter(service="simulator", env="prod")

    entry = json.loads(
        formatter.format(make_record("line", device_id="ECU-001", verdict="PASS", sent=10))
    )

    assert entry["device_id"] == "ECU-001"
    assert entry["verdict"] == "PASS"
    assert entry["sent"] == 10


def test_json_formatter_writes_special_characters_literally():
    line = JsonFormatter(service="simulator", env="dev").format(make_record("-30°C 0.01–0.40 ≤ ≥"))

    assert "°" in line and "–" in line and "≤" in line and "≥" in line
    assert "\\u" not in line
    assert json.loads(line)["msg"] == "-30°C 0.01–0.40 ≤ ≥"


def test_json_formatter_includes_exception_text():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )

    entry = json.loads(JsonFormatter(service="simulator", env="dev").format(record))

    assert "RuntimeError: boom" in entry["exc_info"]


# --- configure_logging: JSON mode ---

def test_json_mode_writes_one_json_object_per_line(capsys):
    configure_logging(make_settings(log_format="json"))

    logging.getLogger("simulator.client").warning("retrying")
    logging.getLogger("simulator.client").info("second")

    lines = output_lines(capsys)
    assert len(lines) == 2
    assert [json.loads(line)["msg"] for line in lines] == ["retrying", "second"]


def test_json_mode_results_logger_writes_structured_json_once(capsys):
    configure_logging(make_settings(log_format="json"))

    logging.getLogger(RESULTS_LOGGER).info(
        RESULT_LINE, extra={"device_id": "ECU-004", "test_name": "Wake-up Time", "verdict": "FAIL"}
    )

    lines = output_lines(capsys)
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["msg"] == RESULT_LINE
    assert entry["service"] == "simulator"
    assert (entry["device_id"], entry["test_name"], entry["verdict"]) == (
        "ECU-004", "Wake-up Time", "FAIL",
    )
    assert "≤" in lines[0] and "°" in lines[0]


# --- configure_logging: text mode ---

def test_text_mode_other_loggers_use_prefixed_format(capsys):
    configure_logging(make_settings(log_format="text"))

    logging.getLogger("simulator.client").info("hello")

    lines = output_lines(capsys)
    assert len(lines) == 1
    assert lines[0].endswith(" | INFO | simulator.client | hello")


def test_text_mode_results_logger_prints_exactly_one_bare_line(capsys):
    configure_logging(make_settings(log_format="text"))

    logging.getLogger(RESULTS_LOGGER).info(RESULT_LINE)

    lines = output_lines(capsys)
    assert lines == [RESULT_LINE]  # exactly one line, no time, level or logger prefix
    assert "INFO" not in lines[0]
    assert RESULTS_LOGGER not in lines[0]


def test_results_logger_does_not_propagate_to_root():
    configure_logging(make_settings(log_format="text"))

    results = logging.getLogger(RESULTS_LOGGER)
    assert results.propagate is False
    assert len(results.handlers) == 1


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_repeated_configuration_does_not_duplicate_output(capsys, log_format):
    for _ in range(3):
        configure_logging(make_settings(log_format=log_format))

    logging.getLogger(RESULTS_LOGGER).info("result")
    logging.getLogger("simulator.client").info("other")

    assert len(output_lines(capsys)) == 2
    assert len(logging.getLogger().handlers) == 1
    assert len(logging.getLogger(RESULTS_LOGGER).handlers) == 1


# --- levels ---

def test_global_level_is_respected(capsys):
    configure_logging(make_settings(log_level="WARNING"))

    logging.getLogger("simulator.client").info("hidden")
    logging.getLogger(RESULTS_LOGGER).info("hidden result")
    logging.getLogger("simulator.client").warning("shown")
    logging.getLogger(RESULTS_LOGGER).warning("shown result")

    output = capsys.readouterr().out
    assert "hidden" not in output
    assert "shown" in output
    assert "shown result" in output


@pytest.mark.parametrize(
    "log_level, expected",
    [
        ("DEBUG", logging.WARNING),
        ("INFO", logging.WARNING),
        ("WARNING", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
    ],
)
def test_http_client_loggers_use_warning_or_stricter_global_level(log_level, expected):
    configure_logging(make_settings(log_level=log_level))

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() == expected


def test_http_client_info_lines_are_suppressed(capsys):
    configure_logging(make_settings(log_level="INFO"))

    logging.getLogger("httpx").info('HTTP Request: POST http://x "HTTP/1.1 201 Created"')
    logging.getLogger("httpcore").debug("send_request_headers.started")

    assert capsys.readouterr().out == ""


def test_http_client_warnings_are_hidden_when_global_level_is_error(capsys):
    configure_logging(make_settings(log_level="ERROR"))

    logging.getLogger("httpx").warning("warning from httpx")

    assert capsys.readouterr().out == ""
