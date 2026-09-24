"""Fixtures shared by the unit tests (pytest loads this file automatically)."""

import pytest
from fastapi.testclient import TestClient

import app.main
from app.main import create_app
from tests.unit.fakes import FakeSession, make_settings


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(session, monkeypatch) -> TestClient:
    """TestClient for a fresh app whose sessions come from the FakeSession.

    Used without ``with``, so the lifespan (real engine) does not run. The real get_db
    dependency still runs, so closing the session is tested too. Every call creates a new
    app with its own metrics registry, so counters start at 0 in each test.
    """
    monkeypatch.setattr(app.main, "configure_logging", lambda settings: None)
    application = create_app(make_settings())
    application.state.session_factory = lambda: session
    return TestClient(application)
