"""End-to-end tests for the Fawry mock, through HTTP (FastAPI TestClient)."""

import logging
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from uvicorn.logging import AccessFormatter

from masr_pay_mcp.core.redaction import RedactingFilter
from masr_pay_mcp.providers.fawry import signatures as sig
from mock_fawry.__main__ import redacted_log_config
from mock_fawry.app import (
    CANCEL_UNPAID_PATH,
    CHARGE_PATH,
    REFUND_PATH,
    STATUS_V2_PATH,
    MockSettings,
    create_app,
)

MERCHANT = "TESTMERCHANT01"
KEY = "fake-test-key"
T0 = 1_790_000_000_000  # an arbitrary "now", in epoch ms
HOUR_MS = 3_600_000


class FakeClock:
    def __init__(self, now_ms: int = T0):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


@pytest.fixture
def clock():
    return FakeClock()


def make_client(clock, fees="0.00"):
    settings = MockSettings(
        merchant_code=MERCHANT, secure_key=KEY, fawry_fees=Decimal(fees), clock=clock
    )
    return TestClient(create_app(settings))


@pytest.fixture
def client(clock):
    return make_client(clock)


# ------------------------------------------------------------------ helpers
def charge_body(ref="ORDER-42", amount="350.00", **overrides):
    body = {
        "merchantCode": MERCHANT,
        "merchantRefNum": ref,
        "paymentMethod": "PayAtFawry",
        "customerName": "Ahmed",
        "customerMobile": "01012345678",
        "customerEmail": "ahmed@example.com",
        "amount": float(amount),
        "description": "Order 42",
        "language": "en-gb",
        "chargeItems": [
            {"itemId": "42", "description": "Order 42", "price": float(amount), "quantity": 1}
        ],
        "paymentExpiry": T0 + 72 * HOUR_MS,
    }
    body.update(overrides)
    if "signature" not in body:  # not setdefault: its default would be computed eagerly
        body["signature"] = sig.charge_request_signature(
            merchant_code=body["merchantCode"],
            merchant_ref_num=body["merchantRefNum"],
            customer_profile_id=body.get("customerProfileId"),
            payment_method=body["paymentMethod"],
            amount=body["amount"],
            secure_key=KEY,
        )
    return body


def create(client, **kwargs):
    resp = client.post(CHARGE_PATH, json=charge_body(**kwargs))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["statusCode"] == 200, data
    return data


def status(client, ref="ORDER-42", signature=None, merchant=MERCHANT):
    if signature is None:
        signature = sig.status_v2_signature(
            merchant_code=merchant, merchant_ref_number=ref, secure_key=KEY
        )
    return client.get(
        STATUS_V2_PATH,
        params={"merchantCode": merchant, "merchantRefNumber": ref, "signature": signature},
    )


def refund(client, reference, amount="350.00", reason="Customer returned item", signature=None):
    body = {
        "merchantCode": MERCHANT,
        "referenceNumber": reference,
        "refundAmount": float(amount),
        "reason": reason,
    }
    body["signature"] = signature or sig.refund_signature(
        merchant_code=MERCHANT,
        reference_number=reference,
        refund_amount=amount,
        reason=reason,
        secure_key=KEY,
    )
    return client.post(REFUND_PATH, json=body)


def cancel(client, reference, signature=None):
    body = {"merchantAccount": MERCHANT, "orderRefNo": reference, "lang": "en-gb"}
    body["signature"] = signature or sig.cancel_unpaid_signature(
        order_ref_no=reference, merchant_account=MERCHANT, lang="en-gb", secure_key=KEY
    )
    return client.post(CANCEL_UNPAID_PATH, json=body)


def kiosk_pay(client, reference):
    return client.post(f"/__admin/kiosk-pay/{reference}")


def assert_response_signature(data, ref_field):
    """Verify a response signature the way our client will: from the response fields alone."""
    expected = sig.charge_response_signature(
        reference_number=data.get(ref_field),
        merchant_ref_num=data["merchantRefNumber"],
        payment_amount=data["paymentAmount"],
        order_amount=data["orderAmount"],
        order_status=data["orderStatus"],
        payment_method=data["paymentMethod"],
        fawry_fees=data.get("fawryFees"),
        shipping_fees=data.get("shippingFees"),
        auth_number=data.get("authNumber"),
        customer_mail=data.get("customerMail"),
        customer_mobile=data.get("customerMobile"),
        secure_key=KEY,
    )
    assert sig.signatures_match(expected, data["signature"])


# ------------------------------------------------------------------ charge
def test_charge_returns_signed_unpaid_reference(client):
    data = create(client)
    assert data["type"] == "ChargeResponse"
    assert data["statusDescription"] == "Operation done successfully"
    assert len(data["referenceNumber"]) == 9 and data["referenceNumber"].isdigit()
    assert data["merchantRefNumber"] == "ORDER-42"
    assert data["orderStatus"] == "UNPAID"
    assert data["orderAmount"] == 350.0
    assert data["paymentMethod"] == "PayAtFawry"
    assert_response_signature(data, "referenceNumber")


