"""Shared test helpers. No network, no database, no external services."""

import os

import httpx

from simulator.config import Settings

API_URL = "http://results-api:8001"
API_KEY = "super-secret-key"
# Text that must never appear in logs, output or results.
SERVER_CANARY = "SERVER-SECRET-MUST-NOT-LEAK"
EXCEPTION_CANARY = "API-KEY-OR-URL-MUST-NOT-LEAK"

# Every environment variable the simulator Settings read (field names in upper case).
SETTINGS_ENV_VARS = frozenset(name.upper() for name in Settings.model_fields)


def clear_settings_env(monkeypatch) -> None:
    """Remove every simulator setting from the environment, in any upper/lower-case spelling.

    pydantic-settings matches variable names case-insensitively, so e.g. ``sim_mode`` would
    also be read. After this, a developer's shell (e.g. a sourced ``.env``) cannot affect tests.
    """
    for name in list(os.environ):
        if name.upper() in SETTINGS_ENV_VARS:
            monkeypatch.delenv(name, raising=False)


def make_settings(**overrides) -> Settings:
    """Build valid Settings directly (without environment variables)."""
    values = {"results_api_url": API_URL, "results_api_key": API_KEY}
    values.update(overrides)
    return Settings(**values)


def scripted_client(*outcomes, **client_kwargs) -> tuple[httpx.Client, list[httpx.Request]]:
    """An httpx.Client that answers each request with the next scripted outcome, no network.

    An ``int`` becomes a response with that status code (body: ``SERVER_CANARY``), an
    ``httpx.Response`` is returned as it is, and an exception class is raised with
    ``EXCEPTION_CANARY`` as its message. Every request is recorded in the returned list.
    """
    remaining = list(outcomes)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        outcome = remaining.pop(0)
        if isinstance(outcome, int):
            return httpx.Response(outcome, text=SERVER_CANARY)
        if isinstance(outcome, httpx.Response):
            return outcome
        raise outcome(EXCEPTION_CANARY)

    return httpx.Client(transport=httpx.MockTransport(handler), **client_kwargs), requests


class RecordingWait:
    """Fake retry wait: records each delay and returns immediately (never sleeps).

    Returns the scripted answers in order (``True`` = stop requested), then ``False``.
    """

    def __init__(self, *answers: bool) -> None:
        self.delays: list[float] = []
        self._answers = list(answers)

    def __call__(self, delay_seconds: float) -> bool:
        self.delays.append(delay_seconds)
        return self._answers.pop(0) if self._answers else False
