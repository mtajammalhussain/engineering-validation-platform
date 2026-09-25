import json
import logging
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import simulator.client
import simulator.main
from simulator.catalog import CAN_CYCLE_TIME, SLEEP_CURRENT, WAKEUP_TIME
from simulator.logging_config import RESULTS_LOGGER, configure_logging
from simulator.main import run_batch, run_once
from simulator.output import BatchSummary
from tests.unit.fakes import (
    RecordingWait,
    make_generated,
    make_settings,
    preserved_logger_state,
    scripted_client,
)

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records():
    """Records of the simulator.results logger (it may not propagate to caplog's handler)."""
    handler = ListHandler()
    with preserved_logger_state():
        results = logging.getLogger(RESULTS_LOGGER)
        results.addHandler(handler)
        results.setLevel(logging.DEBUG)
        yield handler.records


@pytest.fixture
def scripted_results(monkeypatch):
    """Replace generate_result in main with scripted results; record the arguments."""
    calls = []

    def install(*results):
        remaining = list(results)

        def fake_generate(rng, now, *, device_count, failure_rate):
            calls.append((rng, now))
            return remaining.pop(0)

        monkeypatch.setattr(simulator.main, "generate_result", fake_generate)
        return calls

    return install


def clock():
    """A fake clock: T0, T0 + 1 min, T0 + 2 min, ..."""
    times = (T0 + timedelta(minutes=i) for i in range(1000))
    return lambda: next(times)


def batch(*outcomes, batch_size, wait=None, rng=None):
    """Run one batch against a scripted client; return (summary, requests, wait)."""
    client, requests = scripted_client(*outcomes)
    wait = wait or RecordingWait()
    summary = run_batch(
        client, make_settings(sim_batch_size=batch_size), rng or random.Random(1),
        now=clock(), wait=wait,
    )
    return summary, requests, wait


def messages(records):
    return [(r.levelname, r.getMessage()) for r in records]


# --- whole batches with the real generator ---

def test_all_accepted(records):
    summary, requests, wait = batch(201, 201, 201, batch_size=3)

    assert (summary.sent, summary.accepted, summary.rejected, summary.undelivered) == (3, 3, 0, 0)
    assert summary.passed + summary.failed == 3
    assert len(requests) == 3
    assert wait.delays == []
    assert [r.levelname for r in records] == ["INFO"] * 4  # 3 result lines + summary
    assert records[-1].getMessage().startswith("Batch done: 3 sent | 3 accepted")


def test_all_rejected(records):
    summary, requests, _ = batch(401, 401, 401, 401, batch_size=4)

    assert summary == BatchSummary(sent=4, accepted=0, rejected=4, undelivered=0,
                                   passed=0, failed=0)
    assert len(requests) == 4
    assert [r.levelname for r in records] == ["WARNING"] * 4 + ["INFO"]
    assert all("| REJECTED: HTTP 401 (invalid or missing API key)" in r.getMessage()
               for r in records[:4])


def test_all_undelivered_counts_results_not_http_attempts(records):
    summary, requests, wait = batch(*[503] * 6, batch_size=2)

    assert summary == BatchSummary(sent=2, accepted=0, rejected=0, undelivered=2,
                                   passed=0, failed=0)
    assert len(requests) == 6  # 2 results x 3 attempts
    assert wait.delays == [1, 2, 1, 2]
    assert all(r.getMessage().endswith("| UNDELIVERED: HTTP 503 after 3 attempts")
               for r in records[:2])


def test_unexpected_error_propagates(records):
    with pytest.raises(RuntimeError):
        batch(RuntimeError, batch_size=1)


def test_default_wait_is_the_clients_sleep(monkeypatch, records):
    slept = []
    monkeypatch.setattr(simulator.client.time, "sleep", slept.append)
    client, _ = scripted_client(503, 201)

    summary = run_batch(client, make_settings(sim_batch_size=1), random.Random(1), now=clock())

    assert summary.accepted == 1
    assert slept == [1]


def test_client_is_not_closed(records):
    client, _ = scripted_client(201)

    run_batch(client, make_settings(sim_batch_size=1), random.Random(1), now=clock())

    assert not client.is_closed