def test_charge_fees_are_added_to_payment_amount_and_signed(clock):
    client = make_client(clock, fees="5.00")
    data = create(client, amount="580.55")
    assert data["orderAmount"] == 580.55
    assert data["fawryFees"] == 5.0
    assert data["paymentAmount"] == 585.55
    assert_response_signature(data, "referenceNumber")


def test_charge_signature_with_customer_profile_id(client):
    data = create(client, customerProfileId="CUST-7")
    assert data["customerProfileId"] == "CUST-7"


def test_tampered_response_fails_verification(client):
    data = create(client)
    data["orderStatus"] = "PAID"
    with pytest.raises(AssertionError):
        assert_response_signature(data, "referenceNumber")


def test_charge_wrong_merchant_code_is_9901(client):
    resp = client.post(CHARGE_PATH, json=charge_body(merchantCode="SOMEONE-ELSE"))
    assert resp.status_code == 200
    assert resp.json()["statusCode"] == 9901


@pytest.mark.parametrize(
    "bad_signature",
    [
        "",
        "0" * 64,
        # Signed with the wrong amount (tampered body).
        sig.charge_request_signature(
            merchant_code=MERCHANT,
            merchant_ref_num="ORDER-42",
            customer_profile_id=None,
            payment_method="PayAtFawry",
            amount="1.00",
            secure_key=KEY,
        ),
        # Signed with a different key.
        sig.charge_request_signature(
            merchant_code=MERCHANT,
            merchant_ref_num="ORDER-42",
            customer_profile_id=None,
            payment_method="PayAtFawry",
            amount="350.00",
            secure_key="not-the-key",
        ),
    ],
)
def test_charge_bad_signature_is_9946(client, bad_signature):
    resp = client.post(CHARGE_PATH, json=charge_body(signature=bad_signature))
    assert resp.json()["statusCode"] == 9946
    assert client.get("/__admin/orders").json() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"customerMobile": "1012345678"},
        {"customerMobile": "+201012345678"},
        {"customerEmail": "not-an-email"},
        {"paymentMethod": "CARD"},
        {"language": "fr-fr"},
        {"chargeItems": []},
        {"amount": 0},
        {"amount": 350.555},
        {"currencyCode": "USD"},
    ],
)
def test_charge_invalid_fields_are_mock_422(client, overrides):
    body = charge_body(signature="x" * 64, **overrides)
    resp = client.post(CHARGE_PATH, json=body)
    assert resp.status_code == 422
    data = resp.json()
    assert data["statusCode"] == 422
    assert data["statusDescription"].startswith("[mock]")


def test_charge_missing_field_is_mock_422(client):
    body = charge_body()
    del body["customerMobile"]
    resp = client.post(CHARGE_PATH, json=body)
    assert resp.status_code == 422
    assert "customerMobile" in resp.json()["statusDescription"]


def test_charge_non_json_is_mock_400(client):
    resp = client.post(CHARGE_PATH, content=b"not json", headers={"content-type": "text/plain"})
    assert resp.status_code == 400


def test_charge_duplicate_merchant_ref_is_mock_409(client):
    create(client)
    resp = client.post(CHARGE_PATH, json=charge_body())
    assert resp.status_code == 409
    assert resp.json()["statusDescription"].startswith("[mock]")


# ------------------------------------------------------------------ status
def test_status_of_new_order_is_unpaid_and_signed(client):
    ref = create(client)["referenceNumber"]
    resp = status(client)
    data = resp.json()
    assert data["type"] == "PaymentStatusResponse"
    assert data["statusCode"] == 200
    assert data["fawryRefNumber"] == ref
    assert data["orderStatus"] == "UNPAID"
    assert data["requestId"]
    assert "paymentTime" not in data
    assert_response_signature(data, "fawryRefNumber")


def test_status_unknown_order_is_9938(client):
    assert status(client, ref="NOPE").json()["statusCode"] == 9938


def test_status_bad_signature_is_9946(client):
    create(client)
    assert status(client, signature="0" * 64).json()["statusCode"] == 9946


def test_status_wrong_merchant_is_9901(client):
    assert status(client, merchant="OTHER").json()["statusCode"] == 9901


# ------------------------------------------------------------------ kiosk payment
def test_kiosk_payment_marks_order_paid_with_payment_time(client, clock):
    ref = create(client)["referenceNumber"]
    clock.advance(HOUR_MS)
    resp = kiosk_pay(client, ref)
    assert resp.status_code == 200
    assert resp.json()["orderStatus"] == "PAID"

    data = status(client).json()
    assert data["orderStatus"] == "PAID"
    assert data["paymentTime"] == T0 + HOUR_MS
    assert_response_signature(data, "fawryRefNumber")


