"""FawryPay behind the provider-neutral interface in ``core/provider.py``.

``FawryClient`` keeps the Fawry wire format (field names, signatures, error codes); this
class translates its results and errors into the neutral types the MCP layer uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import httpx2

from masr_pay_mcp.core.provider import (
    ErrorKind,
    Language,
    Payment,
    PaymentProvider,
    PaymentRequest,
    PaymentStatus,
    ProviderError,
    ProviderUnavailable,
    ResponseVerificationError,
)
from masr_pay_mcp.providers.fawry import client as fawry
from masr_pay_mcp.providers.fawry.config import FawryConfig

LANGUAGES: dict[Language, fawry.Language] = {"en": "en-gb", "ar": "ar-eg"}
ERROR_KINDS = {
    9901: ErrorKind.INVALID_CREDENTIALS,
    9946: ErrorKind.INVALID_CREDENTIALS,
    9938: ErrorKind.NOT_FOUND,
    9954: ErrorKind.NOT_PAID,
    9935: ErrorKind.AMOUNT_EXCEEDS_PAID,
}


@contextmanager
def _translated_errors() -> Iterator[None]:
    try:
        yield
    except fawry.FawryError as exc:
        raise ProviderError(
            provider=FawryProvider.name,
            kind=ERROR_KINDS.get(exc.status_code, ErrorKind.OTHER),
            code=exc.status_code,
            meaning=exc.meaning,
            http_status=exc.http_status,
        ) from exc
    except fawry.ResponseSignatureError as exc:
        raise ResponseVerificationError(str(exc)) from exc
    except fawry.FawryUnavailable as exc:
        raise ProviderUnavailable(str(exc)) from exc


def _payment(p: fawry.Payment) -> Payment:
    return Payment(
        provider=FawryProvider.name,
        merchant_ref=p.merchant_ref_number,
        provider_ref=p.reference_number,
        status=PaymentStatus(p.status),
        raw_status=p.raw_status,
        order_amount=p.order_amount,
        payment_amount=p.payment_amount,
        fees=p.fawry_fees,
        paid_at=p.paid_at,
        refunded_amount=p.refunded_amount,
    )


class FawryProvider(PaymentProvider):
    name = "fawry"

    def __init__(self, config: FawryConfig, *, transport: httpx2.AsyncBaseTransport | None = None):
        self._client = fawry.FawryClient(config.base_url, config.credentials, transport=transport)
        self._environment = config.environment

    @property
    def environment(self) -> str:
        return self._environment

    async def create_payment(self, request: PaymentRequest) -> Payment:
        with _translated_errors():
            p = await self._client.create_pay_at_fawry_charge(
                merchant_ref_num=request.merchant_ref,
                amount=request.amount,
                customer_mobile=request.customer_mobile,
                customer_email=request.customer_email,
                description=request.description,
                language=LANGUAGES[request.language],
                customer_name=request.customer_name,
                payment_expiry=request.expires_at,
            )
        return _payment(p)

    async def get_status(self, merchant_ref: str) -> Payment:
        with _translated_errors():
            return _payment(await self._client.get_payment_status(merchant_ref))

    async def cancel(self, payment: Payment, *, language: Language = "en") -> None:
        with _translated_errors():
            await self._client.cancel_unpaid(
                reference_number=payment.provider_ref or "", language=LANGUAGES[language]
            )

    async def refund(self, payment: Payment, *, amount: Decimal, reason: str) -> None:
        with _translated_errors():
            await self._client.refund(
                reference_number=payment.provider_ref or "", amount=amount, reason=reason
            )
