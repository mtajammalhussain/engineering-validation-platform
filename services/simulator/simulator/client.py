"""Deliver one result to the Results API and classify the outcome (docs/APP_SPEC.md §8.3.2).

Only transport: the payload is sent exactly as generated. Formatting the REJECTED /
UNDELIVERED lines is done elsewhere, from the ``DeliveryResult`` returned here.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx

from simulator.config import Settings

logger = logging.getLogger(__name__)

RESULTS_PATH = "/api/v1/results"
MAX_ATTEMPTS = 3  # in total: 1 initial attempt + 2 retries
BACKOFF_SECONDS = (1, 2)  # wait before the 2nd and before the 3rd attempt; no random jitter

# Passed with every request, so a differently configured httpx.Client cannot change it.
# Read (10 s) is longer than the Results API's 3 s database connect timeout, so a database
# outage arrives as HTTP 503 instead of a read timeout.
HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=10.0)

# The connection was never established: the request did not reach the server -> safe to retry.
RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# The request may already have reached the server and been stored. POST has no idempotency
# key, so a retry could store the result twice -> never retried.
AFTER_SEND_ERRORS = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.WriteTimeout,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
# Any other exception is a bug or an unexpected state: it is not caught here.


@dataclass(frozen=True)
class DeliveryResult:
    """What happened to one result. Holds only safe metadata: no body, URL, key or payload.

    ``detail`` says why a result was rejected or undelivered: ``"http"`` (a status code),
    ``"connection"`` (connection failed on every attempt), ``"after_send"`` (failure after the
    request may have been sent) or ``"shutdown"`` (stop requested before a retry).
    """

    outcome: Literal["accepted", "rejected", "undelivered"]
    attempts: int
    status_code: int | None = None
    exception_name: str | None = None
    detail: Literal["http", "connection", "after_send", "shutdown"] | None = None


def _sleep(delay_seconds: float) -> bool:
    """Default wait: sleep, then report "not stopped" (same meaning as Event.wait)."""
    time.sleep(delay_seconds)
    return False


def send_result(
    client: httpx.Client,
    settings: Settings,
    payload: dict[str, object],
    *,
    wait: Callable[[float], bool] = _sleep,
) -> DeliveryResult:
    """POST one result, retrying connection failures and 5xx responses (at most 3 attempts).

    ``wait(seconds)`` is called before each retry. It returns ``True`` if a shutdown was
    requested in the meantime (like ``threading.Event.wait``); then no further attempt is made.
    The caller owns ``client`` and closes it; this function never does.
    """
    url = settings.results_api_url + RESULTS_PATH
    headers = {"X-API-Key": settings.results_api_key.get_secret_value()}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(
                url, json=payload, headers=headers,
                timeout=HTTP_TIMEOUT, follow_redirects=False,
            )
        except RETRYABLE_ERRORS as exc:
            # Only the class name is kept: the exception text may contain the URL.
            reason = type(exc).__name__
            failure = DeliveryResult(
                "undelivered", attempt, exception_name=reason, detail="connection"
            )
        except AFTER_SEND_ERRORS as exc:
            return DeliveryResult(
                "undelivered", attempt, exception_name=type(exc).__name__, detail="after_send"
            )
        else:
            # Classified by status code only; the response body is never used.
            status = response.status_code
            if status == 201:
                return DeliveryResult("accepted", attempt, status_code=status)
            if not 500 <= status <= 599:
                return DeliveryResult("rejected", attempt, status_code=status, detail="http")
            reason = f"HTTP {status}"
            failure = DeliveryResult("undelivered", attempt, status_code=status, detail="http")

        if attempt == MAX_ATTEMPTS:
            return failure
        if wait(BACKOFF_SECONDS[attempt - 1]):
            return DeliveryResult(
                "undelivered", attempt, status_code=failure.status_code, detail="shutdown"
            )
        logger.warning(
            "Retrying result delivery after %s (attempt %d/%d)", reason, attempt, MAX_ATTEMPTS
        )
    raise AssertionError("unreachable: the last attempt always returns")
