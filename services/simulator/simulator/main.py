"""Batch orchestration and once mode (docs/APP_SPEC.md §8.3, §8.3.3).

Wires the pieces together: generate a result, deliver it, output one line, count it.
Process startup (settings, logging, HTTP client, seed, exit code) is added later.
"""

import logging
import random
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone

import httpx

from simulator.client import DeliveryResult, send_result
from simulator.config import Settings
from simulator.generator import GeneratedResult, generate_result
from simulator.logging_config import RESULTS_LOGGER
from simulator.output import (
    BatchSummary,
    format_rejected_line,
    format_result_line,
    format_summary_line,
    format_undelivered_line,
)

results_logger = logging.getLogger(RESULTS_LOGGER)


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
) -> BatchSummary:
    """Generate and send ``SIM_BATCH_SIZE`` results one after another, then log a summary.

    The caller owns ``client`` and ``rng`` (seeded once per process). ``now`` is called for
    each result. ``wait`` is handed to ``send_result`` for the retry backoff; ``None`` keeps
    the client's default (sleep).
    """
    send_options = {} if wait is None else {"wait": wait}
    counts = dict.fromkeys(("accepted", "rejected", "undelivered", "passed", "failed"), 0)
    sent = 0

    for _ in range(settings.sim_batch_size):
        generated = generate_result(
            rng, now(),
            device_count=settings.sim_device_count, failure_rate=settings.sim_failure_rate,
        )
        sent += 1
        delivery = send_result(client, settings, generated.payload, **send_options)
        _report(generated, delivery, counts)

    summary = BatchSummary(sent=sent, **counts)
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
    return 0 if summary.accepted >= 1 else 1
