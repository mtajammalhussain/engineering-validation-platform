"""Service configuration, read from environment variables (docs/APP_SPEC.md §2, §9)."""

import sys
from typing import Literal, TypeVar

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings


class MigrationSettings(BaseSettings):
    """The settings the migration command reads: logging and database (spec §2 rule 7, §9).

    Each field is filled from the environment variable with the same name in upper case,
    e.g. ``db_host`` <- ``DB_HOST``. Fields without a default are required.
    """

    # General
    app_env: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"

    # Database
    db_host: str = Field(min_length=1)
    db_port: int = Field(ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1)


class Settings(MigrationSettings):
    """All settings of the running Results API: the migration settings plus HTTP-only ones."""

    port: int = Field(ge=1, le=65535)

    # Authentication for POST /api/v1/results
    results_api_key: SecretStr = Field(min_length=1)


SettingsT = TypeVar("SettingsT", bound=MigrationSettings)


def _load(settings_class: type[SettingsT]) -> SettingsT:
    """Read and validate the settings, or exit the process with a clear error (fail fast).

    The message names each bad variable and the reason, but never the value,
    so secrets cannot leak into the logs.
    """
    try:
        return settings_class()
    except ValidationError as exc:
        lines = ["Invalid configuration, service cannot start:"]
        for error in exc.errors():
            variable = str(error["loc"][0]).upper()
            lines.append(f"  {variable}: {error['msg']}")
        print("\n".join(lines), file=sys.stdout, flush=True)
        sys.exit(1)


def load_settings() -> Settings:
    """Settings for the running API (all fields, including PORT and RESULTS_API_KEY)."""
    return _load(Settings)


def load_migration_settings() -> MigrationSettings:
    """Settings for the ``alembic`` command: database and logging only."""
    return _load(MigrationSettings)
