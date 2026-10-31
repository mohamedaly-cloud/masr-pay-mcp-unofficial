import json
from decimal import Decimal
from pathlib import Path

import pytest

from masr_pay_mcp.providers.fawry import signatures as sig

VECTORS_FILE = json.loads((Path(__file__).parents[2] / "vectors.json").read_text(encoding="utf-8"))
SECURE_KEY = VECTORS_FILE["secure_key"]
VECTORS = VECTORS_FILE["vectors"]

RECIPES = {
    "charge_request": (sig.charge_request_plain, sig.charge_request_signature),
    "charge_response": (sig.charge_response_plain, sig.charge_response_signature),
    "status_v2": (sig.status_v2_plain, sig.status_v2_signature),
    "refund": (sig.refund_plain, sig.refund_signature),
    "cancel_unpaid": (sig.cancel_unpaid_plain, sig.cancel_unpaid_signature),
}


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vector_plain_string(vector):
    plain_fn, _ = RECIPES[vector["recipe"]]
    assert plain_fn(**vector["inputs"], secure_key=SECURE_KEY) == vector["plain"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vector_signature(vector):
    _, sign_fn = RECIPES[vector["recipe"]]
    assert sign_fn(**vector["inputs"], secure_key=SECURE_KEY) == vector["sha256"]


def test_every_recipe_has_a_vector():
    assert {v["recipe"] for v in VECTORS} == set(RECIPES)


# ---------------------------------------------------------------- amounts
@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (350, "350.00"),
        ("350", "350.00"),
        ("350.5", "350.50"),
        (580.55, "580.55"),  # float must not leak binary noise (580.549999...)
        (Decimal("20.00"), "20.00"),
        (" 15.10 ", "15.10"),
        (0, "0.00"),
    ],
)
def test_format_amount(amount, expected):
    assert sig.format_amount(amount) == expected


@pytest.mark.parametrize("amount", ["580.555", "abc", "", "-1", "NaN", "Infinity", True, None])
def test_format_amount_rejects_bad_input(amount):
    with pytest.raises((ValueError, TypeError)):
        sig.format_amount(amount)


# ---------------------------------------------------------------- doc traps
def test_charge_does_not_repeat_merchant_code():
    """Fawry's JS sample does merchantCode.concat(merchantCode, ...). We must not."""
    plain = sig.charge_request_plain(
        merchant_code="M1",
        merchant_ref_num="R1",
        customer_profile_id=None,
        payment_method="PayAtFawry",
        amount="1",
        secure_key="K",
    )
    assert plain == "M1R1PayAtFawry1.00K"


def test_signature_is_lowercase_hex_sha256():
    s = sig.status_v2_signature(merchant_code="M", merchant_ref_number="R", secure_key="K")
    assert len(s) == 64
    assert s == s.lower()
    int(s, 16)


def test_secure_key_changes_signature():
    a = sig.status_v2_signature(merchant_code="M", merchant_ref_number="R", secure_key="K1")
    b = sig.status_v2_signature(merchant_code="M", merchant_ref_number="R", secure_key="K2")
    assert a != b


# ---------------------------------------------------------------- comparison
def test_signatures_match():
    s = sig.sha256_hex("x")
    assert sig.signatures_match(s, s)
    assert sig.signatures_match(s, s.upper())
    assert not sig.signatures_match(s, sig.sha256_hex("y"))
    assert not sig.signatures_match(s, "")
    assert not sig.signatures_match(s, None)
