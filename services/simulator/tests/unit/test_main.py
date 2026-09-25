import json
import logging
import random
import runpy
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import simulator.client
import simulator.main
from simulator.catalog import CAN_CYCLE_TIME, SLEEP_CURRENT, WAKEUP_TIME
from simulator.logging_config import RESULTS_LOGGER, configure_logging
from simulator.main import main, run_batch, run_once
from simulator.output import BatchSummary
from tests.unit.fakes import (
    API_KEY,
    API_URL,
    RecordingWait,
    make_generated,
    make_settings,
    preserved_logger_state,
    preserved_signal_handlers,
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


# =====================================================================================
# Process bootstrap: main() (spec §8.3.3). No real HTTP client, no real signals.
# =====================================================================================

SERVICE_DIR = Path(__file__).resolve().parents[2]


class FakeClient:
    """Stands in for httpx.Client: records how it was created, entered and closed."""

    def __init__(self, *args, **kwargs) -> None:
        self.args, self.kwargs = args, kwargs
        self.entered = self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc_info) -> bool:
        self.exited += 1
        return False  # never swallows an exception


class Process:
    """What main() created and called, for assertions."""

    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        self.seeds: list[object] = []
        self.rngs: list[object] = []
        self.once_calls: list[tuple] = []
        self.loop_calls: list[tuple] = []


@pytest.fixture
def process(monkeypatch):
    """Valid environment; fake HTTP client, random generator, run_once and run_loop.

    The fakes for run_once/run_loop return 0 unless a test replaces ``process.once`` /
    ``process.loop`` with its own function.
    """
    monkeypatch.setenv("RESULTS_API_URL", API_URL)
    monkeypatch.setenv("RESULTS_API_KEY", API_KEY)
    state = Process()
    state.once = lambda client, settings, rng: 0
    state.loop = lambda client, settings, rng, stop_event: 0

    def fake_client(*args, **kwargs):
        state.clients.append(FakeClient(*args, **kwargs))
        return state.clients[-1]

    def fake_random(seed):
        state.seeds.append(seed)
        state.rngs.append(object())
        return state.rngs[-1]

    def fake_run_once(client, settings, rng):
        state.once_calls.append((client, settings, rng))
        return state.once(client, settings, rng)

    def fake_run_loop(client, settings, rng, stop_event):
        state.loop_calls.append((client, settings, rng, stop_event))
        return state.loop(client, settings, rng, stop_event)

    monkeypatch.setattr(simulator.main.httpx, "Client", fake_client)
    monkeypatch.setattr(simulator.main.random, "Random", fake_random)
    monkeypatch.setattr(simulator.main, "run_once", fake_run_once)
    monkeypatch.setattr(simulator.main, "run_loop", fake_run_loop)
    with preserved_logger_state():  # main() configures the real logging
        yield state


@pytest.fixture
def signal_calls(monkeypatch):
    """Record signal.signal() calls instead of changing the real handlers."""
    calls = []

    def fake_signal(signum, handler):
        calls.append((signum, handler))
        return f"previous-{signum}"

    monkeypatch.setattr(simulator.main.signal, "signal", fake_signal)
    return calls


# --- dispatch and exit codes ---

@pytest.mark.parametrize("exit_code", [0, 1])
def test_once_mode_runs_one_batch_and_returns_its_exit_code(process, signal_calls, exit_code):
    process.once = lambda client, settings, rng: exit_code

    assert main() == exit_code

    [client] = process.clients
    assert process.once_calls == [(client, process.once_calls[0][1], process.rngs[0])]
    assert process.loop_calls == []


def test_once_mode_installs_no_signal_handlers(process, signal_calls):
    main()

    assert signal_calls == []


def test_loop_mode_runs_the_loop_with_handlers_and_restores_them(
    process, signal_calls, monkeypatch, capsys
):
    monkeypatch.setenv("SIM_MODE", "loop")
    seen = {}

    def fake_loop(client, settings, rng, stop_event):
        handlers = dict(signal_calls)  # installed before the loop starts
        assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
        handlers[signal.SIGTERM](signal.SIGTERM, None)  # call the handler directly
        seen["stopped_by_sigterm"] = stop_event.is_set()
        seen["client_still_open"] = client.exited == 0
        return 0

    process.loop = fake_loop

    assert main() == 0

    assert seen == {"stopped_by_sigterm": True, "client_still_open": True}
    assert process.once_calls == []
    assert isinstance(process.loop_calls[0][3], threading.Event)
    # installed for both signals, then restored to the previous handlers
    assert [signum for signum, _ in signal_calls] == [
        signal.SIGTERM, signal.SIGINT, signal.SIGTERM, signal.SIGINT,
    ]
    assert signal_calls[2:] == [
        (signal.SIGTERM, f"previous-{signal.SIGTERM}"),
        (signal.SIGINT, f"previous-{signal.SIGINT}"),
    ]
    assert capsys.readouterr().out == ""  # the handler itself logs and prints nothing


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_handler_only_sets_the_stop_event(process, signal_calls, monkeypatch, signum):
    monkeypatch.setenv("SIM_MODE", "loop")
    result = {}

    def fake_loop(client, settings, rng, stop_event):
        handler = dict(signal_calls)[signum]
        assert handler(signum, None) is None  # returns normally: no SystemExit
        result["set"] = stop_event.is_set()
        return 0

    process.loop = fake_loop

    assert main() == 0
    assert result == {"set": True}


