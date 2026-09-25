"""Shared test helpers. No network, no database, no external services."""

import os

from simulator.config import Settings

API_URL = "http://results-api:8001"
API_KEY = "super-secret-key"

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
