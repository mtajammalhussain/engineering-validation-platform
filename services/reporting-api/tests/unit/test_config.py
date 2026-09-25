import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings

REQUIRED_VARS = ("PORT", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


def test_valid_env_is_loaded(valid_env):
    settings = load_settings()

    assert settings.port == 8002
    assert settings.db_host == "db.example.invalid"
    assert settings.db_port == 5432
    assert settings.db_name == "evp"
    assert settings.db_user == "evp_reader"
    assert settings.db_password.get_secret_value() == "super-secret-password"


def test_optional_variables_have_the_results_api_defaults(valid_env):
    settings = load_settings()

    assert settings.app_env == "dev"
    assert settings.log_level == "INFO"
    assert settings.log_format == "text"


@pytest.mark.parametrize("missing", REQUIRED_VARS)
def test_missing_required_variable_exits_and_names_it(valid_env, capsys, missing):
    valid_env.delenv(missing)

    with pytest.raises(SystemExit) as exc_info:
        load_settings()

    assert exc_info.value.code == 1
    assert missing in capsys.readouterr().out


@pytest.mark.parametrize("name", ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"])
def test_empty_required_text_variable_exits(valid_env, capsys, name):
    valid_env.setenv(name, "")

    with pytest.raises(SystemExit) as exc_info:
        load_settings()

    assert exc_info.value.code == 1
    assert name in capsys.readouterr().out


@pytest.mark.parametrize(
    "name, value",
    [
        ("APP_ENV", "dev"),
        ("APP_ENV", "prod"),
        ("LOG_LEVEL", "DEBUG"),
        ("LOG_LEVEL", "INFO"),
        ("LOG_LEVEL", "WARNING"),
        ("LOG_LEVEL", "ERROR"),
        ("LOG_LEVEL", "CRITICAL"),
        ("LOG_FORMAT", "text"),
        ("LOG_FORMAT", "json"),
    ],
)
def test_allowed_optional_values_are_accepted(valid_env, name, value):
    valid_env.setenv(name, value)

    settings = load_settings()

    assert getattr(settings, name.lower()) == value


@pytest.mark.parametrize(
    "name, value",
    [
        ("APP_ENV", "test"),
        ("LOG_LEVEL", "verbose"),
        ("LOG_FORMAT", "xml"),
        ("PORT", "70000"),
        ("PORT", "0"),
        ("DB_PORT", "not-a-number"),
    ],
)
def test_invalid_value_exits_and_names_it(valid_env, capsys, name, value):
    valid_env.setenv(name, value)

    with pytest.raises(SystemExit) as exc_info:
        load_settings()

    assert exc_info.value.code == 1
    assert name in capsys.readouterr().out


def test_results_api_key_is_not_a_setting():
    assert "results_api_key" not in Settings.model_fields


def test_results_api_key_in_environment_is_ignored(valid_env):
    valid_env.setenv("RESULTS_API_KEY", "leftover-from-results-api")

    settings = load_settings()

    assert not hasattr(settings, "results_api_key")


def test_results_api_key_cannot_be_passed_directly(make_settings):
    with pytest.raises(ValidationError):
        make_settings(results_api_key="not-allowed")


def test_secrets_are_hidden_in_repr(valid_env):
    settings = load_settings()

    assert "super-secret" not in repr(settings)
    assert "super-secret" not in str(settings)


def test_error_message_never_contains_secret_values(valid_env, capsys):
    valid_env.delenv("DB_HOST")

    with pytest.raises(SystemExit):
        load_settings()

    assert "super-secret" not in capsys.readouterr().out
