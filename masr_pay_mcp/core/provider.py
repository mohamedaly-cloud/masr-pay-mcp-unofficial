"""The provider-neutral contract every payment provider implements.

The MCP layer (``server.py``) only talks to these types. Adding a provider
(e.g. ``providers/kashier/`` for InstaPay, on hold) means writing one
``PaymentProvider`` subclass that maps its API onto them, with no change to
the tools.

What's neutral here:
  * ``PaymentStatus``: one small status vocabulary. Every ``Payment`` also
    carries the provider's ``raw_status`` unchanged, so nothing is lost in
    translation.
  * ``PaymentRequest`` / ``Payment``: what goes in and what comes out.
  * ``ProviderError`` with an ``ErrorKind``, so callers can react to
    "not found" or "bad credentials" without knowing Fawry's 9938 or 9946.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Literal

Language = Literal["en", "ar"]
"""Customer-facing language. Each provider maps it to its own codes (Fawry: en-gb / ar-eg)."""


class PaymentStatus(StrEnum):
    """Normalized payment status (docs/design.md "Architecture").

    ``UNKNOWN`` isn't in the docs/design.md list. It's what a raw status we don't
    recognise maps to, so a surprise from a provider can never be read as
    ``PAID`` (or as anything else that would trigger an action).
    """

    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELED = "canceled"
    REFUNDED = "refunded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PaymentRequest:
    """Ask a provider to create a payment the customer can complete."""

    merchant_ref: str  # the merchant's own unique order reference
    amount: Decimal  # EGP, at most two decimals
    customer_mobile: str  # Egyptian mobile, 01xxxxxxxxx
    customer_email: str
    description: str
    language: Language = "en"
    customer_name: str | None = None
    expires_at: datetime | None = None  # timezone-aware
    notify_url: str | None = None  # where the provider should report changes to this payment


@dataclass(frozen=True)
class Payment:
    """One payment as reported by a provider, normalized."""

    provider: str  # e.g. "fawry"
    merchant_ref: str
    provider_ref: str | None  # the provider's own reference (Fawry: 9-digit reference number)
    status: PaymentStatus
    raw_status: str  # exactly what the provider said
    order_amount: Decimal  # what the merchant asked for
    payment_amount: Decimal  # what the customer pays (order amount + provider fees)
    fees: Decimal | None
    paid_at: datetime | None  # timezone-aware UTC
    refunded_amount: Decimal | None

    @property
    def is_paid(self) -> bool:
        return self.status is PaymentStatus.PAID

    @property
    def is_awaiting_payment(self) -> bool:
        return self.status is PaymentStatus.AWAITING_PAYMENT


# ----------------------------------------------------------------- errors
class ErrorKind(StrEnum):
    """Why a provider said no, in terms the MCP layer can act on."""

    INVALID_CREDENTIALS = "invalid_credentials"  # merchant code / key / signature rejected
    NOT_FOUND = "not_found"
    NOT_PAID = "not_paid"  # e.g. refund of an unpaid payment
    AMOUNT_EXCEEDS_PAID = "amount_exceeds_paid"
    OTHER = "other"


class ProviderError(Exception):
    """The provider answered, but not with success."""

    def __init__(
        self,
        *,
        provider: str,
        kind: ErrorKind,
        code: int | str | None,
        meaning: str,
        http_status: int | None = None,
    ):
        self.provider = provider
        self.kind = kind
        self.code = code
        self.meaning = meaning
        self.http_status = http_status
        super().__init__(f"{provider} error {code}: {meaning}")


class ProviderUnavailable(Exception):
    """The provider couldn't be reached, timed out, or sent something unusable."""


class ResponseVerificationError(Exception):
    """A response failed its integrity check (e.g. a bad signature). It must not be trusted."""


# ----------------------------------------------------------------- interface
class PaymentProvider(ABC):
    """Contract for one payment provider.

    Implementations are cheap and stateless: the server builds a fresh one for
    every tool call, with credentials loaded for that call only (docs/design.md
    rule 2). They must verify the integrity of every response they turn into
    a ``Payment`` and raise ``ResponseVerificationError`` rather than guess.
    """

    name: ClassVar[str]
    """Short identifier, e.g. "fawry". Becomes the ``provider`` tool argument once there are two."""

    @property
    @abstractmethod
    def environment(self) -> str:
        """Which environment this instance talks to, e.g. "mock", "staging", "production"."""

    @property
    @abstractmethod
    def merchant_id(self) -> str:
        """The merchant these credentials belong to (not secret), e.g. Fawry's merchant code."""

    @abstractmethod
    async def create_payment(self, request: PaymentRequest) -> Payment:
        """Create a payment and return it (normally ``AWAITING_PAYMENT``)."""

    @abstractmethod
    async def get_status(self, merchant_ref: str) -> Payment:
        """Fetch the current state of the payment for ``merchant_ref``."""

    @abstractmethod
    async def cancel(self, payment: Payment, *, language: Language = "en") -> None:
        """Cancel an unpaid payment. ``payment`` should come from a fresh ``get_status``."""

    @abstractmethod
    async def refund(self, payment: Payment, *, amount: Decimal, reason: str) -> None:
        """Refund (part of) a paid payment. ``payment`` should come from a fresh ``get_status``."""
