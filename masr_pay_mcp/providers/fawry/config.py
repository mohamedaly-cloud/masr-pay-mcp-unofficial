"""Where Fawry lives and which merchant credentials to use.

Stateless by design (docs/design.md rule 2): credentials are read fresh for every
tool call and never cached or stored. In local (stdio) mode they come from
environment variables. Milestone 3 adds per-request HTTP headers for the
hosted server.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

BASE_URLS = {
    "mock": "http://127.0.0.1:8080",
    "staging": "https://atfawry.fawrystaging.com",
    "production": "https://www.atfawry.com",
}
# The mock is the default so a misconfigured setup can never touch real money.
DEFAULT_ENVIRONMENT = "mock"


class ConfigError(RuntimeError):
    """The server isn't configured well enough to call Fawry."""


@dataclass(frozen=True)
class Credentials:
    merchant_code: str
    # repr=False keeps the key out of tracebacks, debug output and logs.
    secure_key: str = field(repr=False)


@dataclass(frozen=True)
class FawryConfig:
    base_url: str
    environment: str
    credentials: Credentials


def load_config(env: Mapping[str, str] | None = None) -> FawryConfig:
    """Build the configuration from environment variables.

    FAWRY_MERCHANT_CODE, FAWRY_SECURE_KEY  required
    FAWRY_ENV        mock (default) | staging | production
    FAWRY_BASE_URL   overrides the URL picked by FAWRY_ENV (e.g. a mock on another port)
    """
    env = os.environ if env is None else env
    environment = env.get("FAWRY_ENV", DEFAULT_ENVIRONMENT).strip().lower()
    if environment not in BASE_URLS:
        raise ConfigError(f"FAWRY_ENV must be one of {', '.join(BASE_URLS)}; got {environment!r}.")
    base_url = (env.get("FAWRY_BASE_URL") or BASE_URLS[environment]).rstrip("/")

    merchant_code = env.get("FAWRY_MERCHANT_CODE", "").strip()
    secure_key = env.get("FAWRY_SECURE_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("FAWRY_MERCHANT_CODE", merchant_code),
            ("FAWRY_SECURE_KEY", secure_key),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"Missing {' and '.join(missing)}. Set them in the MCP server's environment "
            "(see README)."
        )
    return FawryConfig(
        base_url=base_url,
        environment=environment,
        credentials=Credentials(merchant_code=merchant_code, secure_key=secure_key),
    )