def test_real_handlers_are_restored_after_loop_mode(process, monkeypatch):
    monkeypatch.setenv("SIM_MODE", "loop")
    with preserved_signal_handlers():
        before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        during = {}

        def fake_loop(client, settings, rng, stop_event):
            during.update({s: signal.getsignal(s) for s in before})
            return 0

        process.loop = fake_loop

        assert main() == 0
        assert all(during[s] is not before[s] for s in before)  # ours while looping
        assert {s: signal.getsignal(s) for s in before} == before  # restored


def test_unknown_previous_handler_is_restored_as_the_default_action(process, monkeypatch):
    monkeypatch.setenv("SIM_MODE", "loop")
    calls = []

    def fake_signal(signum, handler):
        calls.append((signum, handler))
        return None  # "previous handler was not installed from Python"

    monkeypatch.setattr(simulator.main.signal, "signal", fake_signal)  # real handlers untouched

    assert main() == 0
    assert calls[2:] == [(signal.SIGTERM, signal.SIG_DFL), (signal.SIGINT, signal.SIG_DFL)]


def test_restoring_does_not_hide_the_original_error(process, monkeypatch, capsys):
    monkeypatch.setenv("SIM_MODE", "loop")
    monkeypatch.setattr(simulator.main.signal, "signal", lambda signum, handler: None)

    def failing_loop(client, settings, rng, stop_event):
        raise RuntimeError("original loop error")

    process.loop = failing_loop

    assert main() == 3
    assert "RuntimeError: original loop error" in capsys.readouterr().out  # via the logger


def failing_signal(monkeypatch, *failing_calls: int):
    """Fake signal.signal(): calls are numbered 1 = install SIGTERM, 2 = install SIGINT,
    3 = restore SIGTERM, 4 = restore SIGINT; the listed ones raise OSError."""
    calls = []

    def fake_signal(signum, handler):
        calls.append((signum, handler))
        if len(calls) in failing_calls:
            raise OSError(f"signal call {len(calls)} failed")
        return f"previous-{signum}"

    monkeypatch.setattr(simulator.main.signal, "signal", fake_signal)
    return calls


def json_log(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines()]


def raise_original(client, settings, rng, stop_event):
    raise RuntimeError("original")


@pytest.mark.parametrize(
    "failing_restores, warned",
    [((3,), ["SIGTERM"]), ((3, 4), ["SIGTERM", "SIGINT"])],
    ids=["sigterm-restore-fails", "both-restores-fail"],
)
def test_restore_failure_is_logged_and_the_loop_error_still_wins(
    process, monkeypatch, capsys, failing_restores, warned
):
    monkeypatch.setenv("SIM_MODE", "loop")
    monkeypatch.setenv("LOG_FORMAT", "json")
    calls = failing_signal(monkeypatch, *failing_restores)
    process.loop = raise_original

    assert main() == 3

    *warnings, error = json_log(capsys.readouterr().out)
    assert calls[3][0] == signal.SIGINT  # SIGINT restore attempted after SIGTERM failed
    assert [(w["level"], w["msg"]) for w in warnings] == [
        ("WARNING", f"Could not restore the {name} handler") for name in warned
    ]
    for warning, call in zip(warnings, failing_restores):
        assert f"OSError: signal call {call} failed" in warning["exc_info"]
    # the original error is still the one reported as the process failure
    assert (error["level"], error["msg"]) == ("ERROR", "Unexpected simulator error")
    assert "RuntimeError: original" in error["exc_info"]
    assert "OSError" not in error["exc_info"]


def test_restore_failure_after_a_clean_loop_is_raised(process, monkeypatch, capsys):
    monkeypatch.setenv("SIM_MODE", "loop")
    monkeypatch.setenv("LOG_FORMAT", "json")
    calls = failing_signal(monkeypatch, 3)  # first restore (SIGTERM) fails

    assert main() == 3

    [error] = json_log(capsys.readouterr().out)
    assert calls[3] == (signal.SIGINT, f"previous-{signal.SIGINT}")  # both restores tried
    assert "OSError: signal call 3 failed" in error["exc_info"]  # first failure raised


