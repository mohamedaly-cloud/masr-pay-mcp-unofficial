"""FastAPI mock of the FawryPay staging API (Pay-at-Fawry subset).

Uses the same paths as Fawry, checks signatures against ``MOCK_SECURE_KEY``,
returns Fawry-shaped JSON (``statusCode`` / ``statusDescription``) and signs
charge and status responses with the official response-signature recipe, so
our client's verification code actually runs.

Error conventions (see docs/decisions.md):
  * Fawry business errors (9901, 9938, 9946, 9935, 9954) are returned as
    HTTP 200 with the Fawry ``statusCode``. Our client must always check
    ``statusCode``, never just the HTTP status.
  * When Fawry's error code for a case isn't documented (malformed body,
    duplicate merchantRefNum, cancelling a paid order, second refund), the
    mock returns HTTP 4xx with ``statusCode`` equal to the HTTP status and a
    ``statusDescription`` that starts with "[mock]". These codes are
    deliberately not made to look like Fawry's.

Admin endpoints under ``/__admin`` exist only in the mock.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from masr_pay_mcp.providers.fawry import signatures as sig
from mock_fawry.state import (
    PAID,
    REFUNDED,
    Clock,
    DuplicateOrder,
    InvalidTransition,
    OrderNotFound,
    OrderStore,
    system_clock_ms,
)

CHARGE_PATH = "/ECommerceWeb/Fawry/payments/charge"
STATUS_V2_PATH = "/ECommerceWeb/Fawry/payments/status/v2"
REFUND_PATH = "/ECommerceWeb/Fawry/payments/refund"
CANCEL_UNPAID_PATH = "/ECommerceWeb/api/orders/cancel-unpaid-order"

# Official error codes (docs/design.md "Error codes").
OK = 200
INVALID_MERCHANT = 9901
ORDER_NOT_FOUND = 9938
INVALID_SIGNATURE = 9946
REFUND_EXCEEDS_PAID = 9935
ORDER_NOT_PAID = 9954

DESCRIPTIONS = {
    OK: "Operation done successfully",
    INVALID_MERCHANT: "Merchant code blank or invalid",
    ORDER_NOT_FOUND: "Order not found",
    INVALID_SIGNATURE: "Blank or invalid signature",
    REFUND_EXCEEDS_PAID: "Refunded amount greater than paid amount",
    ORDER_NOT_PAID: "Order is not paid",
}

DEFAULT_MERCHANT_CODE = "MOCKMERCHANT0001"
# Obviously fake default so `python -m mock_fawry` works out of the box. Never a real key.
DEFAULT_SECURE_KEY = "mock-secure-key-not-a-real-fawry-key"


@dataclass
class MockSettings:
    merchant_code: str = DEFAULT_MERCHANT_CODE
    secure_key: str = DEFAULT_SECURE_KEY
    fawry_fees: Decimal = Decimal("0.00")
    clock: Clock = field(default=system_clock_ms)

    @classmethod
    def from_env(cls) -> MockSettings:
        return cls(
            merchant_code=os.environ.get("MOCK_MERCHANT_CODE", DEFAULT_MERCHANT_CODE),
            secure_key=os.environ.get("MOCK_SECURE_KEY", DEFAULT_SECURE_KEY),
            fawry_fees=Decimal(sig.format_amount(os.environ.get("MOCK_FAWRY_FEES", "0"))),
        )


# ----------------------------------------------------------------- models
def _two_decimals(value: Decimal) -> Decimal:
    sig.format_amount(value)  # raises ValueError if it has more than two decimals
    return value


Mobile = Annotated[str, Field(pattern=r"^01\d{9}$")]
Money = Annotated[Decimal, Field(gt=0)]


class _Body(BaseModel):
    # Ignore unknown fields the way a tolerant API would; reject wrong types.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ChargeItem(_Body):
    itemId: str = Field(min_length=1)
    description: str | None = None
    price: Money
    quantity: int = Field(ge=1)

    _check_price = field_validator("price")(_two_decimals)


class ChargeRequest(_Body):
    merchantCode: str
    merchantRefNum: str = Field(min_length=1)
    paymentMethod: Literal["PayAtFawry"]
    customerMobile: Mobile
    customerEmail: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    amount: Money
    description: str = Field(min_length=1)
    language: Literal["ar-eg", "en-gb"]
    chargeItems: list[ChargeItem] = Field(min_length=1)
    signature: str = ""
    customerProfileId: str | None = None
    customerName: str | None = None
    paymentExpiry: int | None = Field(default=None, gt=0)  # epoch milliseconds
    orderWebHookUrl: str | None = None
    currencyCode: Literal["EGP"] | None = None

    _check_amount = field_validator("amount")(_two_decimals)


class RefundRequest(_Body):
    merchantCode: str
    referenceNumber: str = Field(min_length=1)
    refundAmount: Money
    reason: str | None = None
    signature: str = ""

    _check_amount = field_validator("refundAmount")(_two_decimals)


class CancelUnpaidRequest(_Body):
    merchantAccount: str
    orderRefNo: str = Field(min_length=1)
    lang: Literal["ar-eg", "en-gb"]
    signature: str = ""


# ----------------------------------------------------------------- helpers
def fawry_result(code: int, response_type: str | None = None, **extra: Any) -> JSONResponse:
    """A Fawry-shaped response. Fawry signals business errors in ``statusCode`` over HTTP 200."""
    body: dict[str, Any] = {}
    if response_type:
        body["type"] = response_type
    body.update(extra)
    body["statusCode"] = code
    body["statusDescription"] = DESCRIPTIONS[code]
    return JSONResponse(body, status_code=200)


def mock_error(http_status: int, message: str, response_type: str | None = None) -> JSONResponse:
    """An error Fawry doesn't document a code for. Clearly labelled as coming from the mock."""
    body: dict[str, Any] = {}
    if response_type:
        body["type"] = response_type
    body["statusCode"] = http_status
    body["statusDescription"] = f"[mock] {message}"
    return JSONResponse(body, status_code=http_status)


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "body"
        parts.append(f"{loc}: {err['msg']}")
    return "invalid request: " + "; ".join(parts)


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _money(value: Decimal) -> float:
    # Fawry sends amounts as JSON numbers. Every amount here has at most two
    # decimals, so the float round-trips cleanly.
    return float(value)


