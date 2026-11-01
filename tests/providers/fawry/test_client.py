"""FawryClient against the mock (in-process) and against hand-made bad responses."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx2
import pytest

from masr_pay_mcp.providers.fawry import signatures as sig
from masr_pay_mcp.providers.fawry.client import (
    FawryClient,
    FawryError,
    FawryUnavailable,
    ResponseSignatureError,
)
from masr_pay_mcp.providers.fawry.config import Credentials
from tests.conftest import HOUR_MS, KEY, MERCHANT, T0

BASE = "http://mock-fawry"
pytestmark = pytest.mark.anyio


@pytest.fixture
def client(credentials, transport):
    return FawryClient(BASE, credentials, transport=transport)


async def create(client, ref="ORDER-42", amount="350.00", **kwargs):
    return await client.create_pay_at_fawry_charge(
        merchant_ref_num=ref,
        amount=Decimal(amount),
        customer_mobile="01012345678",
        customer_email="ahmed@example.com",
        description="Order 42",
        **kwargs,
    )


# ------------------------------------------------------------------ happy paths
async def test_create_returns_normalized_verified_payment(client):
    p = await create(client, customer_name="Ahmed")
    assert len(p.reference_number) == 9
    assert p.merchant_ref_number == "ORDER-42"
    assert p.raw_status == "UNPAID"
    assert p.status == "awaiting_payment"
    assert p.order_amount == Decimal("350.00")
    assert p.fawry_fees == Decimal("5.00")
    assert p.payment_amount == Decimal("355.00")
    assert p.paid_at is None


async def test_status_normalizes_fawry_ref_number_field(client, mock_app):
    created = await create(client)
    status = await client.get_payment_status("ORDER-42")
    assert status.reference_number == created.reference_number  # came from fawryRefNumber
    assert status.status == "awaiting_payment"


async def test_paid_status_has_utc_payment_time(client, mock_app, clock):
    created = await create(client)
    clock.advance(HOUR_MS)
    mock_app.state.store.kiosk_pay(created.reference_number)
    status = await client.get_payment_status("ORDER-42")
    assert status.is_paid
    assert status.paid_at == datetime.fromtimestamp((T0 + HOUR_MS) / 1000, tz=UTC)


async def test_payment_expiry_is_sent_in_epoch_milliseconds(client, mock_app, clock):
    await create(client, payment_expiry=datetime.fromtimestamp((T0 + HOUR_MS) / 1000, tz=UTC))
    order = mock_app.state.store.get_by_merchant_ref(MERCHANT, "ORDER-42")
    assert order.payment_expiry_ms == T0 + HOUR_MS
    clock.advance(HOUR_MS + 1)
    assert (await client.get_payment_status("ORDER-42")).status == "expired"


async def test_refund_and_cancel(client, mock_app):
    paid = await create(client, ref="PAID-1")
    mock_app.state.store.kiosk_pay(paid.reference_number)
    await client.refund(
        reference_number=paid.reference_number, amount=Decimal("100"), reason="منتج تالف"
    )
    refunded = await client.get_payment_status("PAID-1")
    assert refunded.status == "refunded"
    assert refunded.refunded_amount == Decimal("100.00")

    unpaid = await create(client, ref="UNPAID-1")
    await client.cancel_unpaid(reference_number=unpaid.reference_number, language="ar-eg")
    assert (await client.get_payment_status("UNPAID-1")).status == "canceled"


# ------------------------------------------------------------------ Fawry errors
async def test_wrong_secure_key_is_rejected_as_9946(transport):
    client = FawryClient(BASE, Credentials(MERCHANT, "wrong-key"), transport=transport)
    with pytest.raises(FawryError) as exc_info:
        await create(client)
    assert exc_info.value.status_code == 9946
    assert "FAWRY_SECURE_KEY" in exc_info.value.meaning


async def test_wrong_merchant_is_9901(transport):
    client = FawryClient(BASE, Credentials("NOPE", KEY), transport=transport)
    with pytest.raises(FawryError) as exc_info:
        await client.get_payment_status("ORDER-42")
    assert exc_info.value.status_code == 9901


async def test_unknown_order_is_9938(client):
    with pytest.raises(FawryError) as exc_info:
        await client.get_payment_status("NOPE")
    assert exc_info.value.status_code == 9938


async def test_refund_of_unpaid_order_is_9954(client):
    p = await create(client)
    with pytest.raises(FawryError) as exc_info:
        await client.refund(reference_number=p.reference_number, amount=Decimal("1"), reason="x")
    assert exc_info.value.status_code == 9954


async def test_mock_only_error_keeps_http_status(client):
    await create(client)
    with pytest.raises(FawryError) as exc_info:
        await create(client)  # duplicate merchantRefNum
    assert exc_info.value.http_status == 409
    assert exc_info.value.meaning.startswith("[mock]")


async def test_amount_with_three_decimals_is_rejected_before_sending(client):
    with pytest.raises(ValueError):
        await create(client, amount="1.005")


# ------------------------------------------------------------------ response integrity
def _signed_status_body(**overrides):
    body = {
        "type": "PaymentStatusResponse",
        "fawryRefNumber": "123456789",
        "merchantRefNumber": "ORDER-42",
        "orderAmount": 350.0,
        "paymentAmount": 350.0,
        "fawryFees": 0.0,
        "orderStatus": "PAID",
        "paymentMethod": "PayAtFawry",
        "paymentTime": T0,
        "statusCode": 200,
        "statusDescription": "Operation done successfully",
    }
    body["signature"] = sig.charge_response_signature(
        reference_number=body["fawryRefNumber"],
        merchant_ref_num=body["merchantRefNumber"],
        payment_amount=body["paymentAmount"],
        order_amount=body["orderAmount"],
        order_status=body["orderStatus"],
        payment_method=body["paymentMethod"],
        fawry_fees=body["fawryFees"],
        secure_key=KEY,
    )
    body.update(overrides)
    return body


def _client_returning(response: httpx2.Response | Exception, credentials) -> FawryClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if isinstance(response, Exception):
            raise response
        return response

    return FawryClient(BASE, credentials, transport=httpx2.MockTransport(handler))


async def test_correctly_signed_hand_made_response_is_accepted(credentials):
    client = _client_returning(httpx2.Response(200, json=_signed_status_body()), credentials)
    assert (await client.get_payment_status("ORDER-42")).is_paid


@pytest.mark.parametrize(
    "tamper",
    [
        {"orderStatus": "REFUNDED"},  # status changed after signing
        {"paymentAmount": 1.0},  # amount changed after signing
        {"signature": "0" * 64},
        {"signature": None},
    ],
)
async def test_tampered_response_is_refused(credentials, tamper):
    client = _client_returning(
        httpx2.Response(200, json=_signed_status_body(**tamper)), credentials
    )
    with pytest.raises(ResponseSignatureError):
        await client.get_payment_status("ORDER-42")


async def test_genuine_response_about_another_order_is_refused(credentials):
    """A correctly signed "PAID" for ORDER-41, replayed when we asked about ORDER-42."""
    body = _signed_status_body()  # signed for ORDER-42
    client = _client_returning(httpx2.Response(200, json=body), credentials)
    with pytest.raises(ResponseSignatureError, match="different order"):
        await client.get_payment_status("ORDER-41")


async def test_response_missing_fields_is_refused(credentials):
    body = _signed_status_body()
    del body["orderStatus"]
    client = _client_returning(httpx2.Response(200, json=body), credentials)
    with pytest.raises(ResponseSignatureError):
        await client.get_payment_status("ORDER-42")


@pytest.mark.parametrize(
    "response",
    [
        httpx2.ConnectError("boom"),
        httpx2.ReadTimeout("slow"),
        httpx2.Response(502, content=b"<html>Bad gateway</html>"),
        httpx2.Response(200, content=json.dumps([1, 2]).encode()),
    ],
)
async def test_transport_problems_raise_unavailable(credentials, response):
    client = _client_returning(response, credentials)
    with pytest.raises(FawryUnavailable):
        await client.get_payment_status("ORDER-42")


async def test_request_signature_and_fields_on_the_wire(credentials):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"statusCode": 9946, "statusDescription": "x"})

    client = FawryClient(BASE, credentials, transport=httpx2.MockTransport(handler))
    with pytest.raises(FawryError):
        await create(client, amount="20")
    body = seen["body"]
    assert body["amount"] == 20.0
    assert body["paymentMethod"] == "PayAtFawry"
    assert body["currencyCode"] == "EGP"
    assert body["signature"] == sig.sha256_hex(f"{MERCHANT}ORDER-42PayAtFawry20.00{KEY}")
