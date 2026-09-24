"""Prometheus metrics of the Results API (docs/APP_SPEC.md §2.6, §5.4).

Each application instance gets its own ``CollectorRegistry`` (a container for metrics).
``/metrics`` shows exactly this registry, and tests get fresh counters per app.

Assumes one uvicorn worker process per container: all counters live in this process's
memory. With several workers, each would count separately and a scrape would only see one
of them (that would need prometheus_client's multiprocess mode, which we do not use).

Labels are kept bounded: only ``test_name``, ``verdict`` (PASS/FAIL), a fixed set of
rejection reasons, and the HTTP metrics' route template / method / status code. Never
device IDs, raw URLs, error messages, API keys or request bodies.
"""

from fastapi import FastAPI
from prometheus_client import CollectorRegistry, Counter
from prometheus_fastapi_instrumentator import Instrumentator

REJECT_AUTH = "auth"
REJECT_VALIDATION = "validation"
REJECT_REASONS = (REJECT_AUTH, REJECT_VALIDATION)

# Probe and scrape traffic is left out of the standard HTTP metrics (exact route matches).
EXCLUDED_HANDLERS = ["^/health$", "^/ready$", "^/metrics$"]


class ResultsMetrics:
    """The custom §5.4 counters, registered in the given registry."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.registry = registry
        self.results_received = Counter(
            "evp_results_received_total",
            "Results stored successfully",
            ["test_name", "verdict"],
            registry=registry,
        )
        self.results_rejected = Counter(
            "evp_results_rejected_total",
            "POST /api/v1/results requests rejected before storing",
            ["reason"],
            registry=registry,
        )
        # Create each reason with value 0, so it appears in /metrics before the first rejection.
        for reason in REJECT_REASONS:
            self.results_rejected.labels(reason=reason)

    def result_stored(self, test_name: str, verdict: str) -> None:
        self.results_received.labels(test_name=test_name, verdict=verdict).inc()

    def result_rejected(self, reason: str) -> None:
        if reason not in REJECT_REASONS:
            raise ValueError(f"unknown rejection reason: {reason}")
        self.results_rejected.labels(reason=reason).inc()


def setup_metrics(app: FastAPI) -> ResultsMetrics:
    """Create the registry, the custom counters, the HTTP metrics and ``GET /metrics``."""
    registry = CollectorRegistry()
    Instrumentator(
        should_group_status_codes=False,  # keep 401, 422, 503 ... apart (still a bounded set)
        excluded_handlers=EXCLUDED_HANDLERS,
        registry=registry,
    ).instrument(app).expose(app, endpoint="/metrics", tags=["operations"])
    return ResultsMetrics(registry)
