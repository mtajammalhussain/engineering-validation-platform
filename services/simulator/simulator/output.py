"""Human-readable output lines (docs/APP_SPEC.md §8.4).

Pure functions only: they build strings and never print or log. ``main.py`` writes the
lines through the ``simulator.results`` logger (spec §9).
"""

from dataclasses import dataclass

from simulator.client import DeliveryResult
from simulator.generator import GeneratedResult

# Fixed reasons, chosen by status code only (never taken from the response body).
_REJECTED_REASONS = {401: "invalid or missing API key", 422: "payload failed validation"}


@dataclass(frozen=True)
class BatchSummary:
    """Counts of one batch. ``passed``/``failed`` count accepted results only."""

    sent: int
    accepted: int
    rejected: int
    undelivered: int
    passed: int
    failed: int

    def __post_init__(self) -> None:
        if min(self.sent, self.accepted, self.rejected, self.undelivered,
               self.passed, self.failed) < 0:
            raise ValueError("counts must not be negative")
        if self.sent != self.accepted + self.rejected + self.undelivered:
            raise ValueError("sent must equal accepted + rejected + undelivered")
        if self.accepted != self.passed + self.failed:
            raise ValueError("accepted must equal passed + failed")


def _limit_decimals(limits: list[float]) -> int:
    """The fewest decimals (0, 1 or 2) that show every limit exactly, e.g. 0.01/0.40 -> 2."""
    for decimals in (0, 1, 2):
        if all(round(limit, decimals) == limit for limit in limits):
            return decimals
    raise ValueError("limits need more than 2 decimals")


def format_limits(limit_min: float | None, limit_max: float | None, unit: str) -> str:
    """E.g. ``0.01–0.40 mA`` (en dash), ``≤ 150 ms`` or ``≥ 5.5 V``."""
    given = [limit for limit in (limit_min, limit_max) if limit is not None]
    if not given:
        raise ValueError("at least one limit is required")
    decimals = _limit_decimals(given)  # one shared precision for both limits of a test

    def number(limit: float) -> str:
        return f"{limit:.{decimals}f}"

    if limit_min is not None and limit_max is not None:
        return f"{number(limit_min)}–{number(limit_max)} {unit}"
    if limit_max is not None:
        return f"≤ {number(limit_max)} {unit}"
    return f"≥ {number(limit_min)} {unit}"


def _prefix(result: GeneratedResult) -> str:
    return f"{result.payload['device_id']} | Test: {result.payload['test_name']}"


def format_result_line(result: GeneratedResult) -> str:
    """Line for an accepted result; limits are appended only on FAIL."""
    payload, spec = result.payload, result.spec
    line = (
        f"{_prefix(result)} | Temperature: {payload['temperature_c']:.0f}°C"
        f" | Measured {spec.quantity}: {payload['measured_value']:.2f} {spec.unit}"
        f" | Result: {payload['verdict']}"
    )
    if payload["verdict"] == "FAIL":
        line += f" (limits: {format_limits(spec.limit_min, spec.limit_max, spec.unit)})"
    return line


def format_rejected_line(result: GeneratedResult, delivery: DeliveryResult) -> str:
    """E.g. ``… | REJECTED: HTTP 401 (invalid or missing API key)``."""
    if delivery.outcome != "rejected" or delivery.status_code is None:
        raise ValueError("a rejected delivery with a status code is required")
    reason = _REJECTED_REASONS.get(delivery.status_code, "unexpected response")
    return f"{_prefix(result)} | REJECTED: HTTP {delivery.status_code} ({reason})"


def format_undelivered_line(result: GeneratedResult, delivery: DeliveryResult) -> str:
    """E.g. ``… | UNDELIVERED: ConnectError after 3 attempts`` (spec §8.4)."""
    if delivery.outcome != "undelivered":
        raise ValueError("an undelivered delivery is required")
    if delivery.detail == "http" and delivery.status_code is not None:
        reason = f"HTTP {delivery.status_code} after {delivery.attempts} attempts"
    elif delivery.detail == "connection" and delivery.exception_name:
        reason = f"{delivery.exception_name} after {delivery.attempts} attempts"
    elif delivery.detail == "after_send" and delivery.exception_name:
        reason = f"{delivery.exception_name}, not retried (may have been stored)"
    elif delivery.detail == "shutdown":
        reason = "shutdown requested before retry"
    else:
        raise ValueError("undelivered delivery is missing its detail, status or exception")
    return f"{_prefix(result)} | UNDELIVERED: {reason}"


def format_summary_line(summary: BatchSummary) -> str:
    """E.g. ``Batch done: 10 sent | 8 accepted | 1 rejected | 1 undelivered | 7 PASS | 1 FAIL``."""
    return (
        f"Batch done: {summary.sent} sent | {summary.accepted} accepted"
        f" | {summary.rejected} rejected | {summary.undelivered} undelivered"
        f" | {summary.passed} PASS | {summary.failed} FAIL"
    )
