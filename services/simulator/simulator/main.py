"""Process entry point, batch orchestration, once and loop mode (docs/APP_SPEC.md §8.3).

Wires the pieces together: generate a result, deliver it, output one line, count it.
Started with ``python -m simulator`` (see ``__main__.py``).
"""

import logging
import random
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone

import httpx

from simulator.client import DeliveryResult, send_result
from simulator.config import Settings, load_settings
from simulator.generator import GeneratedResult, generate_result
from simulator.logging_config import RESULTS_LOGGER, configure_logging
from simulator.output import (
    BatchSummary,
    format_rejected_line,
    format_result_line,
    format_summary_line,
    format_undelivered_line,
)

logger = logging.getLogger(__name__)
results_logger = logging.getLogger(RESULTS_LOGGER)

# Exit codes (spec §8.3.3). 1 = once mode without an accepted result; 2 = invalid
# configuration, raised by load_settings() before anything else happens.
EXIT_OK = 0
EXIT_NOTHING_ACCEPTED = 1
EXIT_INTERNAL_ERROR = 3

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _report(generated: GeneratedResult, delivery: DeliveryResult, counts: dict[str, int]) -> None:
    """Count one delivered result and log exactly one line for it."""
    payload = generated.payload
    counts[delivery.outcome] += 1
    if delivery.outcome == "accepted":
        # PASS/FAIL are counted for accepted results only.
        counts["passed" if payload["verdict"] == "PASS" else "failed"] += 1
        results_logger.info(
            format_result_line(generated),
            extra={
                "device_id": payload["device_id"],
                "test_name": payload["test_name"],
                "verdict": payload["verdict"],
            },
        )
        return
    line = (
        format_rejected_line(generated, delivery)
        if delivery.outcome == "rejected"
        else format_undelivered_line(generated, delivery)
    )
    results_logger.warning(
        line,
        extra={
            "device_id": payload["device_id"],
            "test_name": payload["test_name"],
            "outcome": delivery.outcome,
            "status_code": delivery.status_code,
        },
    )


def run_batch(
    client: httpx.Client,
    settings: Settings,
    rng: random.Random,
    *,
    now: Callable[[], datetime] = _utc_now,
    wait: Callable[[float], bool] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> BatchSummary:
    """Generate and send ``SIM_BATCH_SIZE`` results one after another, then log a summary.

    The caller owns ``client`` and ``rng`` (seeded once per process). ``now`` is called for
    each result. ``wait`` is handed to ``send_result`` for the retry backoff; ``None`` keeps
    the client's default (sleep). If ``stop_requested()`` returns True, no further result is
    started and the summary covers only the results sent so far (a partial batch).
    """
    send_options = {} if wait is None else {"wait": wait}
    counts = dict.fromkeys(("accepted", "rejected", "undelivered", "passed", "failed"), 0)
    sent = 0

    for _ in range(settings.sim_batch_size):
        if stop_requested is not None and stop_requested():
            break
        generated = generate_result(
            rng, now(),
            device_count=settings.sim_device_count, failure_rate=settings.sim_failure_rate,
        )
        sent += 1
        delivery = send_result(client, settings, generated.payload, **send_options)
        _report(generated, delivery, counts)

    summary = BatchSummary(sent=sent, **counts)
    # sent == 0 only happens when a stop was requested before the first result: then this
    # batch never started and gets no summary line (only a started batch reports, spec §8.3.3).
    if sent:
        results_logger.info(format_summary_line(summary), extra=asdict(summary))
    return summary


def run_once(
    client: httpx.Client,
    settings: Settings,
    rng: random.Random,
    *,
    now: Callable[[], datetime] = _utc_now,
    wait: Callable[[float], bool] | None = None,
) -> int:
    """Run one batch. Exit code 0 if at least one result was accepted, else 1 (spec §8.3.3)."""
    summary = run_batch(client, settings, rng, now=now, wait=wait)
    return EXIT_OK if summary.accepted >= 1 else EXIT_NOTHING_ACCEPTED


def run_loop(
    client: httpx.Client,
    settings: Settings,
    rng: random.Random,
    stop_event: threading.Event,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> int:
    """Run batches until ``stop_event`` is set, then return 0 (spec §8.3.3).

    The first batch starts at once; after each completed batch the loop waits
    ``SIM_INTERVAL_S`` (fixed delay). The same event stops everything: no new result is
    started, a retry backoff ends without retrying, and the wait between batches ends
    immediately. A batch without any accepted result does not stop the loop.
    """
    while not stop_event.is_set():
        run_batch(
            client, settings, rng,
            now=now, wait=stop_event.wait, stop_requested=stop_event.is_set,
        )
        if stop_event.wait(settings.sim_interval_s):  # True: stop requested while waiting
            break
    return EXIT_OK


@contextmanager
def _stop_on_signals(stop_event: threading.Event) -> Iterator[None]:
    """While active, SIGTERM and SIGINT only set ``stop_event``; afterwards restore the old
    handlers (also after an exception)."""

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()  # nothing else: no logging, no I/O inside a signal handler

    previous: dict[int, object] = {}
    try:
        for signum in STOP_SIGNALS:  # if one fails, the ones already replaced are restored
            previous[signum] = signal.signal(signum, request_stop)
        yield
    except BaseException:
        # An error is already on its way out: restore both handlers, but only log a restore
        # failure, so it can never replace the original error.
        for signum, restore_error in _restore_handlers(previous):
            _log_restore_failure(signum, restore_error)
        raise
    failures = _restore_handlers(previous)
    for signum, restore_error in failures[1:]:
        _log_restore_failure(signum, restore_error)
    if failures:
        raise failures[0][1]  # clean run: the first restore failure is the error


def _restore_handlers(previous: dict[int, object]) -> list[tuple[int, Exception]]:
    """Put every saved handler back, even if one fails; return the failures."""
    failures = []
    for signum, handler in previous.items():
        try:
            # None means the old handler was not set from Python; it cannot be passed back
            # to signal.signal(), so the default action is restored instead.
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)
        except Exception as restore_error:
            failures.append((signum, restore_error))
    return failures


def _log_restore_failure(signum: int, restore_error: Exception) -> None:
    logger.warning(
        "Could not restore the %s handler", signal.Signals(signum).name, exc_info=restore_error
    )


def main() -> int:
    """Run the simulator process and return its exit code (spec §8.3.3).

    Returns 0 or 1 (once mode), 0 (clean loop stop) or 3 (unexpected error after logging
    is set up). Code 2 is not returned: invalid configuration raises ``SystemExit(2)`` inside
    ``load_settings()``, before logging is set up or any HTTP client exists.
    """
    settings = load_settings()
    configure_logging(settings)
    try:
        # One random generator and one HTTP client for the whole process.
        rng = random.Random(settings.sim_seed)  # None (unset) = random seed; 0 is a valid seed
        with httpx.Client() as client:
            if settings.sim_mode == "once":
                # No signal handlers: SIGTERM/SIGINT keep their normal behaviour.
                return run_once(client, settings, rng)
            stop_event = threading.Event()
            with _stop_on_signals(stop_event):
                return run_loop(client, settings, rng, stop_event)
    except Exception:
        # The single place that turns an unexpected error into exit code 3. SystemExit and
        # KeyboardInterrupt are not Exception subclasses and pass through unchanged.
        logger.exception("Unexpected simulator error")
        return EXIT_INTERNAL_ERROR
