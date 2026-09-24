"""Readiness, database-unavailable handling and metrics against real PostgreSQL."""

import socket
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import Result
from tests.integration.support import INTEGRATION_API_KEY
from tests.unit.fakes import URL, valid_body

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

HEADERS = {"X-API-Key": INTEGRATION_API_KEY}


def count_rows(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(Result.__table__)).scalar_one()


def closed_local_port() -> int:
    """A localhost port with nothing listening: bind to a free port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_ready_returns_200_with_real_database(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unreachable_database_gives_503_without_leaking_details(client, clean_db, test_settings):
    client.post(URL, json=valid_body(), headers=HEADERS)  # one real row in the test database
    assert count_rows(clean_db) == 1

    port = closed_local_port()
    unreachable = test_settings.model_copy(update={"db_host": "127.0.0.1", "db_port": port})

    started = time.monotonic()
    with TestClient(create_app(unreachable)) as broken:
        ready = broken.get("/ready")
        post = broken.post(URL, json=valid_body(device_id="ECU-999"), headers=HEADERS)
    elapsed = time.monotonic() - started

    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable"}
    assert post.status_code == 503
    assert post.json() == {"detail": "Database unavailable"}
    for response in (ready, post):
        for secret in ("127.0.0.1", str(port), test_settings.db_user, test_settings.db_name,
                       test_settings.db_password.get_secret_value(), "SELECT", "INSERT",
                       "psycopg2", "OperationalError", "refused"):
            assert secret not in response.text, secret
    assert elapsed < 30  # bounded; a closed local port usually fails at once

    # The real test database is untouched.
    assert count_rows(clean_db) == 1


def test_received_counter_increases_after_real_insert(client, clean_db):
    registry = client.app.state.metrics.registry
    labels = {"test_name": "Wake-up Time", "verdict": "FAIL"}
    before = registry.get_sample_value("evp_results_received_total", labels) or 0.0

    response = client.post(
        URL, json=valid_body(test_name="Wake-up Time", verdict="FAIL"), headers=HEADERS
    )

    assert response.status_code == 201
    assert count_rows(clean_db) == 1
    assert registry.get_sample_value("evp_results_received_total", labels) == before + 1
    assert (
        'evp_results_received_total{test_name="Wake-up Time",verdict="FAIL"} 1.0'
        in client.get("/metrics").text
    )
