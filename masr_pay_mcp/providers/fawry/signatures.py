"""FawryPay signature recipes.

Every Fawry signature is the hex-encoded SHA-256 of the plain concatenation of
some fields (no separators) followed by the merchant's secure key. Amounts are
formatted with exactly two decimals ("580.55", "20.00").

One public function per recipe. Each recipe is split into a ``*_plain`` builder
(the string that gets hashed) and the hashing function, so tests can check the
field order separately from the hash.

Confidence labels match docs/design.md:
  [official]    read directly on developer.fawrystaging.com
  [secondary]   consistent across several third-party SDKs/specs
  [third-party] single third-party source; still on the verify list

Known traps in Fawry's own docs, deliberately NOT copied here:
  * the docs' sample signatures are placeholders, not hashes of the sample
    inputs (the same value appears on pages with different inputs), so they
    are not used as test vectors (see tests/vectors.json). The documented
    formula itself hasn't been shown to be wrong;
  * the JavaScript sample concatenates merchantCode twice;
  * the Python sample hashes a ``str`` (which fails on Python 3). We always
    ``.encode("utf-8")``.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal, InvalidOperation

AmountLike = Decimal | int | float | str

_TWO_PLACES = Decimal("0.01")


def format_amount(amount: AmountLike) -> str:
    """Format an EGP amount with exactly two decimals, the way signatures need it.

    Raises ``ValueError`` if the amount isn't finite, is negative, or has more
    than two decimal places. We never round silently: the signed amount has to
    equal the amount in the request body, so rounding here would only produce
    a signature that doesn't match.
    """
    if isinstance(amount, bool):  # bool is a subclass of int; reject it explicitly
        raise ValueError(f"invalid amount: {amount!r}")
    try:
        # str() first so floats like 580.55 don't pick up binary noise.
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {amount!r}") from exc
    if not value.is_finite():
        raise ValueError(f"invalid amount: {amount!r}")
    if value < 0:
        raise ValueError(f"amount must not be negative: {amount!r}")
    quantized = value.quantize(_TWO_PLACES)
    if quantized != value:
        raise ValueError(f"amount has more than two decimal places: {amount!r}")
    return f"{quantized:.2f}"


def sha256_hex(plain: str) -> str:
    """Hex-encoded SHA-256 of a UTF-8 string."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def signatures_match(expected: str, received: str | None) -> bool:
    """Compare two hex signatures in constant time (case-insensitive hex)."""
    if not received:
        return False
    return hmac.compare_digest(expected.lower(), received.strip().lower())


def _opt(value: object | None) -> str:
    """Optional text field: empty string when absent."""
    return "" if value is None else str(value)


def _opt_amount(value: AmountLike | None) -> str:
    """Optional amount field: empty string when absent, else two decimals."""
    return "" if value is None else format_amount(value)


# --------------------------------------------------------------------------
# 1. Create Pay-at-Fawry charge, request signature [official]
#    merchantCode + merchantRefNum + customerProfileId + paymentMethod
#    + amount(2dp) + secureKey      (customerProfileId is "" when absent)
# --------------------------------------------------------------------------
def charge_request_plain(
    *,
    merchant_code: str,
    merchant_ref_num: str,
    customer_profile_id: str | None,
    payment_method: str,
    amount: AmountLike,
    secure_key: str,
) -> str:
    return (
        merchant_code
        + merchant_ref_num
        + _opt(customer_profile_id)
        + payment_method
        + format_amount(amount)
        + secure_key
    )


def charge_request_signature(**fields) -> str:
    """Signature for ``POST /ECommerceWeb/Fawry/payments/charge``. See ``charge_request_plain``."""
    return sha256_hex(charge_request_plain(**fields))


# --------------------------------------------------------------------------
# 1b. Charge response signature [official]
#     referenceNumber (if exists) + merchantRefNum + paymentAmount(2dp)
#     + orderAmount(2dp) + orderStatus + paymentMethod
#     + fawryFees(2dp, if exists) + shippingFees(2dp, if exists)
#     + authNumber (if exists) + customerMail (if exists)
#     + customerMobile (if exists) + secureKey
#
#     The mock also signs status v2 responses with this recipe, passing
#     ``fawryRefNumber`` as ``reference_number``. Fawry doesn't document that
#     use, so it's on the verify list (docs/decisions.md).
# --------------------------------------------------------------------------
def charge_response_plain(
    *,
    reference_number: str | None,
    merchant_ref_num: str,
    payment_amount: AmountLike,
    order_amount: AmountLike,
    order_status: str,
    payment_method: str,
    fawry_fees: AmountLike | None = None,
    shipping_fees: AmountLike | None = None,
    auth_number: str | None = None,
    customer_mail: str | None = None,
    customer_mobile: str | None = None,
    secure_key: str,
) -> str:
    return (
        _opt(reference_number)
        + merchant_ref_num
        + format_amount(payment_amount)
        + format_amount(order_amount)
        + order_status
        + payment_method
        + _opt_amount(fawry_fees)
        + _opt_amount(shipping_fees)
        + _opt(auth_number)
        + _opt(customer_mail)
        + _opt(customer_mobile)
        + secure_key
    )


def charge_response_signature(**fields) -> str:
    """Signature Fawry puts on a charge response. See ``charge_response_plain``."""
    return sha256_hex(charge_response_plain(**fields))


# --------------------------------------------------------------------------
# 2. Get payment status v2 [secondary]
#    merchantCode + merchantRefNumber + secureKey
# --------------------------------------------------------------------------
def status_v2_plain(*, merchant_code: str, merchant_ref_number: str, secure_key: str) -> str:
    return merchant_code + merchant_ref_number + secure_key


def status_v2_signature(**fields) -> str:
    """Signature for ``GET /ECommerceWeb/Fawry/payments/status/v2``."""
    return sha256_hex(status_v2_plain(**fields))


# --------------------------------------------------------------------------
# 3. Refund: endpoint [official], signature [third-party]
#    merchantCode + referenceNumber + refundAmount(2dp) + reason + secureKey
# --------------------------------------------------------------------------
def refund_plain(
    *,
    merchant_code: str,
    reference_number: str,
    refund_amount: AmountLike,
    reason: str | None,
    secure_key: str,
) -> str:
    return (
        merchant_code + reference_number + format_amount(refund_amount) + _opt(reason) + secure_key
    )


def refund_signature(**fields) -> str:
    """Signature for ``POST /ECommerceWeb/Fawry/payments/refund``."""
    return sha256_hex(refund_plain(**fields))


# --------------------------------------------------------------------------
# 4. Cancel unpaid order [third-party]
#    orderRefNo + merchantAccount + lang + secureKey
# --------------------------------------------------------------------------
def cancel_unpaid_plain(
    *, order_ref_no: str, merchant_account: str, lang: str, secure_key: str
) -> str:
    return order_ref_no + merchant_account + lang + secure_key


def cancel_unpaid_signature(**fields) -> str:
    """Signature for ``POST /ECommerceWeb/api/orders/cancel-unpaid-order``."""
    return sha256_hex(cancel_unpaid_plain(**fields))
