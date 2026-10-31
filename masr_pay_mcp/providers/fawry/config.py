"""Where Fawry lives and which merchant credentials to use.

Stateless by design (docs/design.md rule 2): credentials are read fresh for every
tool call and never cached or stored. In local (stdio) mode they come from
environment variables; on the hosted HTTP server they come from each request's
headers (``X-Fawry-Merchant-Code``, ``X-Fawry-Secure-Key``).
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
MERCHANT_CODE_HEADER = "x-fawry-merchant-code"
SECURE_KEY_HEADER = "x-fawry-secure-key"


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


def load_config(
    env: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None
) -> FawryConfig:
    """Build the configuration from environment variables, plus request headers over HTTP.

    FAWRY_MERCHANT_CODE, FAWRY_SECURE_KEY  required (stdio)
    FAWRY_ENV        mock (default) | staging | production
    FAWRY_BASE_URL   overrides the URL picked by FAWRY_ENV (e.g. a mock on another port)

    When ``headers`` is given (the HTTP server), the credentials come only from the
    X-Fawry-Merchant-Code and X-Fawry-Secure-Key headers, never from the environment, so one
    merchant can't fall back to credentials meant for another. Where Fawry is (FAWRY_ENV,
    FAWRY_BASE_URL) is always the operator's choice: a header can't point the server at
    another URL.
    """
    env = os.environ if env is None else env
    environment = env.get("FAWRY_ENV", DEFAULT_ENVIRONMENT).strip().lower()
    if environment not in BASE_URLS:
        raise ConfigError(f"FAWRY_ENV must be one of {', '.join(BASE_URLS)}; got {environment!r}.")
    base_url = (env.get("FAWRY_BASE_URL") or BASE_URLS[environment]).rstrip("/")

    if headers is None:
        source, code_name, key_name = env, "FAWRY_MERCHANT_CODE", "FAWRY_SECURE_KEY"
        where = "the MCP server's environment"
    else:
        source = {k.lower(): v for k, v in headers.items()}
        code_name, key_name = MERCHANT_CODE_HEADER, SECURE_KEY_HEADER
        where = "your MCP client's request headers"
    merchant_code = source.get(code_name, "").strip()
    secure_key = source.get(key_name, "").strip()
    missing = [
        name for name, value in ((code_name, merchant_code), (key_name, secure_key)) if not value
    ]
    if missing:
        raise ConfigError(f"Missing {' and '.join(missing)}. Set them in {where} (see README).")
    return FawryConfig(
        base_url=base_url,
        environment=environment,
        credentials=Credentials(merchant_code=merchant_code, secure_key=secure_key),
    )
