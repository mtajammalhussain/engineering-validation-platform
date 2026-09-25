import pytest
from pydantic import SecretStr

from simulator.config import EXIT_CONFIG_ERROR, Settings, load_settings
from tests.unit.fakes import API_KEY, API_URL, clear_settings_env

VALID_ENV = {"RESULTS_API_URL": API_URL, "RESULTS_API_KEY": API_KEY}


@pytest.fixture
def valid_env(monkeypatch):
    """Set only the two required variables; everything else uses its default."""
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def fails_with(capsys, variable: str) -> str:
    """Assert load_settings() exits with code 2 and names the variable; return the output."""
    with pytest.raises(SystemExit) as exc_info:
        load_settings()
    assert exc_info.value.code == EXIT_CONFIG_ERROR == 2
    output = capsys.readouterr().out
    assert f"  {variable}:" in output
    return output


# --- valid configuration and defaults ---

def test_valid_env_is_loaded_with_exact_defaults(valid_env):
    settings = load_settings()

    assert settings.results_api_url == API_URL
    assert settings.results_api_key.get_secret_value() == API_KEY
    assert settings.app_env == "dev"
    assert settings.log_level == "INFO"
    assert settings.log_format == "text"
    assert settings.sim_mode == "once"
    assert settings.sim_batch_size == 10
    assert settings.sim_interval_s == 60
    assert settings.sim_failure_rate == 0.08
    assert settings.sim_device_count == 20
    assert settings.sim_seed is None


def test_all_values_can_be_set(valid_env):
    for name, value in {
        "APP_ENV": "prod", "LOG_LEVEL": "DEBUG", "LOG_FORMAT": "json", "SIM_MODE": "loop",
        "SIM_BATCH_SIZE": "1000", "SIM_INTERVAL_S": "86400", "SIM_FAILURE_RATE": "1",
        "SIM_DEVICE_COUNT": "999", "SIM_SEED": "0",
    }.items():
        valid_env.setenv(name, value)

    settings = load_settings()

    assert (settings.app_env, settings.log_level, settings.log_format) == ("prod", "DEBUG", "json")
    assert settings.sim_mode == "loop"
    assert settings.sim_batch_size == 1000
    assert settings.sim_interval_s == 86400
    assert settings.sim_failure_rate == 1.0
    assert settings.sim_device_count == 999
    assert settings.sim_seed == 0


def test_unrelated_api_variables_are_ignored(valid_env):
    for name in ("PORT", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
                 "TEST_DB_HOST", "TEST_DB_PASSWORD"):
        valid_env.setenv(name, "anything-even-invalid")

    settings = load_settings()

    assert not {"port", "db_host", "db_password"} & set(Settings.model_fields)
    assert settings.results_api_url == API_URL


# --- required variables ---

@pytest.mark.parametrize("missing", ["RESULTS_API_URL", "RESULTS_API_KEY"])
def test_missing_required_variable_exits_2_and_names_it(valid_env, capsys, missing):
    valid_env.delenv(missing)

    fails_with(capsys, missing)


# --- RESULTS_API_URL ---

@pytest.mark.parametrize("url", ["http://results-api:8001", "http://results-api:8001/"])
def test_base_url_is_stored_without_trailing_slash(valid_env, url):
    valid_env.setenv("RESULTS_API_URL", url)

    assert load_settings().results_api_url == "http://results-api:8001"


def test_https_is_accepted(valid_env):
    valid_env.setenv("RESULTS_API_URL", "https://evp.example.com")

    assert load_settings().results_api_url == "https://evp.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://results-api:8001",           # wrong scheme
        "results-api:8001",                 # no scheme
        "http://",                          # no host
        "http://:8001",                     # no host
        "http://user:pw@results-api:8001",  # user name and password
        "http://user@results-api:8001",     # user name only
        "http://results-api:8001?x=1",      # query
        "http://results-api:8001?",         # empty query
        "http://results-api:8001#top",      # fragment
        "http://results-api:8001/api",      # path
        "http://results-api:8001/api/v1/results",
        "http://results-api:99999",         # port out of range
        "http://results-api:abc",           # port not a number
        "http://results api:8001",          # space
        "http://results-api:8001\n",        # control character
        "",
    ],
)
def test_invalid_url_is_rejected(valid_env, capsys, url):
    valid_env.setenv("RESULTS_API_URL", url)

    fails_with(capsys, "RESULTS_API_URL")


