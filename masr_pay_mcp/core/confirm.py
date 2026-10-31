"""Stateless two-step confirmation for destructive tools (docs/design.md "Security and trust" #1).

First call: the tool previews the action and returns a token. Second call: the same arguments
plus that token. The token is ``<expiry>.<hmac>``, where the HMAC covers the tool name, a hash
of the exact arguments and the expiry. The server keeps nothing: it recomputes the HMAC from the
second call's arguments, so a token for "refund 10 EGP" can't authorise "refund 150 EGP", and
only a holder of the server secret can mint one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta

TTL = timedelta(minutes=5)
SECRET_ENV = "MASR_PAY_CONFIRM_SECRET"  # noqa: S105 - the variable's name, not a secret
MIN_SECRET_LENGTH = 32


class ConfirmationError(Exception):
    """The token is missing, forged, expired, or for a different action."""


def load_secret(env: Mapping[str, str] | None = None) -> bytes:
    """The HMAC key: ``MASR_PAY_CONFIRM_SECRET`` if set, else random for this process."""
    env = os.environ if env is None else env
    value = env.get(SECRET_ENV, "").strip()
    if not value:
        # A per-process key is fine for stdio (one process). The HTTP server (web.py) never gets
        # here: with many instances, tokens would fail (closed) across them, so it refuses to start.
        return secrets.token_bytes(32)
    if len(value) < MIN_SECRET_LENGTH:
        raise ValueError(f"{SECRET_ENV} must be at least {MIN_SECRET_LENGTH} characters.")
    return value.encode("utf-8")


def _mac(secret: bytes, tool: str, args: Mapping[str, object], expires: int) -> str:
    canonical = json.dumps(
        {"tool": tool, "args": args, "exp": expires},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,  # Decimal("150.00") -> "150.00", stable
    )
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def issue(
    secret: bytes, tool: str, args: Mapping[str, object], now: datetime
) -> tuple[str, datetime]:
    """Mint a token for exactly this tool + arguments, valid for ``TTL``."""
    expires = int((now + TTL).timestamp())
    return f"{expires}.{_mac(secret, tool, args, expires)}", datetime.fromtimestamp(
        expires, tz=now.tzinfo
    )


def verify(secret: bytes, token: str, tool: str, args: Mapping[str, object], now: datetime) -> None:
    """Raise ``ConfirmationError`` unless ``token`` was issued for this tool + arguments and is live."""
    expires_text, _, mac = token.strip().partition(".")
    if not expires_text.isdigit() or not mac:
        raise ConfirmationError("The confirmation token is malformed.")
    expires = int(expires_text)
    # Check the MAC first: an attacker-edited expiry fails here, not as "expired".
    if not hmac.compare_digest(mac, _mac(secret, tool, args, expires)):
        raise ConfirmationError(
            "The confirmation token doesn't match this exact action (same order, amount and "
            "other arguments as the preview), or wasn't issued by this server."
        )
    if now.timestamp() > expires:
        raise ConfirmationError("The confirmation token has expired (they last 5 minutes).")
