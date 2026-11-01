"""Thin async HTTP client for the FawryPay endpoints we use.

Responsibilities:
  * sign every request (``signatures.py``);
  * verify the signature on every response that carries order data, and
    refuse to trust one that doesn't verify ("fail closed");
  * turn Fawry's inconsistent field names into one ``Payment`` shape
    (``merchantRefNum``/``merchantRefNumber``, ``referenceNumber``/``fawryRefNumber``);
  * turn Fawry error codes into ``FawryError`` with a plain-English meaning.

It never logs request or response bodies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx2

from masr_pay_mcp.providers.fawry import signatures as sig
from masr_pay_mcp.providers.fawry.config import Credentials

log = logging.getLogger(__name__)

CHARGE_PATH = "/ECommerceWeb/Fawry/payments/charge"
STATUS_V2_PATH = "/ECommerceWeb/Fawry/payments/status/v2"
REFUND_PATH = "/ECommerceWeb/Fawry/payments/refund"
CANCEL_UNPAID_PATH = "/ECommerceWeb/api/orders/cancel-unpaid-order"

PAY_AT_FAWRY = "PayAtFawry"
Language = Literal["ar-eg", "en-gb"]

# Sources disagree on which status Fawry uses for a fresh order (docs/design.md),
# so NEW, UNPAID and PENDING all mean "awaiting payment".
NORMALIZED_STATUS = {
    "NEW": "awaiting_payment",
    "UNPAID": "awaiting_payment",
    "PENDING": "awaiting_payment",
    "PAID": "paid",
    "EXPIRED": "expired",
    "CANCELED": "canceled",
    "CANCELLED": "canceled",
    "REFUNDED": "refunded",
    "PARTIALLY_REFUNDED": "refunded",
    "FAILED": "failed",
}
NormalizedStatus = Literal[
    "awaiting_payment", "paid", "expired", "canceled", "refunded", "failed", "unknown"
]

# Official error codes and what they mean for the caller.
ERROR_MEANINGS = {
    9901: "Fawry rejected the merchant code. The server's FAWRY_MERCHANT_CODE is probably wrong.",
    9938: "Fawry has no order with that reference.",
    9946: (
        "Fawry rejected the request signature. The server's FAWRY_SECURE_KEY or "
        "FAWRY_MERCHANT_CODE is probably wrong."
    ),
    9935: "The refund amount is greater than the amount that was paid.",
    9954: "The order hasn't been paid, so it can't be refunded.",
}


# ----------------------------------------------------------------- errors
class FawryError(Exception):
    """Fawry answered, but not with success."""

    def __init__(self, status_code: int | None, description: str, http_status: int | None):
        self.status_code = status_code
        self.description = description
        self.http_status = http_status
        meaning = ERROR_MEANINGS.get(status_code) if status_code is not None else None
        self.meaning = meaning or description or "Fawry returned an error."
        super().__init__(f"Fawry error {status_code}: {self.meaning}")


class FawryUnavailable(Exception):
    """Fawry couldn't be reached, timed out, or sent something that isn't JSON."""


class ResponseSignatureError(Exception):
    """A response carried order data but its signature didn't verify. Don't trust it."""


# ----------------------------------------------------------------- result model
@dataclass(frozen=True)
class Payment:
    """One Pay-at-Fawry order, normalized across the charge and status responses."""

    reference_number: str | None
    merchant_ref_number: str
    raw_status: str
    status: NormalizedStatus
    order_amount: Decimal
    payment_amount: Decimal
    fawry_fees: Decimal | None
    payment_method: str
    paid_at: datetime | None
    refunded_amount: Decimal | None

    @property
    def is_paid(self) -> bool:
        return self.status == "paid"

    @property
    def is_awaiting_payment(self) -> bool:
        return self.status == "awaiting_payment"


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _epoch_ms_to_utc(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


# ----------------------------------------------------------------- client
class FawryClient:
    """One instance per tool call: holds no state beyond its config."""

    def __init__(
        self,
        base_url: str,
        credentials: Credentials,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._creds = credentials
        self._transport = transport  # tests inject an ASGI transport pointing at the mock
        self._timeout = timeout

    # ------------------------------------------------------------ public API
    async def create_pay_at_fawry_charge(
        self,
        *,
        merchant_ref_num: str,
        amount: Decimal,
        customer_mobile: str,
        customer_email: str,
        description: str,
        language: Language = "en-gb",
        customer_name: str | None = None,
        payment_expiry: datetime | None = None,
    ) -> Payment:
        amount_2dp = sig.format_amount(amount)
        body: dict[str, Any] = {
            "merchantCode": self._creds.merchant_code,
            "merchantRefNum": merchant_ref_num,
            "paymentMethod": PAY_AT_FAWRY,
            "customerMobile": customer_mobile,
            "customerEmail": customer_email,
            # Sent as a JSON number, like Fawry's samples. The value has at most two
            # decimals, so float(str) is exact enough to round-trip to the signed text.
            "amount": float(amount_2dp),
            "currencyCode": "EGP",
            "description": description,
            "language": language,
            "chargeItems": [
                {
                    "itemId": merchant_ref_num,
                    "description": description,
                    "price": float(amount_2dp),
                    "quantity": 1,
                }
            ],
            "signature": sig.charge_request_signature(
                merchant_code=self._creds.merchant_code,
                merchant_ref_num=merchant_ref_num,
                customer_profile_id=None,
                payment_method=PAY_AT_FAWRY,
                amount=amount_2dp,
                secure_key=self._creds.secure_key,
            ),
        }
        if customer_name:
            body["customerName"] = customer_name
        if payment_expiry is not None:
            body["paymentExpiry"] = int(payment_expiry.timestamp() * 1000)

        data = await self._request("POST", CHARGE_PATH, json=body)
        return self._payment_from(data, ref_field="referenceNumber", merchant_ref=merchant_ref_num)

    async def get_payment_status(self, merchant_ref_number: str) -> Payment:
        params = {
            "merchantCode": self._creds.merchant_code,
            "merchantRefNumber": merchant_ref_number,
            "signature": sig.status_v2_signature(
                merchant_code=self._creds.merchant_code,
                merchant_ref_number=merchant_ref_number,
                secure_key=self._creds.secure_key,
            ),
        }
        data = await self._request("GET", STATUS_V2_PATH, params=params)
        return self._payment_from(
            data, ref_field="fawryRefNumber", merchant_ref=merchant_ref_number
        )

    async def refund(self, *, reference_number: str, amount: Decimal, reason: str) -> None:
        amount_2dp = sig.format_amount(amount)
        body = {
            "merchantCode": self._creds.merchant_code,
            "referenceNumber": reference_number,
            "refundAmount": float(amount_2dp),
            "reason": reason,
            "signature": sig.refund_signature(
                merchant_code=self._creds.merchant_code,
                reference_number=reference_number,
                refund_amount=amount_2dp,
                reason=reason,
                secure_key=self._creds.secure_key,
            ),
        }
        await self._request("POST", REFUND_PATH, json=body)

    async def cancel_unpaid(self, *, reference_number: str, language: Language = "en-gb") -> None:
        # Third-party recipe; orderRefNo assumed to be Fawry's reference (verify list V4).
        body = {
            "merchantAccount": self._creds.merchant_code,
            "orderRefNo": reference_number,
            "lang": language,
            "signature": sig.cancel_unpaid_signature(
                order_ref_no=reference_number,
                merchant_account=self._creds.merchant_code,
                lang=language,
                secure_key=self._creds.secure_key,
            ),
        }
        await self._request("POST", CANCEL_UNPAID_PATH, json=body)

    # ------------------------------------------------------------ internals
    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx2.AsyncClient(
                base_url=self._base_url, transport=self._transport, timeout=self._timeout
            ) as http:
                response = await http.request(method, path, **kwargs)
        except httpx2.TimeoutException as exc:
            raise FawryUnavailable("Fawry didn't answer in time.") from exc
        except httpx2.HTTPError as exc:
            raise FawryUnavailable(f"Couldn't reach Fawry ({type(exc).__name__}).") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise FawryUnavailable(
                f"Fawry answered HTTP {response.status_code} with a body that isn't JSON."
            ) from exc
        if not isinstance(data, dict):
            raise FawryUnavailable(
                f"Fawry answered HTTP {response.status_code} with unexpected JSON."
            )

        status_code = data.get("statusCode")
        try:
            status_code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        # Log the outcome only: path, HTTP status, Fawry code. Never bodies.
        log.info(
            "fawry %s %s -> HTTP %s, statusCode %s", method, path, response.status_code, status_code
        )
        if response.status_code != 200 or status_code != 200:
            raise FawryError(
                status_code, str(data.get("statusDescription") or ""), response.status_code
            )
        return data

    def _payment_from(self, data: dict[str, Any], *, ref_field: str, merchant_ref: str) -> Payment:
        self._verify_response_signature(data, ref_field=ref_field)
        # A genuine, signed answer about another order (replayed by someone in the middle) must
        # not be read as this order's status.
        answered_ref = str(data.get("merchantRefNumber") or data.get("merchantRefNum"))
        if answered_ref != merchant_ref:
            raise ResponseSignatureError("Fawry's response is about a different order.")
        raw_status = str(data.get("orderStatus") or "")
        order_amount = _decimal(data.get("orderAmount"))
        payment_amount = _decimal(data.get("paymentAmount"))
        if order_amount is None or payment_amount is None:
            raise FawryUnavailable("Fawry's response is missing the order or payment amount.")
        return Payment(
            reference_number=data.get(ref_field) or data.get("referenceNumber"),
            merchant_ref_number=str(data.get("merchantRefNumber") or data.get("merchantRefNum")),
            raw_status=raw_status,
            status=NORMALIZED_STATUS.get(raw_status.upper(), "unknown"),
            order_amount=order_amount,
            payment_amount=payment_amount,
            fawry_fees=_decimal(data.get("fawryFees")),
            payment_method=str(data.get("paymentMethod") or ""),
            paid_at=_epoch_ms_to_utc(data.get("paymentTime")),
            refunded_amount=_decimal(data.get("refundedAmount")),
        )

    def _verify_response_signature(self, data: dict[str, Any], *, ref_field: str) -> None:
        """Official charge-response recipe (also assumed for status v2, verify list V2)."""
        try:
            expected = sig.charge_response_signature(
                reference_number=data.get(ref_field),
                merchant_ref_num=str(data.get("merchantRefNumber") or data.get("merchantRefNum")),
                payment_amount=data["paymentAmount"],
                order_amount=data["orderAmount"],
                order_status=data["orderStatus"],
                payment_method=data["paymentMethod"],
                fawry_fees=data.get("fawryFees"),
                shipping_fees=data.get("shippingFees"),
                auth_number=data.get("authNumber"),
                customer_mail=data.get("customerMail"),
                customer_mobile=data.get("customerMobile"),
                secure_key=self._creds.secure_key,
            )
        except (KeyError, ValueError) as exc:
            raise ResponseSignatureError(
                "Fawry's response is missing fields needed to verify its signature."
            ) from exc
        if not sig.signatures_match(expected, data.get("signature")):
            raise ResponseSignatureError("Fawry's response signature didn't verify.")