# ----------------------------------------------------------------- app
def create_app(settings: MockSettings | None = None) -> FastAPI:
    settings = settings or MockSettings.from_env()
    store = OrderStore(clock=settings.clock)

    app = FastAPI(
        title="Mock FawryPay API (unofficial)",
        description=(
            "Development mock of the FawryPay staging API, part of masr-pay-mcp-unofficial. "
            "Not Fawry, not affiliated with or endorsed by Fawry. No real money moves."
        ),
        version="0.1.0",
    )
    app.state.store = store
    app.state.settings = settings

    def signed_order_fields(order, ref_field: str) -> dict[str, Any]:
        """Order fields plus a response signature (official charge-response recipe)."""
        signature = sig.charge_response_signature(
            reference_number=order.reference_number,
            merchant_ref_num=order.merchant_ref_num,
            payment_amount=order.payment_amount,
            order_amount=order.order_amount,
            order_status=order.status,
            payment_method=order.payment_method,
            fawry_fees=order.fawry_fees,
            customer_mail=order.customer_email,
            customer_mobile=order.customer_mobile,
            secure_key=settings.secure_key,
        )
        fields: dict[str, Any] = {
            ref_field: order.reference_number,
            "merchantRefNumber": order.merchant_ref_num,
            "orderAmount": _money(order.order_amount),
            "paymentAmount": _money(order.payment_amount),
            "fawryFees": _money(order.fawry_fees),
            "paymentMethod": order.payment_method,
            "orderStatus": order.status,
            "customerMobile": order.customer_mobile,
            "customerMail": order.customer_email,
        }
        if order.customer_name is not None:
            fields["customerName"] = order.customer_name
        if order.customer_profile_id is not None:
            fields["customerProfileId"] = order.customer_profile_id
        if order.payment_time_ms is not None:
            fields["paymentTime"] = order.payment_time_ms
        if order.refunded_amount is not None:
            fields["refundedAmount"] = _money(order.refunded_amount)
        fields["signature"] = signature
        return fields

    # ------------------------------------------------------------ 1. charge
    @app.post(CHARGE_PATH)
    async def charge(request: Request) -> JSONResponse:
        rtype = "ChargeResponse"
        body = await _json_body(request)
        if body is None:
            return mock_error(400, "request body must be a JSON object", rtype)
        if body.get("merchantCode") != settings.merchant_code:
            return fawry_result(INVALID_MERCHANT, rtype)
        try:
            req = ChargeRequest.model_validate(body)
        except ValidationError as exc:
            return mock_error(422, _validation_message(exc), rtype)

        expected = sig.charge_request_signature(
            merchant_code=req.merchantCode,
            merchant_ref_num=req.merchantRefNum,
            customer_profile_id=req.customerProfileId,
            payment_method=req.paymentMethod,
            amount=req.amount,
            secure_key=settings.secure_key,
        )
        if not sig.signatures_match(expected, req.signature):
            return fawry_result(INVALID_SIGNATURE, rtype)

        try:
            order = store.create(
                merchant_code=req.merchantCode,
                merchant_ref_num=req.merchantRefNum,
                order_amount=req.amount,
                fawry_fees=settings.fawry_fees,
                payment_method=req.paymentMethod,
                customer_mobile=req.customerMobile,
                customer_email=req.customerEmail,
                customer_name=req.customerName,
                customer_profile_id=req.customerProfileId,
                description=req.description,
                language=req.language,
                charge_items=[item.model_dump(mode="json") for item in req.chargeItems],
                payment_expiry_ms=req.paymentExpiry,
                order_webhook_url=req.orderWebHookUrl,
            )
        except DuplicateOrder as exc:
            return mock_error(409, str(exc), rtype)

        return fawry_result(OK, rtype, **signed_order_fields(order, "referenceNumber"))

    # ------------------------------------------------------------ 2. status v2
    @app.get(STATUS_V2_PATH)
    async def status_v2(
        merchantCode: str = "", merchantRefNumber: str = "", signature: str = ""
    ) -> JSONResponse:
        rtype = "PaymentStatusResponse"
        if merchantCode != settings.merchant_code:
            return fawry_result(INVALID_MERCHANT, rtype)
        if not merchantRefNumber:
            return mock_error(422, "invalid request: merchantRefNumber is required", rtype)
        expected = sig.status_v2_signature(
            merchant_code=merchantCode,
            merchant_ref_number=merchantRefNumber,
            secure_key=settings.secure_key,
        )
        if not sig.signatures_match(expected, signature):
            return fawry_result(INVALID_SIGNATURE, rtype)
        try:
            order = store.get_by_merchant_ref(merchantCode, merchantRefNumber)
        except OrderNotFound:
            return fawry_result(ORDER_NOT_FOUND, rtype)
        return fawry_result(
            OK,
            rtype,
            requestId=str(uuid.uuid4()),
            **signed_order_fields(order, "fawryRefNumber"),
        )

    # ------------------------------------------------------------ 3. refund
    @app.post(REFUND_PATH)
    async def refund(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if body is None:
            return mock_error(400, "request body must be a JSON object")
        if body.get("merchantCode") != settings.merchant_code:
            return fawry_result(INVALID_MERCHANT)
        try:
            req = RefundRequest.model_validate(body)
        except ValidationError as exc:
            return mock_error(422, _validation_message(exc))

        expected = sig.refund_signature(
            merchant_code=req.merchantCode,
            reference_number=req.referenceNumber,
            refund_amount=req.refundAmount,
            reason=req.reason,
            secure_key=settings.secure_key,
        )
        if not sig.signatures_match(expected, req.signature):
            return fawry_result(INVALID_SIGNATURE)
        try:
            order = store.get_by_reference(req.referenceNumber)
        except OrderNotFound:
            return fawry_result(ORDER_NOT_FOUND)
        if order.status == REFUNDED:
            # Reported by Checkout.com, not confirmed in Fawry's docs: one refund per payment.
            return mock_error(409, "order was already refunded; only one refund per order")
        if order.status != PAID:
            return fawry_result(ORDER_NOT_PAID)
        # Compared with the order amount: Fawry's fees aren't the merchant's to refund.
        if req.refundAmount > order.order_amount:
            return fawry_result(REFUND_EXCEEDS_PAID)
        store.refund(order.reference_number, req.refundAmount, req.reason)
        return fawry_result(OK, "ResponseDataModel")

    # ------------------------------------------------------------ 4. cancel unpaid
    @app.post(CANCEL_UNPAID_PATH)
    async def cancel_unpaid(request: Request) -> JSONResponse:
        body = await _json_body(request)
        if body is None:
            return mock_error(400, "request body must be a JSON object")
        if body.get("merchantAccount") != settings.merchant_code:
            return fawry_result(INVALID_MERCHANT)
        try:
            req = CancelUnpaidRequest.model_validate(body)
        except ValidationError as exc:
            return mock_error(422, _validation_message(exc))

        expected = sig.cancel_unpaid_signature(
            order_ref_no=req.orderRefNo,
            merchant_account=req.merchantAccount,
            lang=req.lang,
            secure_key=settings.secure_key,
        )
        if not sig.signatures_match(expected, req.signature):
            return fawry_result(INVALID_SIGNATURE)
        # Assumption (on the verify list): orderRefNo is Fawry's referenceNumber.
        try:
            store.cancel(req.orderRefNo)
        except OrderNotFound:
            return fawry_result(ORDER_NOT_FOUND)
        except InvalidTransition as exc:
            return mock_error(
                409, f"only UNPAID orders can be cancelled (status is {exc.order.status})"
            )
        return fawry_result(OK)

    # ------------------------------------------------------------ admin (mock only)
    @app.post("/__admin/kiosk-pay/{reference_number}")
    async def admin_kiosk_pay(reference_number: str) -> JSONResponse:
        """Simulate the customer paying cash for this reference at a Fawry outlet."""
        try:
            order = store.kiosk_pay(reference_number)
        except OrderNotFound:
            return mock_error(404, f"no order with referenceNumber {reference_number}")
        except InvalidTransition as exc:
            return mock_error(409, f"only UNPAID orders can be paid (status is {exc.order.status})")
        return JSONResponse(order.snapshot())

    @app.post("/__admin/reset")
    async def admin_reset() -> dict[str, int]:
        return {"cleared": store.reset()}

    @app.get("/__admin/orders")
    async def admin_orders() -> list[dict[str, Any]]:
        return [order.snapshot() for order in store.all()]

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
