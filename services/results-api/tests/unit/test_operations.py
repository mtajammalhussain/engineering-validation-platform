"""/health, /ready, /metrics and the §5.4 counters (no database, no network).

Every test gets a new app from the ``client`` fixture, and every app has its own
Prometheus registry. Counters therefore start at 0 in each test, and the order in which
tests run does not matter.

/ready is tested against a fake session: this proves the endpoint's logic, not that real
PostgreSQL answers (that is covered by the integration tests).
"""

import logging

import pytest
from prometheus_client import CONTENT_TYPE_LATEST

import app.main
from app.main import create_app
from app.metrics import ResultsMetrics
from tests.unit.fakes import (
    API_KEY,
    UNAVAILABLE_ERRORS,
    UNEXPECTED_ERRORS,
    URL,
    make_settings,
    valid_body,
)

ALL_DB_ERRORS = {**UNAVAILABLE_ERRORS, **UNEXPECTED_ERRORS}
ALLOWED_LABEL_NAMES = {"test_name", "verdict", "reason", "handler", "method", "status", "le"}


def registry(client):
    return client.app.state.metrics.registry


def value(client, name: str, **labels) -> float:
    """Current value of one metric sample; 0 if that label combination does not exist yet."""
    return registry(client).get_sample_value(name, labels) or 0.0


def total_received(client) -> float:
    return sum(
        sample.value
        for metric in registry(client).collect()
        if metric.name == "evp_results_received"
        for sample in metric.samples
        if sample.name == "evp_results_received_total"
    )


def all_samples(client):
    return [sample for metric in registry(client).collect() for sample in metric.samples]


def http_handlers(client) -> set[str]:
    return {
        sample.labels["handler"]
        for sample in all_samples(client)
        if sample.name == "http_requests_total"
    }


def post(client, body=None, key=API_KEY):
    headers = {"X-API-Key": key} if key is not None else {}
    return client.post(URL, json=body if body is not None else valid_body(), headers=headers)


# --- /health ---------------------------------------------------------------------------

def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_touch_the_database(client):
    def no_database():
        pytest.fail("/health must not open a database session")

    client.app.state.session_factory = no_database

    assert client.get("/health").status_code == 200


# --- /ready ----------------------------------------------------------------------------

def test_ready_returns_ok_when_select_1_succeeds(client, session):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert [str(statement) for statement in session.statements] == ["SELECT 1"]
    assert session.closed


@pytest.mark.parametrize("make_error", ALL_DB_ERRORS.values(), ids=ALL_DB_ERRORS.keys())
def test_ready_returns_503_when_database_fails(client, session, caplog, make_error):
    session.error = make_error()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    for secret in ("db.example.invalid", "SELECT", "INSERT", "db-password-must-not-leak"):
        assert secret not in response.text
    # One short warning with the error class only; details and traceback stay out.
    assert f"Readiness check failed: {type(session.error).__name__}" in caplog.text
    assert "db.example.invalid" not in caplog.text
    assert session.closed


# --- /metrics --------------------------------------------------------------------------

