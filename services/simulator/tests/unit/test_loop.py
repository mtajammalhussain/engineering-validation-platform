"""Loop mode and stopping (spec §8.3.3). No real signals, no real waiting, no network."""

import json
import logging
import random
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import simulator.main
from simulator.logging_config import RESULTS_LOGGER
from simulator.main import run_batch, run_loop
from simulator.output import BatchSummary
from tests.unit.fakes import make_settings, preserved_logger_state, scripted_client

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
EMPTY = BatchSummary(0, 0, 0, 0, 0, 0)


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def lines():
    """Messages logged on simulator.results during the test."""
    handler = ListHandler()
    with preserved_logger_state():
        results = logging.getLogger(RESULTS_LOGGER)
        results.addHandler(handler)
        results.setLevel(logging.DEBUG)
        yield handler.records


def messages(records):
    return [record.getMessage() for record in records]


class FakeEvent:
    """Stands in for threading.Event: wait() returns scripted answers instantly.

    Every call is recorded in ``log`` (shared with fake batches) to check the order.
    """

    def __init__(self, *wait_answers: bool, log=None) -> None:
        self._answers = list(wait_answers)
        self._set = False
        self.log = [] if log is None else log

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float) -> bool:
        self.log.append(("wait", timeout))
        if self._answers.pop(0):
            self._set = True
        return self._set


def clock():
    times = (T0 + timedelta(minutes=i) for i in range(1000))
    return lambda: next(times)


@pytest.fixture
def fake_batches(monkeypatch):
    """Replace run_batch in main with a recorder; returns the shared call log."""
    log = []

    def fake_run_batch(client, settings, rng, *, now, wait, stop_requested):
        log.append(("batch", client, rng, wait, stop_requested))
        # A loop that never waits/stops would otherwise hang the test run.
        assert sum(entry[0] == "batch" for entry in log) <= 10, "loop does not stop"
        return EMPTY

    monkeypatch.setattr(simulator.main, "run_batch", fake_run_batch)
    return log


def loop(event, *, interval=60, batch_size=3, client=None, rng=None):
    return run_loop(
        client or object(), make_settings(sim_interval_s=interval, sim_batch_size=batch_size),
        rng or random.Random(1), event, now=clock(),
    )


# --- sequencing ---

def test_first_batch_starts_immediately_then_fixed_delay(fake_batches):
    event = FakeEvent(False, False, True, log=fake_batches)

    assert loop(event, interval=17) == 0
    assert [entry[:2] if entry[0] == "wait" else entry[0] for entry in fake_batches] == [
        "batch", ("wait", 17), "batch", ("wait", 17), "batch", ("wait", 17),
    ]


def test_stop_during_interval_starts_no_further_batch(fake_batches):
    event = FakeEvent(True, log=fake_batches)

    assert loop(event) == 0
    assert [entry[0] for entry in fake_batches] == ["batch", "wait"]


def test_stop_before_first_batch_runs_nothing(fake_batches, lines):
    event = FakeEvent(log=fake_batches)
    event.set()

    assert loop(event) == 0
    assert fake_batches == []   # no batch, no wait
    assert lines == []          # no zero-result summary


def test_same_event_client_and_rng_for_every_batch(fake_batches):
    event = FakeEvent(False, True, log=fake_batches)
    client, rng = object(), random.Random(3)

    loop(event, client=client, rng=rng)

    batches = [entry for entry in fake_batches if entry[0] == "batch"]
    assert len(batches) == 2
    for _, batch_client, batch_rng, wait, stop_requested in batches:
        assert batch_client is client and batch_rng is rng
        assert wait == event.wait                  # retry backoff uses the event
        assert stop_requested == event.is_set      # so does the stop check before each result


def test_real_event_waits_with_the_interval_as_timeout(monkeypatch, fake_batches):
    waits = []
    event = threading.Event()

    def recording_wait(timeout):
        waits.append(timeout)
        event.set()
        return True

    monkeypatch.setattr(event, "wait", recording_wait)

    assert loop(event, interval=42) == 0
    assert waits == [42]


# --- loop with the real run_batch ---

def test_batches_without_accepted_results_do_not_stop_the_loop(lines):
    # batch 1: all rejected; batch 2: all undelivered (not retried); then stop in the interval
    client, requests = scripted_client(401, 401, httpx.ReadError, httpx.ReadError)
    event = FakeEvent(False, True)

    assert loop(event, batch_size=2, client=client) == 0
    assert len(requests) == 4
    summaries = [m for m in messages(lines) if m.startswith("Batch done")]
    assert summaries == [
        "Batch done: 2 sent | 0 accepted | 2 rejected | 0 undelivered | 0 PASS | 0 FAIL",
        "Batch done: 2 sent | 0 accepted | 0 rejected | 2 undelivered | 0 PASS | 0 FAIL",
    ]
    assert event.log == [("wait", 60), ("wait", 60)]