def test_url_with_credentials_is_rejected_without_echoing_it(valid_env, capsys):
    url = "http://user:secret@host:8001"
    valid_env.setenv("RESULTS_API_URL", url)

    output = fails_with(capsys, "RESULTS_API_URL")

    assert "secret" not in output
    assert url not in output
    assert "user:" not in output


# --- RESULTS_API_KEY ---

def test_api_key_is_a_secret_and_hidden_in_repr(valid_env):
    settings = load_settings()

    assert isinstance(settings.results_api_key, SecretStr)
    assert API_KEY not in repr(settings)
    assert API_KEY not in str(settings)


@pytest.mark.parametrize(
    "key",
    ["", "has space-secret", "tab\tsecret", "umlaut-ä-secret", "euro-€-secret", "newline\nsecret"],
)
def test_invalid_api_key_is_rejected(valid_env, capsys, key):
    valid_env.setenv("RESULTS_API_KEY", key)

    fails_with(capsys, "RESULTS_API_KEY")


def test_invalid_api_key_is_not_echoed(valid_env, capsys):
    valid_env.setenv("RESULTS_API_KEY", "my-recognizable-secret-ä")

    output = fails_with(capsys, "RESULTS_API_KEY")

    assert "recognizable" not in output


def test_valid_api_key_is_not_echoed_when_another_variable_fails(valid_env, capsys):
    valid_env.setenv("SIM_MODE", "broken")

    output = fails_with(capsys, "SIM_MODE")

    assert API_KEY not in output


# --- simulation settings ---

@pytest.mark.parametrize(
    "name, value",
    [
        ("SIM_MODE", "broken"),
        ("SIM_MODE", "ONCE"),
        ("SIM_BATCH_SIZE", "0"),
        ("SIM_BATCH_SIZE", "1001"),
        ("SIM_BATCH_SIZE", "ten"),
        ("SIM_INTERVAL_S", "0"),
        ("SIM_INTERVAL_S", "86401"),
        ("SIM_FAILURE_RATE", "-0.01"),
        ("SIM_FAILURE_RATE", "1.01"),
        ("SIM_FAILURE_RATE", "nan"),
        ("SIM_FAILURE_RATE", "inf"),
        ("SIM_FAILURE_RATE", "-inf"),
        ("SIM_DEVICE_COUNT", "0"),
        ("SIM_DEVICE_COUNT", "1000"),
        ("SIM_SEED", "-1"),
        ("SIM_SEED", "abc"),
        ("APP_ENV", "test"),
        ("LOG_LEVEL", "TRACE"),
        ("LOG_FORMAT", "xml"),
    ],
)
def test_invalid_value_exits_2_and_names_it(valid_env, capsys, name, value):
    valid_env.setenv(name, value)

    fails_with(capsys, name)


def test_interval_is_validated_in_once_mode_too(valid_env, capsys):
    valid_env.setenv("SIM_MODE", "once")
    valid_env.setenv("SIM_INTERVAL_S", "0")

    fails_with(capsys, "SIM_INTERVAL_S")


@pytest.mark.parametrize("value", ["0.0", "1.0"])
def test_failure_rate_limits_are_inclusive(valid_env, value):
    valid_env.setenv("SIM_FAILURE_RATE", value)

    assert load_settings().sim_failure_rate == float(value)


def test_seed_unset_is_none(valid_env):
    assert load_settings().sim_seed is None


def test_empty_seed_is_none(valid_env):
    valid_env.setenv("SIM_SEED", "")

    assert load_settings().sim_seed is None


def test_seed_is_parsed_as_integer(valid_env):
    valid_env.setenv("SIM_SEED", "42")

    assert load_settings().sim_seed == 42


@pytest.mark.parametrize("app_env", ["dev", "prod"])
def test_app_env_values(valid_env, app_env):
    valid_env.setenv("APP_ENV", app_env)

    assert load_settings().app_env == app_env


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_log_level_values(valid_env, level):
    valid_env.setenv("LOG_LEVEL", level)

    assert load_settings().log_level == level


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_log_format_values(valid_env, log_format):
    valid_env.setenv("LOG_FORMAT", log_format)

    assert load_settings().log_format == log_format


# --- test isolation itself ---

def test_lower_case_shell_variables_are_removed_too(monkeypatch):
    # pydantic-settings would read "sim_mode" as well as "SIM_MODE".
    monkeypatch.setenv("sim_mode", "broken")

    clear_settings_env(monkeypatch)
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)

    assert load_settings().sim_mode == "once"
