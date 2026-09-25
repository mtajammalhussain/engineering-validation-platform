"""Service configuration, read from environment variables (docs/APP_SPEC.md §2, §9)."""

import sys
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All settings of the Reporting API.

    Each field is filled from the environment variable with the same name in upper case,
    e.g. ``db_host`` <- ``DB_HOST``. Fields without a default are required.
    There is no API key: the Reporting API only reads (docs/APP_SPEC.md §6, §7).
    """

    # General
    app_env: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    port: int = Field(ge=1, le=65535)

    # Database (intended for a SELECT-only user such as evp_reader)
    db_host: str = Field(min_length=1)
    db_port: int = Field(ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1)


def load_settings() -> Settings:
    """Read and validate the settings, or exit the process with a clear error (fail fast).

    The message names each bad variable and the reason, but never the value,
    so secrets cannot leak into the logs.
    """
    try:
        return Settings()
    except ValidationError as exc:
        lines = ["Invalid configuration, service cannot start:"]
        for error in exc.errors():
            variable = str(error["loc"][0]).upper()
            lines.append(f"  {variable}: {error['msg']}")
        print("\n".join(lines), file=sys.stdout, flush=True)
        sys.exit(1)