def test_both_restores_failing_after_a_clean_loop_raise_the_first(
    process, monkeypatch, capsys
):
    monkeypatch.setenv("SIM_MODE", "loop")
    monkeypatch.setenv("LOG_FORMAT", "json")
    failing_signal(monkeypatch, 3, 4)

    assert main() == 3

    warning, error = json_log(capsys.readouterr().out)
    assert warning["msg"] == "Could not restore the SIGINT handler"  # the second is logged
    assert "OSError: signal call 3 failed" in error["exc_info"]      # the first is raised


def test_failed_installation_restores_the_handler_already_replaced(
    process, monkeypatch, capsys
):
    monkeypatch.setenv("SIM_MODE", "loop")
    calls = failing_signal(monkeypatch, 2)  # installing SIGINT fails

    assert main() == 3

    assert [signum for signum, _ in calls] == [signal.SIGTERM, signal.SIGINT, signal.SIGTERM]
    assert calls[2] == (signal.SIGTERM, f"previous-{signal.SIGTERM}")  # rolled back
    assert process.loop_calls == []  # the loop never started
    assert "OSError: signal call 2 failed" in capsys.readouterr().out


def test_handlers_are_restored_when_the_loop_fails(process, signal_calls, monkeypatch):
    monkeypatch.setenv("SIM_MODE", "loop")

    def failing_loop(client, settings, rng, stop_event):
        raise RuntimeError("loop failed")

    process.loop = failing_loop

    assert main() == 3
    assert signal_calls[2:] == [
        (signal.SIGTERM, f"previous-{signal.SIGTERM}"),
        (signal.SIGINT, f"previous-{signal.SIGINT}"),
    ]
    assert process.clients[0].exited == 1


# --- process resources: one random generator, one HTTP client ---

@pytest.mark.parametrize("env_value, seed", [("123", 123), ("0", 0), ("", None), (None, None)])
def test_random_generator_is_created_once_from_sim_seed(process, monkeypatch, env_value, seed):
    if env_value is not None:
        monkeypatch.setenv("SIM_SEED", env_value)

    main()

    assert process.seeds == [seed]  # exactly one generator; SIM_SEED=0 stays 0
    assert process.once_calls[0][2] is process.rngs[0]


def test_one_plain_http_client_is_created_and_closed(process):
    main()

    [client] = process.clients
    assert (client.args, client.kwargs) == ((), {})  # default httpx settings (trust_env)
    assert (client.entered, client.exited) == (1, 1)
    assert process.once_calls[0][0] is client


def test_loop_mode_gets_the_one_client_and_generator(process, monkeypatch):
    monkeypatch.setenv("SIM_MODE", "loop")

    main()

    [client] = process.clients
    assert process.loop_calls[0][0] is client and process.loop_calls[0][2] is process.rngs[0]
    assert client.exited == 1


# --- invalid configuration and unexpected errors ---

def test_invalid_configuration_exits_2_before_anything_starts(process, monkeypatch, capsys):
    monkeypatch.delenv("RESULTS_API_URL")

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "RESULTS_API_URL" in capsys.readouterr().out
    assert process.clients == [] and process.seeds == [] and process.once_calls == []


def raise_runtime_error(*args):
    raise RuntimeError("boom-internal")


def test_unexpected_error_returns_3_and_closes_the_client(process):
    process.once = raise_runtime_error

    assert main() == 3
    assert process.clients[0].exited == 1


def test_unexpected_error_is_logged_as_json(process, monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    process.once = raise_runtime_error

    assert main() == 3

    captured = capsys.readouterr()
    [entry] = [json.loads(line) for line in captured.out.splitlines()]  # every line is JSON
    assert entry["level"] == "ERROR"
    assert entry["msg"] == "Unexpected simulator error"
    assert "Traceback" in entry["exc_info"]
    assert "RuntimeError: boom-internal" in entry["exc_info"]
    assert captured.err == ""


def test_unexpected_error_is_logged_as_text(process, capsys):
    process.once = raise_runtime_error

    assert main() == 3

    captured = capsys.readouterr()
    assert " | ERROR | simulator.main | Unexpected simulator error" in captured.out
    assert "Traceback" in captured.out and "RuntimeError: boom-internal" in captured.out
    assert captured.err == ""


def test_keyboard_interrupt_is_not_turned_into_exit_code_3(process):
    def interrupted(*args):
        raise KeyboardInterrupt

    process.once = interrupted

    with pytest.raises(KeyboardInterrupt):
        main()
    assert process.clients[0].exited == 1


# --- python -m simulator ---

def test_package_entry_point_only_delegates_to_main(monkeypatch):
    monkeypatch.setattr(simulator.main, "main", lambda: 7)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("simulator", run_name="__main__")

    assert exc_info.value.code == 7


def test_importing_main_has_no_side_effects():
    code = (
        "import logging, signal\n"
        "before = [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)]\n"
        "import simulator.main\n"
        "assert [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)] == before\n"
        "assert logging.getLogger().handlers == []\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=SERVICE_DIR, env={}, capture_output=True,
        text=True, timeout=30,
    )

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