def test_kiosk_pay_twice_is_409(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    assert kiosk_pay(client, ref).status_code == 409


def test_kiosk_pay_unknown_reference_is_404(client):
    assert kiosk_pay(client, "999999999").status_code == 404


# ------------------------------------------------------------------ expiry
def test_order_expires_after_payment_expiry(client, clock):
    ref = create(client, paymentExpiry=T0 + HOUR_MS)["referenceNumber"]
    clock.advance(HOUR_MS)  # exactly at expiry: still payable
    assert status(client).json()["orderStatus"] == "UNPAID"
    clock.advance(1)
    assert status(client).json()["orderStatus"] == "EXPIRED"
    assert kiosk_pay(client, ref).status_code == 409


def test_order_without_expiry_does_not_expire(client, clock):
    body = charge_body()
    del body["paymentExpiry"]
    client.post(CHARGE_PATH, json=body)
    clock.advance(365 * 24 * HOUR_MS)
    assert status(client).json()["orderStatus"] == "UNPAID"


# ------------------------------------------------------------------ cancel
def test_cancel_unpaid_order(client):
    ref = create(client)["referenceNumber"]
    resp = cancel(client, ref)
    assert resp.json()["statusCode"] == 200
    assert status(client).json()["orderStatus"] == "CANCELED"
    assert kiosk_pay(client, ref).status_code == 409  # can't pay a cancelled reference


def test_cancel_paid_order_is_mock_409(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    resp = cancel(client, ref)
    assert resp.status_code == 409
    assert status(client).json()["orderStatus"] == "PAID"


def test_cancel_bad_signature_is_9946(client):
    ref = create(client)["referenceNumber"]
    assert cancel(client, ref, signature="0" * 64).json()["statusCode"] == 9946
    assert status(client).json()["orderStatus"] == "UNPAID"


def test_cancel_unknown_order_is_9938(client):
    assert cancel(client, "999999999").json()["statusCode"] == 9938


# ------------------------------------------------------------------ refund
def test_refund_paid_order(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    resp = refund(client, ref)
    assert resp.json() == {
        "type": "ResponseDataModel",
        "statusCode": 200,
        "statusDescription": "Operation done successfully",
    }
    data = status(client).json()
    assert data["orderStatus"] == "REFUNDED"
    assert data["refundedAmount"] == 350.0
    assert_response_signature(data, "fawryRefNumber")


def test_partial_refund_is_allowed(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    assert refund(client, ref, amount="100.00").json()["statusCode"] == 200
    assert status(client).json()["refundedAmount"] == 100.0


def test_refund_unpaid_order_is_9954(client):
    ref = create(client)["referenceNumber"]
    assert refund(client, ref).json()["statusCode"] == 9954


def test_refund_more_than_paid_is_9935(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    assert refund(client, ref, amount="350.01").json()["statusCode"] == 9935
    assert status(client).json()["orderStatus"] == "PAID"


def test_only_one_refund_per_order(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    assert refund(client, ref, amount="100.00").json()["statusCode"] == 200
    resp = refund(client, ref, amount="50.00")
    assert resp.status_code == 409
    assert "one refund" in resp.json()["statusDescription"]


def test_refund_bad_signature_is_9946(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    assert refund(client, ref, signature="0" * 64).json()["statusCode"] == 9946
    assert status(client).json()["orderStatus"] == "PAID"


def test_refund_signature_covers_arabic_reason(client):
    ref = create(client)["referenceNumber"]
    kiosk_pay(client, ref)
    assert refund(client, ref, reason="منتج تالف").json()["statusCode"] == 200


def test_refund_unknown_order_is_9938(client):
    assert refund(client, "999999999").json()["statusCode"] == 9938


# ------------------------------------------------------------------ admin
def test_admin_orders_and_reset(client):
    create(client, ref="A")
    create(client, ref="B")
    orders = client.get("/__admin/orders").json()
    assert {o["merchantRefNum"] for o in orders} == {"A", "B"}
    assert client.post("/__admin/reset").json() == {"cleared": 2}
    assert client.get("/__admin/orders").json() == []


# ------------------------------------------------------------------ demo flow
def test_milestone_demo_flow(client):
    """Charge 150 EGP for order 42 -> customer pays at an outlet -> 'has it been paid?' -> yes."""
    ref = create(client, ref="42", amount="150")["referenceNumber"]
    assert status(client, ref="42").json()["orderStatus"] == "UNPAID"
    kiosk_pay(client, ref)
    assert status(client, ref="42").json()["orderStatus"] == "PAID"


# ------------------------------------------------------------------ logging
def test_runner_log_config_redacts_on_every_handler():
    config = redacted_log_config()
    assert config["filters"]["redact"]["()"] == "masr_pay_mcp.core.redaction.RedactingFilter"
    assert all("redact" in h["filters"] for h in config["handlers"].values())


def test_access_log_line_has_no_signature():
    signature = sig.status_v2_signature(
        merchant_code=MERCHANT, merchant_ref_number="ORDER-42", secure_key=KEY
    )
    # Same shape as uvicorn's access-log record: (client, method, path, http version, status).
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:5000",
            "GET",
            f"{STATUS_V2_PATH}?merchantCode={MERCHANT}&merchantRefNumber=ORDER-42"
            f"&signature={signature}",
            "1.1",
            200,
        ),
        None,
    )
    RedactingFilter().filter(record)
    # Format with uvicorn's real access formatter: it unpacks record.args, so the
    # filter has to keep them as a 5-tuple (a live run caught this once).
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert signature not in line
    assert "signature=***" in line
    assert "merchantRefNumber=ORDER-42" in line
