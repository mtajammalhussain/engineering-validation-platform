"""Fixtures shared by the unit tests (pytest loads this file automatically)."""

import pytest

from tests.unit.fakes import clear_settings_env


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test without any simulator variable from the developer's shell."""
    clear_settings_env(monkeypatch)
