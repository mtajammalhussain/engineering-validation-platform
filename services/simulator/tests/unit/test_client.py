import copy
import json
import logging
import random
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import httpx
import pytest

import simulator.client
from simulator.client import DeliveryResult, send_result
from simulator.generator import generate_result
from tests.unit.fakes import (
    API_KEY,
    API_URL,
    EXCEPTION_CANARY,
    SERVER_CANARY,
    RecordingWait,
    make_settings,
    scripted_client,
)

RESULTS_URL = API_URL + "/api/v1/results"
PAYLOAD = generate_result(
    random.Random(1), datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
    device_count=20, failure_rate=0.08,
).payload

RETRYABLE = [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
AFTER_SEND = [
    httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError,
    httpx.RemoteProtocolError,
]


def send(*outcomes, wait=None, payload=PAYLOAD, **client_kwargs):
    """Send PAYLOAD through a scripted client; return (result, requests, wait)."""
    client, requests = scripted_client(*outcomes, **client_kwargs)
    wait = wait or RecordingWait()
    result = send_result(client, make_settings(), payload, wait=wait)
    return result, requests, wait


# --- accepted ---

def test_201_is_accepted_on_first_attempt():
    result, requests, wait = send(201)

    assert result == DeliveryResult("accepted", 1, status_code=201)
    assert len(requests) == 1
    assert wait.delays == []


# --- rejected: every non-201, non-5xx status, no retry ---

@pytest.mark.parametrize("status", [100, 200, 202, 204, 301, 302, 400, 401, 404, 409, 422, 429])
def test_non_retryable_status_is_rejected_immediately(status):
    result, requests, wait = send(status)

    assert result == DeliveryResult("rejected", 1, status_code=status, detail="http")
    assert len(requests) == 1
    assert wait.delays == []


def test_redirect_is_not_followed_even_if_the_client_would_follow_it():
    redirect = httpx.Response(
        302, headers={"Location": "https://redirect.example.invalid/should-not-be-requested"}
    )

    result, requests, _ = send(redirect, follow_redirects=True)  # conflicting client default

    assert result == DeliveryResult("rejected", 1, status_code=302, detail="http")
    assert [str(r.url) for r in requests] == [RESULTS_URL]


# --- 5xx: retried, 3 attempts in total ---

@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_5xx_is_retried(status):
    result, requests, wait = send(status, 201)

    assert result == DeliveryResult("accepted", 2, status_code=201)
    assert len(requests) == 2
    assert wait.delays == [1]


def test_accepted_on_third_attempt():
    result, requests, wait = send(503, 503, 201)

    assert result == DeliveryResult("accepted", 3, status_code=201)
    assert len(requests) == 3
    assert wait.delays == [1, 2]


def test_5xx_on_every_attempt_is_undelivered_after_three():
    result, requests, wait = send(503, 502, 500)

    assert result == DeliveryResult("undelivered", 3, status_code=500, detail="http")
    assert len(requests) == 3
    assert wait.delays == [1, 2]  # no wait after the third attempt


def test_rejection_on_a_retry_stops_immediately():
    result, requests, wait = send(503, 401)

    assert result == DeliveryResult("rejected", 2, status_code=401, detail="http")
    assert len(requests) == 2
    assert wait.delays == [1]


# --- connection failures: retried ---

@pytest.mark.parametrize("error", RETRYABLE)
def test_connection_failure_is_retried_until_success(error):
    result, requests, wait = send(error, 201)

    assert result == DeliveryResult("accepted", 2, status_code=201)
    assert len(requests) == 2
    assert wait.delays == [1]


@pytest.mark.parametrize("error", RETRYABLE)
def test_connection_failure_on_every_attempt_is_undelivered(error):
    result, requests, wait = send(error, error, error)

    assert result == DeliveryResult(
        "undelivered", 3, exception_name=error.__name__, detail="connection"
    )
    assert len(requests) == 3
    assert wait.delays == [1, 2]


def test_final_result_describes_the_last_failure():
    result, _, _ = send(httpx.ConnectError, 503, httpx.PoolTimeout)
    assert result == DeliveryResult(
        "undelivered", 3, exception_name="PoolTimeout", detail="connection"
    )

    result, _, _ = send(httpx.ConnectError, httpx.ConnectTimeout, 504)
    assert result == DeliveryResult("undelivered", 3, status_code=504, detail="http")


# --- failures after sending: never retried ---

@pytest.mark.parametrize("error", AFTER_SEND)
def test_failure_after_sending_is_not_retried(error):
    result, requests, wait = send(error)

    assert result == DeliveryResult(
        "undelivered", 1, exception_name=error.__name__, detail="after_send"
    )
    assert len(requests) == 1
    assert wait.delays == []


def test_failure_after_sending_on_a_retry():
    result, requests, wait = send(503, httpx.ReadTimeout)

    assert result == DeliveryResult(
        "undelivered", 2, exception_name="ReadTimeout", detail="after_send"
    )
    assert len(requests) == 2
    assert wait.delays == [1]


# --- unexpected exceptions are not classified ---

@pytest.mark.parametrize("error", [RuntimeError, httpx.UnsupportedProtocol, httpx.ProxyError])
def test_unexpected_exception_propagates(error):
    with pytest.raises(error):
        send(error)


# --- shutdown before a retry ---

def test_shutdown_during_first_backoff_after_5xx():
    result, requests, wait = send(503, wait=RecordingWait(True))

    assert result == DeliveryResult("undelivered", 1, status_code=503, detail="shutdown")
    assert len(requests) == 1
    assert wait.delays == [1]


def test_shutdown_during_first_backoff_after_connection_failure():
    result, requests, _ = send(httpx.ConnectError, wait=RecordingWait(True))

    assert result == DeliveryResult("undelivered", 1, detail="shutdown")
    assert result.status_code is None and result.exception_name is None
    assert len(requests) == 1


def test_shutdown_during_second_backoff():
    result, requests, wait = send(503, 503, wait=RecordingWait(False, True))

    assert result == DeliveryResult("undelivered", 2, status_code=503, detail="shutdown")
    assert len(requests) == 2
    assert wait.delays == [1, 2]


def test_default_wait_sleeps_the_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(simulator.client.time, "sleep", slept.append)
    client, _ = scripted_client(503, 503, 201)

    result = send_result(client, make_settings(), PAYLOAD)

    assert result.outcome == "accepted"
    assert slept == [1, 2]


# --- the request itself ---

def test_request_url_header_and_body():
    _, requests, _ = send(201)
    request = requests[0]

    assert request.method == "POST"
    assert str(request.url) == RESULTS_URL
    assert request.headers["X-API-Key"] == API_KEY
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == PAYLOAD


def test_api_key_is_only_in_the_header():
    _, requests, _ = send(201)
    request = requests[0]

    assert API_KEY not in str(request.url)
    assert API_KEY not in request.content.decode()
    assert [name for name, value in request.headers.items() if API_KEY in value] == ["x-api-key"]


def test_payload_is_sent_unchanged_and_not_mutated():
    payload = copy.deepcopy(PAYLOAD)

    _, requests, _ = send(503, 201, payload=payload)

    assert payload == PAYLOAD
    assert all(json.loads(request.content) == PAYLOAD for request in requests)
    assert "quantity" not in json.loads(requests[0].content)


def test_per_request_timeout_overrides_the_client_default():
    _, requests, _ = send(201, timeout=httpx.Timeout(99.0))  # conflicting client default

    assert requests[0].extensions["timeout"] == {
        "connect": 3.0, "read": 10.0, "write": 10.0, "pool": 10.0,
    }


def test_injected_client_is_not_closed():
    client, requests = scripted_client(201, 201)

    send_result(client, make_settings(), PAYLOAD, wait=RecordingWait())

    assert not client.is_closed
    assert client.post(RESULTS_URL).status_code == 201  # still usable
    assert len(requests) == 2


# --- DeliveryResult ---

def test_delivery_result_is_frozen():
    result = DeliveryResult("accepted", 1, status_code=201)

    with pytest.raises(FrozenInstanceError):
        result.outcome = "rejected"


# --- retry logging and secrecy ---

def test_one_warning_per_retry_that_actually_happens(caplog):
    caplog.set_level(logging.WARNING, logger="simulator.client")

    send(503, httpx.ConnectError, httpx.ConnectTimeout)

    messages = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    assert messages == [
        ("simulator.client", "WARNING", "Retrying result delivery after HTTP 503 (attempt 1/3)"),
        ("simulator.client", "WARNING",
         "Retrying result delivery after ConnectError (attempt 2/3)"),
    ]


def own_records(caplog):
    """Records of the simulator's own loggers (httpx's lines are handled by logging_config)."""
    return [r for r in caplog.records if r.name.startswith("simulator")]


def test_no_retry_warning_when_shutdown_cancels_the_retry(caplog):
    caplog.set_level(logging.DEBUG)

    send(503, wait=RecordingWait(True))

    assert own_records(caplog) == []


@pytest.mark.parametrize("outcomes", [(201,), (401,), (httpx.ReadError,)])
def test_no_logging_without_a_retry(caplog, outcomes):
    caplog.set_level(logging.DEBUG, logger="simulator")

    send(*outcomes)

    assert own_records(caplog) == []


@pytest.mark.parametrize(
    "outcomes",
    [
        (503, 503, 503),
        (401,),
        (201,),
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
        (500, httpx.ReadTimeout),
        (httpx.RemoteProtocolError,),
    ],
)
def test_no_secret_leaks_into_logs_output_or_result(caplog, capsys, outcomes):
    caplog.set_level(logging.DEBUG)  # everything, including httpx's own DEBUG/INFO lines

    result, _, _ = send(*outcomes)

    captured = capsys.readouterr()
    everything = caplog.text + captured.out + captured.err + repr(result)
    for secret in (SERVER_CANARY, EXCEPTION_CANARY, API_KEY):
        assert secret not in everything
    # Our own messages do not contain the URL either (httpx's INFO line does, but
    # logging_config keeps httpx at WARNING or stricter in the real process).
    own_text = " ".join(r.getMessage() for r in own_records(caplog))
    assert RESULTS_URL not in own_text + repr(result)
