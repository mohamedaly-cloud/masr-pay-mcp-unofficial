"""Money limits that hold no matter what the model asks for (docs/design.md "Security and trust" #3).

Provider-neutral and pure: each check raises ``GuardrailError`` with a message the tool can pass
straight to the model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

DEFAULT_MAX_PAYMENT_EGP = Decimal("50000")
MAX_PAYMENT_ENV = "MASR_PAY_MAX_PAYMENT_EGP"
# Lower than the payment limit: a wrong refund is money gone, a wrong charge is just unpaid.
DEFAULT_MAX_REFUND_EGP = Decimal("2000")
MAX_REFUND_ENV = "MASR_PAY_MAX_REFUND_EGP"
# Off by default: a customer-facing bot must not offer refunds or cancellations at all.
ENABLE_DESTRUCTIVE_ENV = "MASR_PAY_ENABLE_DESTRUCTIVE_TOOLS"


class GuardrailError(ValueError):
    """An amount is outside what this server allows."""


def _limit_from_env(env: Mapping[str, str] | None, name: str, default: Decimal) -> Decimal:
    env = os.environ if env is None else env
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        limit = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{name} must be a number of EGP; got {raw!r}.") from None
    if not limit.is_finite() or limit <= 0:
        raise ValueError(f"{name} must be greater than 0; got {raw!r}.")
    return limit


def max_payment_from_env(env: Mapping[str, str] | None = None) -> Decimal:
    return _limit_from_env(env, MAX_PAYMENT_ENV, DEFAULT_MAX_PAYMENT_EGP)


def max_refund_from_env(env: Mapping[str, str] | None = None) -> Decimal:
    return _limit_from_env(env, MAX_REFUND_ENV, DEFAULT_MAX_REFUND_EGP)


def destructive_tools_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only for an explicit yes; a typo fails at startup instead of silently enabling refunds."""
    env = os.environ if env is None else env
    raw = env.get(ENABLE_DESTRUCTIVE_ENV, "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ValueError(f"{ENABLE_DESTRUCTIVE_ENV} must be true or false; got {raw!r}.")


def check_payment_amount(amount: Decimal, limit: Decimal) -> None:
    if amount > limit:
        raise GuardrailError(
            f"{amount:.2f} EGP is above this server's limit of {limit:.2f} EGP per payment "
            f"({MAX_PAYMENT_ENV}). Nothing was created."
        )


def check_refund_amount(amount: Decimal, order_amount: Decimal) -> None:
    """A refund can never exceed the order amount (Fawry's fees aren't refundable: verify V6)."""
    if amount > order_amount:
        raise GuardrailError(
            f"Can't refund {amount:.2f} EGP: the order amount is {order_amount:.2f} EGP. "
            "Nothing was changed."
        )


def check_refund_limit(amount: Decimal, limit: Decimal) -> None:
    if amount > limit:
        raise GuardrailError(
            f"Can't refund {amount:.2f} EGP: this server's limit is {limit:.2f} EGP per refund "
            f"({MAX_REFUND_ENV}). A larger refund must be done by the merchant directly with "
            "Fawry. Nothing was changed."
        )
