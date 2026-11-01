"""Unit tests for core/confirm.py and core/guardrails.py (the MCP-level flows are in test_server.py)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from masr_pay_mcp.core import confirm, guardrails

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
SECRET = b"s" * 32
ARGS = {"order_id": "42", "amount": Decimal("150.00"), "reason": "استرجاع"}


def test_token_round_trip_and_expiry_time():
    token, expires = confirm.issue(SECRET, "refund_payment", ARGS, NOW)
    assert expires == NOW + confirm.TTL
    confirm.verify(SECRET, token, "refund_payment", dict(ARGS), NOW + timedelta(minutes=4))


@pytest.mark.parametrize(
    ("tool", "args", "secret"),
    [
        ("cancel_unpaid_payment", ARGS, SECRET),  # other tool
        ("refund_payment", {**ARGS, "amount": Decimal("150.01")}, SECRET),  # other amount
        ("refund_payment", ARGS, b"t" * 32),  # other server
    ],
)
def test_token_is_bound_to_tool_arguments_and_secret(tool, args, secret):
    token, _ = confirm.issue(SECRET, "refund_payment", ARGS, NOW)
    with pytest.raises(confirm.ConfirmationError, match="doesn't match"):
        confirm.verify(secret, token, tool, args, NOW)


def test_expired_token():
    token, _ = confirm.issue(SECRET, "refund_payment", ARGS, NOW)
    with pytest.raises(confirm.ConfirmationError, match="expired"):
        confirm.verify(SECRET, token, "refund_payment", ARGS, NOW + timedelta(minutes=5, seconds=1))


def test_editing_the_expiry_breaks_the_mac():
    token, _ = confirm.issue(SECRET, "refund_payment", ARGS, NOW)
    exp, mac = token.split(".")
    with pytest.raises(confirm.ConfirmationError, match="doesn't match"):
        confirm.verify(SECRET, f"{int(exp) + 3600}.{mac}", "refund_payment", ARGS, NOW)


def test_secret_from_env():
    assert confirm.load_secret({confirm.SECRET_ENV: "x" * 32}) == b"x" * 32
    with pytest.raises(ValueError, match="at least 32"):
        confirm.load_secret({confirm.SECRET_ENV: "short"})
    a, b = confirm.load_secret({}), confirm.load_secret({})
    assert len(a) == 32 and a != b  # random per process when unset


def test_payment_limit_from_env():
    assert guardrails.max_payment_from_env({}) == Decimal("50000")
    assert guardrails.max_payment_from_env({guardrails.MAX_PAYMENT_ENV: "1000"}) == 1000
    for bad in ("abc", "0", "-5", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            guardrails.max_payment_from_env({guardrails.MAX_PAYMENT_ENV: bad})


def test_amount_checks():
    guardrails.check_payment_amount(Decimal("50000"), Decimal("50000"))  # at the limit is fine
    with pytest.raises(guardrails.GuardrailError):
        guardrails.check_payment_amount(Decimal("50000.01"), Decimal("50000"))
    guardrails.check_refund_amount(Decimal("150"), Decimal("150"))
    with pytest.raises(guardrails.GuardrailError, match=r"only 150\.00 EGP"):
        guardrails.check_refund_amount(Decimal("150.01"), Decimal("150"))
    with pytest.raises(guardrails.GuardrailError, match=r"only 0\.00 EGP"):
        guardrails.check_refund_amount(Decimal("0"), Decimal("0"))  # fully refunded already


def test_refund_limit_and_destructive_switch_from_env():
    assert guardrails.max_refund_from_env({}) == Decimal("2000")
    assert guardrails.max_refund_from_env({guardrails.MAX_REFUND_ENV: "500"}) == 500
    assert guardrails.destructive_tools_enabled({}) is False
    for yes in ("true", "1", "YES", " on "):
        assert guardrails.destructive_tools_enabled({guardrails.ENABLE_DESTRUCTIVE_ENV: yes})
    for no in ("false", "0", "no", "off", ""):
        assert not guardrails.destructive_tools_enabled({guardrails.ENABLE_DESTRUCTIVE_ENV: no})
    with pytest.raises(ValueError, match="true or false"):
        guardrails.destructive_tools_enabled({guardrails.ENABLE_DESTRUCTIVE_ENV: "ture"})
    with pytest.raises(guardrails.GuardrailError, match="per refund"):
        guardrails.check_refund_limit(Decimal("2000.01"), Decimal("2000"))