def test_stop_during_retry_backoff_sends_no_retry_and_no_new_result(lines):
    event = threading.Event()
    requests = []

    def handler(request):  # the stop arrives while the first request is in flight
        requests.append(request)
        event.set()
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert loop(event, batch_size=3, client=client) == 0  # real Event: returns at once

    assert len(requests) == 1  # no retry
    result_line, summary = messages(lines)
    assert result_line.endswith("| UNDELIVERED: shutdown requested before retry")
    assert summary == (
        "Batch done: 1 sent | 0 accepted | 0 rejected | 1 undelivered | 0 PASS | 0 FAIL"
    )


# --- stopping inside a batch (run_batch) ---

def test_stop_between_results_gives_a_partial_summary(lines, monkeypatch):
    generated = []
    real_generate = simulator.main.generate_result

    def recording_generate(*args, **kwargs):
        generated.append(real_generate(*args, **kwargs))
        return generated[-1]

    monkeypatch.setattr(simulator.main, "generate_result", recording_generate)
    client, requests = scripted_client(201, 201)

    summary = run_batch(
        client, make_settings(sim_batch_size=5), random.Random(1),
        now=clock(), stop_requested=lambda: len(requests) >= 2,
    )

    assert summary.sent == 2 and summary.accepted == 2
    assert len(generated) == 2 and len(requests) == 2  # results 3-5 never started
    assert messages(lines)[-1].startswith("Batch done: 2 sent | 2 accepted")


def test_stop_during_an_in_flight_request_still_reports_that_result(lines, monkeypatch):
    event = threading.Event()
    real_send = simulator.main.send_result

    def send_then_stop(*args, **kwargs):
        delivery = real_send(*args, **kwargs)
        event.set()  # the signal arrived while this request was in flight
        return delivery

    monkeypatch.setattr(simulator.main, "send_result", send_then_stop)
    client, requests = scripted_client(201)

    summary = run_batch(
        client, make_settings(sim_batch_size=4), random.Random(1),
        now=clock(), wait=event.wait, stop_requested=event.is_set,
    )

    assert summary == BatchSummary(sent=1, accepted=1, rejected=0, undelivered=0,
                                   passed=summary.passed, failed=summary.failed)
    assert len(requests) == 1
    first, last = messages(lines)
    assert "| Result: " in first and last.startswith("Batch done: 1 sent")


def test_stop_before_the_first_result_of_a_batch_logs_no_summary(lines, monkeypatch):
    generated = []
    monkeypatch.setattr(simulator.main, "generate_result",
                        lambda *args, **kwargs: generated.append(args))
    client, requests = scripted_client()

    summary = run_batch(client, make_settings(sim_batch_size=3), random.Random(1),
                        now=clock(), stop_requested=lambda: True)

    assert summary == EMPTY
    assert generated == [] and requests == []
    assert lines == []  # not "Batch done: 0 sent | ..."


class RacingEvent(FakeEvent):
    """is_set() is False when run_loop checks it and True a moment later in run_batch:
    the signal arrives between the two checks."""

    def __init__(self, *is_set_answers: bool, wait_answers=(True,)) -> None:
        super().__init__(*wait_answers)
        self._is_set_answers = list(is_set_answers)

    def is_set(self) -> bool:
        return self._is_set_answers.pop(0) if self._is_set_answers else self._set


def test_signal_between_loop_check_and_first_result_logs_nothing(lines):
    client, requests = scripted_client()
    event = RacingEvent(False, True)  # run_loop: not stopped; run_batch: stopped

    assert loop(event, client=client) == 0
    assert requests == []
    assert lines == []  # no fake zero-result summary


def test_retryable_failure_after_stop_is_not_retried(lines):
    event = threading.Event()
    event.set()  # already stopped when the backoff starts
    client, requests = scripted_client(httpx.ConnectError)

    # stop_requested is not passed here, so the result starts and hits the backoff
    summary = run_batch(client, make_settings(sim_batch_size=1), random.Random(1),
                        now=clock(), wait=event.wait)

    assert len(requests) == 1
    assert summary.undelivered == 1
    assert messages(lines)[0].endswith("| UNDELIVERED: shutdown requested before retry")


def test_without_stop_requested_the_full_batch_runs(lines):
    client, requests = scripted_client(201, 201, 201)

    summary = run_batch(client, make_settings(sim_batch_size=3), random.Random(1), now=clock())

    assert summary.sent == 3 and len(requests) == 3
    assert all(json.loads(r.content)["source"] == "simulator" for r in requests)
