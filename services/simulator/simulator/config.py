"""Simulator configuration, read from environment variables (docs/APP_SPEC.md §2, §9)."""

import sys
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings

# Exit code for invalid configuration (spec §8.3.3). The APIs use 1; the simulator uses 2 so a
# CronJob can tell "bad configuration" apart from "no result accepted" (exit code 1).
EXIT_CONFIG_ERROR = 2


class Settings(BaseSettings):
    """All settings of the simulator.

    Each field is filled from the environment variable with the same name in upper case,
    e.g. ``sim_mode`` <- ``SIM_MODE``. Fields without a default are required.
    The simulator has no database and no HTTP server, so there is no ``PORT`` or ``DB_*``.
    """

    # General
    app_env: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"

    # Results API (the only service the simulator talks to)
    results_api_url: str
    results_api_key: SecretStr = Field(min_length=1)

    # Simulation
    sim_mode: Literal["once", "loop"] = "once"
    sim_batch_size: int = Field(default=10, ge=1, le=1000)
    sim_interval_s: int = Field(default=60, ge=1, le=86400)
    # allow_inf_nan=False rejects NaN and ±Infinity with a clear message.
    sim_failure_rate: float = Field(default=0.08, ge=0.0, le=1.0, allow_inf_nan=False)
    sim_device_count: int = Field(default=20, ge=1, le=999)  # ECU-### has three digits
    sim_seed: int | None = Field(default=None, ge=0)

    @field_validator("results_api_url")
    @classmethod
    def base_url_only(cls, value: str) -> str:
        """Accept only a base URL such as ``http://results-api:8001`` (spec §9).

        Returns it without a trailing slash, so the client can append ``/api/v1/results``
        without producing ``//``. The error messages never contain the value, because a
        rejected URL may contain a password.
        """
        # urlsplit silently drops tabs/newlines and trims spaces, so check this first.
        if any(char.isspace() or not char.isprintable() for char in value):
            raise ValueError("must not contain spaces or control characters")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            raise ValueError("scheme must be http or https")
        if "@" in parts.netloc:
            raise ValueError("must not contain a user name or password")
        if not parts.hostname:
            raise ValueError("host is required")
        try:
            parts.port  # reading it validates the port (number, 0-65535)
        except ValueError:
            raise ValueError("port is invalid") from None
        # "?" / "#" are checked directly: urlsplit reports "http://host?" as an empty query.
        if "?" in value:
            raise ValueError("must not contain a query")
        if "#" in value:
            raise ValueError("must not contain a fragment")
        if parts.path not in ("", "/"):
            raise ValueError("must not contain a path (the client adds /api/v1/results)")
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("results_api_key")
    @classmethod
    def visible_ascii_only(cls, value: SecretStr) -> SecretStr:
        # An HTTP header can only carry these characters; others would crash the client later.
        if not all("!" <= char <= "~" for char in value.get_secret_value()):
            raise ValueError("must contain only visible ASCII characters (! to ~)")
        return value

    @field_validator("sim_seed", mode="before")
    @classmethod
    def empty_seed_means_unset(cls, value: object) -> object:
        # .env.example contains "SIM_SEED=" (empty): treat it like an unset variable.
        return None if value == "" else value


def load_settings() -> Settings:
    """Read and validate the settings, or exit with code 2 and a clear error (fail fast).

    The message names each bad variable and the reason, but never the value,
    so secrets cannot leak into the logs.
    """
    try:
        return Settings()
    except ValidationError as exc:
        lines = ["Invalid configuration, simulator cannot start:"]
        for error in exc.errors():
            variable = str(error["loc"][0]).upper()
            lines.append(f"  {variable}: {error['msg']}")
        print("\n".join(lines), file=sys.stdout, flush=True)
        sys.exit(EXIT_CONFIG_ERROR)
