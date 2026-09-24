from sqlalchemy import Engine, make_url

import app.db
from app.config import Settings
from app.db import build_database_url, create_db_engine, create_session_factory

SPECIAL_PASSWORD = "p@ss:w/rd"


def make_settings(**overrides) -> Settings:
    """Build Settings directly. All fields are passed, so shell variables cannot interfere."""
    values = {
        "app_env": "dev",
        "log_level": "INFO",
        "log_format": "text",
        "port": 8001,
        "db_host": "db.example.invalid",
        "db_port": 5432,
        "db_name": "evp",
        "db_user": "evp_writer",
        "db_password": SPECIAL_PASSWORD,
        "results_api_key": "secret",
    }
    values.update(overrides)
    return Settings(**values)


def test_url_contains_all_parts_from_settings():
    url = build_database_url(make_settings())

    assert url.drivername == "postgresql+psycopg2"
    assert url.username == "evp_writer"
    assert url.host == "db.example.invalid"
    assert url.port == 5432
    assert url.database == "evp"


def test_special_characters_in_password_are_preserved():
    url = build_database_url(make_settings())

    assert url.password == SPECIAL_PASSWORD
    # Written out as a full string, the special characters are percent-encoded ...
    full = url.render_as_string(hide_password=False)
    assert "p%40ss%3Aw%2Frd" in full
    # ... and parsing that string back gives the original password.
    assert make_url(full).password == SPECIAL_PASSWORD


def test_normal_string_representation_masks_password():
    url = build_database_url(make_settings())

    for text in (str(url), repr(url)):
        assert SPECIAL_PASSWORD not in text
        assert "p%40ss%3Aw%2Frd" not in text
        assert "***" in text


def test_engine_uses_pool_pre_ping_and_connect_timeout(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"], captured["kwargs"] = url, kwargs
        return "fake-engine"

    monkeypatch.setattr(app.db, "create_engine", fake_create_engine)

    engine = create_db_engine(make_settings())

    assert engine == "fake-engine"
    assert captured["kwargs"]["pool_pre_ping"] is True
    assert captured["kwargs"]["connect_args"] == {"connect_timeout": app.db.DB_CONNECT_TIMEOUT_S}
    assert 0 < app.db.DB_CONNECT_TIMEOUT_S <= 5
    assert captured["url"].password == SPECIAL_PASSWORD


def test_creating_engine_does_not_connect():
    # The host does not exist. If creating the engine tried to connect, this would fail.
    engine = create_db_engine(make_settings())

    assert isinstance(engine, Engine)
    engine.dispose()


def test_no_engine_is_created_at_import_time():
    assert not any(isinstance(value, Engine) for value in vars(app.db).values())


def test_session_factory_is_bound_to_engine():
    engine = create_db_engine(make_settings())

    factory = create_session_factory(engine)

    assert factory.kw["bind"] is engine
    engine.dispose()