def test_metrics_endpoint_uses_prometheus_format(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "# TYPE evp_results_rejected_total counter" in response.text
    assert "# TYPE evp_results_received_total counter" in response.text


def test_rejection_reasons_are_visible_with_zero_before_first_rejection(client):
    text = client.get("/metrics").text

    assert 'evp_results_rejected_total{reason="auth"} 0.0' in text
    assert 'evp_results_rejected_total{reason="validation"} 0.0' in text


def test_standard_http_metrics_are_recorded(client):
    client.get(URL)

    assert value(
        client, "http_requests_total", handler=URL, method="GET", status="200"
    ) == 1
    assert value(
        client, "http_request_duration_seconds_count", handler=URL, method="GET"
    ) == 1


# --- evp_results_received_total -----------------------------------------------------------

def test_successful_post_increments_received_counter(client):
    post(client)
    post(client, valid_body(test_name="Wake-up Time", verdict="FAIL"))
    post(client, valid_body(test_name="Wake-up Time", verdict="FAIL"))

    name = "evp_results_received_total"
    assert value(client, name, test_name="Sleep Current", verdict="PASS") == 1
    assert value(client, name, test_name="Wake-up Time", verdict="FAIL") == 2
    assert 'test_name="Wake-up Time",verdict="FAIL"} 2.0' in client.get("/metrics").text


@pytest.mark.parametrize("make_error", ALL_DB_ERRORS.values(), ids=ALL_DB_ERRORS.keys())
def test_failed_database_write_is_not_counted(client, session, make_error):
    session.error = make_error()

    post(client)

    assert total_received(client) == 0
    assert value(client, "evp_results_rejected_total", reason="auth") == 0
    assert value(client, "evp_results_rejected_total", reason="validation") == 0


# --- evp_results_rejected_total ----------------------------------------------------------

@pytest.mark.parametrize("key", [None, "wrong-key"], ids=["missing", "wrong"])
def test_auth_rejection_is_counted(client, key):
    assert post(client, key=key).status_code == 401

    assert value(client, "evp_results_rejected_total", reason="auth") == 1
    assert value(client, "evp_results_rejected_total", reason="validation") == 0
    assert total_received(client) == 0


@pytest.mark.parametrize(
    "body",
    [valid_body(verdict="OK"), valid_body(device_id="ECU-1"), valid_body(unknown=1), {}],
    ids=["verdict", "device_id", "unknown-field", "empty"],
)
def test_validation_rejection_is_counted(client, body):
    assert post(client, body).status_code == 422

    assert value(client, "evp_results_rejected_total", reason="validation") == 1
    assert value(client, "evp_results_rejected_total", reason="auth") == 0
    assert total_received(client) == 0


def test_non_json_body_is_counted_as_validation(client):
    response = client.post(
        URL, content="not json", headers={"X-API-Key": API_KEY, "Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert value(client, "evp_results_rejected_total", reason="validation") == 1


def test_unauthenticated_invalid_body_is_counted_once_as_auth(client):
    assert post(client, {"verdict": "OK"}, key=None).status_code == 401

    assert value(client, "evp_results_rejected_total", reason="auth") == 1
    assert value(client, "evp_results_rejected_total", reason="validation") == 0


def test_invalid_get_query_is_not_counted_as_rejected_result(client):
    assert client.get(URL, params={"limit": 999}).status_code == 422
    assert client.get(f"{URL}/abc").status_code == 422

    assert value(client, "evp_results_rejected_total", reason="validation") == 0


def test_unknown_rejection_reason_is_refused():
    from prometheus_client import CollectorRegistry

    metrics = ResultsMetrics(CollectorRegistry())

    with pytest.raises(ValueError):
        metrics.result_rejected("device ECU-003 said no")


# --- label hygiene -----------------------------------------------------------------------

def test_labels_contain_no_secrets_or_unbounded_values(client):
    post(client, valid_body(device_id="ECU-042", source="HIL-secret-bench"))
    post(client, key="attacker-guess-123")
    post(client, valid_body(device_id="ECU-4242"))
    client.get(URL, params={"device_id": "ECU-042", "from": "2026-09-16T00:00:00Z"})
    client.get(f"{URL}/12345")
    client.get("/some/random/path/67890")

    samples = all_samples(client)
    label_names = {name for sample in samples for name in sample.labels}
    label_values = {value for sample in samples for value in sample.labels.values()}

    assert label_names <= ALLOWED_LABEL_NAMES
    for forbidden in (
        API_KEY, "attacker-guess-123", "ECU-042", "ECU-4242", "HIL-secret-bench",
        "12345", "67890", "/some/random/path", "2026-09-16", "?", "db.example.invalid",
    ):
        assert not any(forbidden in label for label in label_values), forbidden
    # Route templates, not concrete URLs:
    assert http_handlers(client) >= {URL, URL + "/{result_id}"}


# --- probe and scrape traffic -----------------------------------------------------------

def test_probe_and_scrape_endpoints_are_excluded_from_http_metrics(client):
    for _ in range(3):
        client.get("/health")
        client.get("/ready")
        client.get("/metrics")
    client.get(URL)

    assert http_handlers(client) == {URL}
    # The latency histogram without a handler label only counts the one real request.
    assert value(client, "http_request_duration_highr_seconds_count") == 1


# --- isolation between app instances -------------------------------------------------------

def test_each_app_has_its_own_metrics(monkeypatch):
    monkeypatch.setattr(app.main, "configure_logging", lambda settings: None)
    first, second = create_app(make_settings()), create_app(make_settings())

    first.state.metrics.result_rejected("auth")

    assert first.state.metrics.registry is not second.state.metrics.registry
    assert second.state.metrics.registry.get_sample_value(
        "evp_results_rejected_total", {"reason": "auth"}
    ) == 0


def test_metrics_do_not_use_the_global_default_registry(client):
    from prometheus_client import REGISTRY

    post(client)

    assert REGISTRY.get_sample_value(
        "evp_results_received_total", {"test_name": "Sleep Current", "verdict": "PASS"}
    ) is None


def test_log_output_unaffected(client, caplog):
    caplog.set_level(logging.INFO)

    post(client)

    assert "Result stored" in caplog.text