# --- mixed batch with scripted results ---

MIXED = [
    make_generated(SLEEP_CURRENT, "ECU-001", -30, 0.18, "PASS"),   # accepted PASS
    make_generated(WAKEUP_TIME, "ECU-002", 105, 163.25, "FAIL"),   # accepted FAIL
    make_generated(SLEEP_CURRENT, "ECU-003", -40, 0.52, "FAIL"),   # rejected
    make_generated(CAN_CYCLE_TIME, "ECU-004", 23, 11.0, "FAIL"),   # undelivered
]
MIXED_OUTCOMES = (201, 201, 422, httpx.ConnectError, httpx.ConnectError, httpx.ConnectError)


def test_mixed_batch_counts_and_lines(records, scripted_results):
    scripted_results(*MIXED)

    summary, _, _ = batch(*MIXED_OUTCOMES, batch_size=4)

    # Rejected and undelivered FAIL results do not count as FAIL.
    assert summary == BatchSummary(sent=4, accepted=2, rejected=1, undelivered=1,
                                   passed=1, failed=1)
    assert messages(records) == [
        ("INFO", "ECU-001 | Test: Sleep Current | Temperature: -30°C | Measured current: 0.18 mA"
                 " | Result: PASS"),
        ("INFO", "ECU-002 | Test: Wake-up Time | Temperature: 105°C | Measured time: 163.25 ms"
                 " | Result: FAIL (limits: ≤ 150 ms)"),
        ("WARNING", "ECU-003 | Test: Sleep Current | REJECTED: HTTP 422"
                    " (payload failed validation)"),
        ("WARNING", "ECU-004 | Test: CAN Cycle Time | UNDELIVERED: ConnectError after 3 attempts"),
        ("INFO", "Batch done: 4 sent | 2 accepted | 1 rejected | 1 undelivered | 1 PASS | 1 FAIL"),
    ]


def test_mixed_batch_structured_fields(records, scripted_results):
    scripted_results(*MIXED)

    batch(*MIXED_OUTCOMES, batch_size=4)

    accepted, _, rejected, undelivered, summary = records
    assert (accepted.name, accepted.device_id, accepted.test_name, accepted.verdict) == (
        RESULTS_LOGGER, "ECU-001", "Sleep Current", "PASS"
    )
    assert (rejected.device_id, rejected.test_name, rejected.outcome, rejected.status_code) == (
        "ECU-003", "Sleep Current", "rejected", 422
    )
    assert (undelivered.outcome, undelivered.status_code) == ("undelivered", None)
    assert {key: getattr(summary, key) for key in
            ("sent", "accepted", "rejected", "undelivered", "passed", "failed")} == {
        "sent": 4, "accepted": 2, "rejected": 1, "undelivered": 1, "passed": 1, "failed": 1,
    }
    for record in records:
        assert not hasattr(record, "payload")


def test_undelivered_after_5xx_keeps_its_status_code(records, scripted_results):
    scripted_results(MIXED[3])

    batch(503, 503, 503, batch_size=1)

    assert (records[0].outcome, records[0].status_code) == ("undelivered", 503)


def test_rejected_fail_and_undelivered_fail_do_not_count_as_fail(records, scripted_results):
    scripted_results(MIXED[2], MIXED[3])

    summary, _, _ = batch(401, httpx.ReadTimeout, batch_size=2)

    assert (summary.rejected, summary.undelivered, summary.passed, summary.failed) == (1, 1, 0, 0)


# --- once mode exit code ---

@pytest.mark.parametrize(
    "outcomes, batch_size, exit_code",
    [
        ((201, 201, 201), 3, 0),
        ((201, 401, 401), 3, 0),
        ((201, 503, 503, 503), 2, 0),
        ((401, 401, 401), 3, 1),
        ((httpx.ConnectError,) * 6, 2, 1),
        ((401, httpx.ReadError), 2, 1),
    ],
)
def test_run_once_exit_code(records, outcomes, batch_size, exit_code):
    client, _ = scripted_client(*outcomes)

    code = run_once(client, make_settings(sim_batch_size=batch_size), random.Random(1),
                    now=clock(), wait=RecordingWait())

    assert code == exit_code


