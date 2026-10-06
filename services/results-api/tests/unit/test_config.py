from typing import get_args

import pytest
from pydantic import SecretStr

from app.config import MigrationSettings, Settings, load_migration_settings, load_settings

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


# --- Migration settings vs runtime settings ---------------------------------------------

HEADER = "Invalid configuration, service cannot start:"
MIGRATION_VARS = (
    "APP_ENV", "LOG_LEVEL", "LOG_FORMAT", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
)
RUNTIME_ONLY_VARS = ("PORT", "RESULTS_API_KEY")
MIGRATION_ENV = {name: value for name, value in VALID_ENV.items() if name not in RUNTIME_ONLY_VARS}
RUNTIME_ENV = dict(VALID_ENV)


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every configuration variable, then set exactly the given ones."""

    def apply(values: dict[str, str]) -> None:
        for name in MIGRATION_VARS + RUNTIME_ONLY_VARS:
            monkeypatch.delenv(name, raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return apply


def run_failing(loader, capsys) -> list[str]:
    """Call a loader that must fail fast; check exit code and header; return the error lines."""
    with pytest.raises(SystemExit) as exc_info:
        loader()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "super-secret" not in out
    lines = out.splitlines()
    assert lines[0] == HEADER
    return lines[1:]


def test_runtime_settings_still_require_port(clean_env, capsys):
    clean_env({k: v for k, v in RUNTIME_ENV.items() if k != "PORT"})

    assert run_failing(load_settings, capsys) == ["  PORT: Field required"]


def test_runtime_settings_still_require_results_api_key(clean_env, capsys):
    clean_env({k: v for k, v in RUNTIME_ENV.items() if k != "RESULTS_API_KEY"})

    assert run_failing(load_settings, capsys) == ["  RESULTS_API_KEY: Field required"]


def test_runtime_settings_reject_empty_results_api_key(clean_env, capsys):
    clean_env({**RUNTIME_ENV, "RESULTS_API_KEY": ""})

    lines = run_failing(load_settings, capsys)

    assert len(lines) == 1
    assert lines[0].startswith("  RESULTS_API_KEY: ")


def test_migration_settings_load_without_api_only_variables(clean_env):
    clean_env(MIGRATION_ENV)

    settings = load_migration_settings()

    assert type(settings) is MigrationSettings
    assert settings.db_host == "localhost"
    assert settings.db_port == 5432
    assert settings.db_name == "evp"
    assert settings.db_user == "evp_writer"
    assert settings.db_password.get_secret_value() == "super-secret-password"
    assert (settings.app_env, settings.log_level, settings.log_format) == ("dev", "INFO", "text")


def test_runtime_settings_fail_in_the_same_environment(clean_env, capsys):
    clean_env(MIGRATION_ENV)

    assert run_failing(load_settings, capsys) == [
        "  PORT: Field required",
        "  RESULTS_API_KEY: Field required",
    ]


def test_migration_settings_ignore_api_only_variables_in_the_environment(clean_env):
    clean_env(RUNTIME_ENV)

    settings = load_migration_settings()

    assert not hasattr(settings, "port")
    assert not hasattr(settings, "results_api_key")


def test_migration_settings_hide_the_db_password_in_repr(clean_env):
    clean_env(MIGRATION_ENV)

    assert "super-secret" not in repr(load_migration_settings())


def constraints(field) -> dict:
    """The validation constraints of a field, e.g. {"ge": 1, "le": 65535}."""
    found = {}
    for item in field.metadata:
        for attr in ("min_length", "ge", "le"):
            if getattr(item, attr, None) is not None:
                found[attr] = getattr(item, attr)
    return found


def test_settings_are_migration_settings_plus_only_the_api_fields():
    migration_fields = {name.lower() for name in MIGRATION_VARS}

    assert issubclass(Settings, MigrationSettings)
    assert set(MigrationSettings.model_fields) == migration_fields
    assert set(Settings.model_fields) - migration_fields == {"port", "results_api_key"}


@pytest.mark.parametrize("name", [name.lower() for name in MIGRATION_VARS])
def test_shared_fields_have_identical_rules_in_both_classes(name):
    runtime = Settings.model_fields[name]
    migration = MigrationSettings.model_fields[name]

    assert runtime.annotation == migration.annotation
    assert runtime.metadata == migration.metadata
    assert runtime.default == migration.default
    assert runtime.is_required() == migration.is_required()


def test_migration_field_rules_are_unchanged():
    fields = MigrationSettings.model_fields

    assert get_args(fields["app_env"].annotation) == ("dev", "prod")
    assert get_args(fields["log_level"].annotation) == (
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
    )
    assert get_args(fields["log_format"].annotation) == ("text", "json")
    assert (fields["app_env"].default, fields["log_level"].default) == ("dev", "INFO")
    assert fields["log_format"].default == "text"
    for name in ("db_host", "db_name", "db_user"):
        assert fields[name].annotation is str
        assert fields[name].is_required()
        assert constraints(fields[name]) == {"min_length": 1}
    assert fields["db_port"].annotation is int
    assert fields["db_port"].is_required()
    assert constraints(fields["db_port"]) == {"ge": 1, "le": 65535}
    assert fields["db_password"].annotation is SecretStr
    assert fields["db_password"].is_required()
    assert constraints(fields["db_password"]) == {"min_length": 1}


def test_api_only_field_rules_are_unchanged():
    port = Settings.model_fields["port"]
    api_key = Settings.model_fields["results_api_key"]

    assert port.is_required()
    assert port.annotation is int
    assert constraints(port) == {"ge": 1, "le": 65535}
    assert api_key.is_required()
    assert api_key.annotation is SecretStr
    assert constraints(api_key) == {"min_length": 1}


SHARED_INVALID = [
    ("DB_PORT", "not-a-number"),
    ("DB_PORT", "0"),
    ("DB_PORT", "70000"),
    ("DB_HOST", ""),
    ("DB_NAME", ""),
    ("DB_USER", ""),
    ("DB_PASSWORD", ""),
    ("LOG_LEVEL", "VERBOSE"),
    ("LOG_FORMAT", "xml"),
    ("APP_ENV", "test"),
]
RUNTIME_ONLY_INVALID = [("PORT", "0"), ("PORT", "70000"), ("PORT", "abc"), ("RESULTS_API_KEY", "")]
PARITY_CASES = (
    [("migration", name, value) for name, value in SHARED_INVALID]
    + [("runtime", name, value) for name, value in SHARED_INVALID + RUNTIME_ONLY_INVALID]
)


@pytest.mark.parametrize("loader_name, name, value", PARITY_CASES)
def test_exactly_one_invalid_variable_is_reported_by_either_loader(
    clean_env, capsys, loader_name, name, value
):
    loader, base_env = {
        "migration": (load_migration_settings, MIGRATION_ENV),
        "runtime": (load_settings, RUNTIME_ENV),
    }[loader_name]
    clean_env({**base_env, name: value})

    lines = run_failing(loader, capsys)

    assert len(lines) == 1
    assert lines[0].startswith(f"  {name}: ")
