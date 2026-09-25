"""Prometheus metrics of the Reporting API (docs/APP_SPEC.md §2.6, §7.4).

Only the standard HTTP metrics (request count, latency, status code per route); there are
no Reporting-specific custom counters.

Each application instance gets its own ``CollectorRegistry`` (a container for metrics).
``/metrics`` shows exactly this registry, and tests get fresh metrics per app.

Assumes one uvicorn worker process per container: all metrics live in this process's
memory. With several workers, each would count separately and a scrape would only see one
of them (that would need prometheus_client's multiprocess mode, which we do not use).

Labels are kept bounded: route template (not the raw URL), method and status code. Never
device IDs, test names, query values or error messages.
"""

from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator

# Probe and scrape traffic is left out of the standard HTTP metrics (exact route matches).
EXCLUDED_HANDLERS = ["^/health$", "^/ready$", "^/metrics$"]


def setup_metrics(app: FastAPI) -> CollectorRegistry:
    """Create the registry, the HTTP metrics middleware and ``GET /metrics``."""
    registry = CollectorRegistry()
    Instrumentator(
        should_group_status_codes=False,  # keep 422, 500, 503 ... apart (still a bounded set)
        excluded_handlers=EXCLUDED_HANDLERS,
        registry=registry,
    ).instrument(app).expose(app, endpoint="/metrics", tags=["operations"])
    return registry