# --- inputs handed to generator and client ---

def test_now_is_called_once_per_result(records):
    times = [T0, T0 + timedelta(seconds=7), T0 + timedelta(minutes=3)]
    scripted_now = iter(times)
    client, requests = scripted_client(201, 201, 201)

    run_batch(client, make_settings(sim_batch_size=3), random.Random(1),
              now=lambda: next(scripted_now), wait=RecordingWait())

    assert [json.loads(r.content)["started_at"] for r in requests] == [
        t.isoformat() for t in times
    ]


def test_one_rng_object_is_reused(monkeypatch, records):
    seen = []
    real_generate = simulator.main.generate_result

    def recording_generate(rng, now, **kwargs):
        seen.append(rng)
        return real_generate(rng, now, **kwargs)

    monkeypatch.setattr(simulator.main, "generate_result", recording_generate)
    rng = random.Random(5)

    batch(201, 201, 201, batch_size=3, rng=rng)

    assert len(seen) == 3 and all(item is rng for item in seen)


def test_same_seed_gives_the_same_requests(records):
    _, first, _ = batch(201, 201, 201, batch_size=3, rng=random.Random(9))
    _, second, _ = batch(201, 201, 201, batch_size=3, rng=random.Random(9))

    assert [r.content for r in first] == [r.content for r in second]


def test_generated_payload_is_handed_over_unchanged(monkeypatch, records):
    generated, sent = [], []
    real_generate, real_send = simulator.main.generate_result, simulator.main.send_result

    def recording_generate(*args, **kwargs):
        generated.append(real_generate(*args, **kwargs))
        return generated[-1]

    def recording_send(client, settings, payload, **kwargs):
        sent.append(payload)
        return real_send(client, settings, payload, **kwargs)

    monkeypatch.setattr(simulator.main, "generate_result", recording_generate)
    monkeypatch.setattr(simulator.main, "send_result", recording_send)

    _, requests, _ = batch(201, 201, batch_size=2)

    assert all(s is g.payload for s, g in zip(sent, generated, strict=True))
    assert [json.loads(r.content) for r in requests] == [g.payload for g in generated]


# --- real logging configuration (smoke tests) ---

def test_text_logging_prints_bare_lines(capsys, scripted_results):
    scripted_results(MIXED[1])
    with preserved_logger_state():
        configure_logging(make_settings(log_format="text"))
        batch(201, batch_size=1)

    assert capsys.readouterr().out.splitlines() == [
        "ECU-002 | Test: Wake-up Time | Temperature: 105°C | Measured time: 163.25 ms"
        " | Result: FAIL (limits: ≤ 150 ms)",
        "Batch done: 1 sent | 1 accepted | 0 rejected | 0 undelivered | 0 PASS | 1 FAIL",
    ]


def test_json_logging_writes_structured_lines(capsys, scripted_results):
    scripted_results(MIXED[1])
    with preserved_logger_state():
        configure_logging(make_settings(log_format="json"))
        batch(201, batch_size=1)

    lines = capsys.readouterr().out.splitlines()
    result, summary = (json.loads(line) for line in lines)
    assert result["msg"] == (
        "ECU-002 | Test: Wake-up Time | Temperature: 105°C | Measured time: 163.25 ms"
        " | Result: FAIL (limits: ≤ 150 ms)"
    )
    assert (result["device_id"], result["test_name"], result["verdict"]) == (
        "ECU-002", "Wake-up Time", "FAIL"
    )
    assert (summary["sent"], summary["accepted"], summary["failed"]) == (1, 1, 1)
    assert "°C" in lines[0] and "≤" in lines[0] and "\\u" not in lines[0]


def test_json_logging_keeps_en_dash_and_ge_literal(capsys, scripted_results):
    scripted_results(make_generated(SLEEP_CURRENT, "ECU-002", -40, 0.52, "FAIL"))
    with preserved_logger_state():
        configure_logging(make_settings(log_format="json"))
        batch(201, batch_size=1)

    first_line = capsys.readouterr().out.splitlines()[0]
    assert "0.01–0.40 mA" in first_line and "\\u" not in first_line
