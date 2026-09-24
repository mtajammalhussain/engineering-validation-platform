"""The destructive-test safety guard of the integration suite (no database needed)."""

import pytest

from tests.integration.support import (
    REQUIRED_VARS,
    IntegrationConfigError,
    alembic_env,
    load_test_db_config,
)

VALID = {
    "TEST_DB_HOST": "localhost",
    "TEST_DB_PORT": "5432",
    "TEST_DB_NAME": "evp_test",
    "TEST_DB_USER": "evp_writer",
    "TEST_DB_PASSWORD": "test-password-must-not-leak",
    "DB_NAME": "evp",
}


def test_valid_configuration_is_accepted():
    config = load_test_db_config(VALID)

    assert config.name == "evp_test"
    assert config.port == 5432
    assert config.settings().db_name == "evp_test"


def test_db_name_may_be_absent():
    # e.g. in CI, where only TEST_DB_* is set
    environ = {k: v for k, v in VALID.items() if k != "DB_NAME"}

    assert load_test_db_config(environ).name == "evp_test"


@pytest.mark.parametrize("name", REQUIRED_VARS)
@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
def test_missing_or_empty_variable_is_refused(name, value):
    environ = dict(VALID)
    if value is None:
        del environ[name]
    else:
        environ[name] = value

    with pytest.raises(IntegrationConfigError, match=name):
        load_test_db_config(environ)


@pytest.mark.parametrize("name", ["evp", "evp_testing", "test_evp", "evp-test", "evp_TEST"])
def test_database_name_without_test_suffix_is_refused(name):
    with pytest.raises(IntegrationConfigError, match="does not end with '_test'"):
        load_test_db_config({**VALID, "TEST_DB_NAME": name})


def test_database_name_equal_to_db_name_is_refused():
    environ = {**VALID, "TEST_DB_NAME": "evp_test", "DB_NAME": "evp_test"}

    with pytest.raises(IntegrationConfigError, match="same as DB_NAME"):
        load_test_db_config(environ)


def test_non_numeric_port_is_refused():
    with pytest.raises(IntegrationConfigError, match="TEST_DB_PORT"):
        load_test_db_config({**VALID, "TEST_DB_PORT": "abc"})


@pytest.mark.parametrize(
    "overrides", [{"TEST_DB_NAME": "evp"}, {"TEST_DB_HOST": ""}, {"TEST_DB_PORT": "x"}]
)
def test_error_messages_never_contain_the_password(overrides):
    with pytest.raises(IntegrationConfigError) as exc_info:
        load_test_db_config({**VALID, **overrides})

    assert "test-password-must-not-leak" not in str(exc_info.value)


def test_alembic_subprocess_gets_test_database_as_db_vars(monkeypatch):
    monkeypatch.setenv("DB_NAME", "evp")
    monkeypatch.setenv("DB_HOST", "dev-host")
    config = load_test_db_config(VALID)

    env = alembic_env(config)

    assert env["DB_NAME"] == "evp_test"
    assert env["DB_HOST"] == "localhost"
    assert env["DB_PASSWORD"] == "test-password-must-not-leak"
    assert env["PORT"] and env["RESULTS_API_KEY"]


def test_alembic_env_does_not_change_the_developer_environment(monkeypatch):
    import os

    monkeypatch.setenv("DB_NAME", "evp")

    alembic_env(load_test_db_config(VALID))

    assert os.environ["DB_NAME"] == "evp"
