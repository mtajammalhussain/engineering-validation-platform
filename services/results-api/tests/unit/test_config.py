import pytest

from app.config import Settings, load_settings

VALID_ENV = {
    "PORT": "8001",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "evp",
    "DB_USER": "evp_writer",
    "DB_PASSWORD": "super-secret-password",
    "RESULTS_API_KEY": "super-secret-key",
}
OPTIONAL_VARS = ("APP_ENV", "LOG_LEVEL", "LOG_FORMAT")


@pytest.fixture
def valid_env(monkeypatch):
    """Set a complete, valid environment and remove anything else from the developer's shell."""
    for name in OPTIONAL_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_valid_env_is_loaded_with_defaults(valid_env):
    settings = load_settings()

    assert settings.db_host == "localhost"
    assert settings.db_port == 5432
    assert settings.port == 8001
    assert settings.app_env == "dev"
    assert settings.log_level == "INFO"
    assert settings.log_format == "text"


def test_secrets_are_hidden_in_repr(valid_env):
    settings = Settings()

    assert "super-secret" not in repr(settings)
    assert settings.db_password.get_secret_value() == "super-secret-password"


@pytest.mark.parametrize("missing", ["DB_PASSWORD", "RESULTS_API_KEY", "DB_HOST", "PORT"])
def test_missing_required_variable_exits_and_names_it(valid_env, capsys, missing):
    valid_env.delenv(missing)

    with pytest.raises(SystemExit) as exc_info:
        load_settings()

    assert exc_info.value.code == 1
    assert missing in capsys.readouterr().out


@pytest.mark.parametrize(
    "name, value",
    [("DB_PORT", "not-a-number"), ("PORT", "70000"), ("LOG_FORMAT", "xml"), ("APP_ENV", "test")],
)
def test_invalid_value_exits_and_names_it(valid_env, capsys, name, value):
    valid_env.setenv(name, value)

    with pytest.raises(SystemExit):
        load_settings()

    assert name in capsys.readouterr().out


def test_error_message_never_contains_secret_values(valid_env, capsys):
    valid_env.delenv("DB_HOST")

    with pytest.raises(SystemExit):
        load_settings()

    assert "super-secret" not in capsys.readouterr().out
