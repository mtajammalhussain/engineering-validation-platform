import pytest
from sqlalchemy import Engine, event, make_url

import app.db
from app.db import (
    DB_CONNECT_TIMEOUT_S,
    DB_READ_ONLY_OPTIONS,
    build_database_url,
    create_db_engine,
    create_session_factory,
)

SPECIAL_PASSWORD = "p@ss:w/rd"
EXPECTED_CONNECT_ARGS = {
    "connect_timeout": 3,
    "options": "-c default_transaction_read_only=on",
}


class StopBeforeConnecting(Exception):
    """Raised by the test listener so that no real connection is attempted."""


@pytest.fixture
def settings(make_settings):
    return make_settings(db_password=SPECIAL_PASSWORD)


def test_url_contains_all_parts_from_settings(settings):
    url = build_database_url(settings)

    assert url.drivername == "postgresql+psycopg2"
    assert url.username == "evp_reader"
    assert url.host == "db.example.invalid"
    assert url.port == 5432
    assert url.database == "evp"


def test_url_carries_no_query_options(settings):
    # The read-only setting travels in connect_args, not in the URL.
    assert build_database_url(settings).query == {}


def test_special_characters_in_password_are_preserved(settings):
    url = build_database_url(settings)

    assert url.password == SPECIAL_PASSWORD
    # Written out as a full string, the special characters are percent-encoded ...
    full = url.render_as_string(hide_password=False)
    assert "p%40ss%3Aw%2Frd" in full
    # ... and parsing that string back gives the original password.
    assert make_url(full).password == SPECIAL_PASSWORD


def test_normal_string_representation_masks_password(settings):
    url = build_database_url(settings)

    for text in (str(url), repr(url)):
        assert SPECIAL_PASSWORD not in text
        assert "p%40ss%3Aw%2Frd" not in text
        assert "***" in text


def test_driver_option_constants():
    assert DB_CONNECT_TIMEOUT_S == 3
    assert DB_READ_ONLY_OPTIONS == "-c default_transaction_read_only=on"


def test_engine_gets_pre_ping_and_both_driver_options(settings, monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"], captured["kwargs"] = url, kwargs
        return "fake-engine"

    monkeypatch.setattr(app.db, "create_engine", fake_create_engine)

    engine = create_db_engine(settings)

    assert engine == "fake-engine"
    assert captured["kwargs"]["pool_pre_ping"] is True
    # Exact equality: both keys present, neither overwritten, nothing else added.
    assert captured["kwargs"]["connect_args"] == EXPECTED_CONNECT_ARGS
    assert captured["url"].password == SPECIAL_PASSWORD


def test_real_engine_hands_both_options_to_the_driver(settings):
    """Capture what the real engine would pass to psycopg2.connect(), then stop.

    The "do_connect" event runs right before the driver is called; raising there means
    no network connection is ever attempted.
    """
    engine = create_db_engine(settings)
    captured = {}

    @event.listens_for(engine, "do_connect")
    def capture(dialect, conn_rec, cargs, cparams):
        captured.update(cparams)
        raise StopBeforeConnecting

    try:
        with pytest.raises(StopBeforeConnecting):
            engine.connect()
    finally:
        engine.dispose()

    assert captured["connect_timeout"] == 3
    assert captured["options"] == "-c default_transaction_read_only=on"
    assert captured["host"] == "db.example.invalid"
    assert captured["port"] == 5432
    assert captured["dbname"] == "evp"
    assert captured["user"] == "evp_reader"


def test_creating_engine_does_not_connect(settings):
    # The host does not exist. If creating the engine tried to connect, this would fail.
    engine = create_db_engine(settings)

    assert isinstance(engine, Engine)
    engine.dispose()


def test_no_engine_is_created_at_import_time():
    assert not any(isinstance(value, Engine) for value in vars(app.db).values())


def test_session_factory_is_bound_to_engine(settings):
    engine = create_db_engine(settings)

    factory = create_session_factory(engine)

    assert factory.kw["bind"] is engine
    engine.dispose()
