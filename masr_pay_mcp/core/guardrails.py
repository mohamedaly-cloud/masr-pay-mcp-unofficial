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


class GuardrailError(ValueError):
    """An amount is outside what this server allows."""


def max_payment_from_env(env: Mapping[str, str] | None = None) -> Decimal:
    env = os.environ if env is None else env
    raw = env.get(MAX_PAYMENT_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_PAYMENT_EGP
    try:
        limit = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{MAX_PAYMENT_ENV} must be a number of EGP; got {raw!r}.") from None
    if not limit.is_finite() or limit <= 0:
        raise ValueError(f"{MAX_PAYMENT_ENV} must be greater than 0; got {raw!r}.")
    return limit


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
