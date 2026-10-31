"""MCP tools end to end: in-memory MCP client -> our server -> mock Fawry (in-process)."""

import mcp.types as types
import pytest
from mcp import Client

from masr_pay_mcp.providers.fawry.config import ConfigError
from masr_pay_mcp.server import create_server
from tests.conftest import HOUR_MS, MERCHANT

pytestmark = pytest.mark.anyio

CREATE_ARGS = {
    "amount_egp": 150,
    "order_id": "42",
    "customer_mobile": "+20 101 234 5678",
    "customer_email": "ahmed@example.com",
    "description": "Order 42",
    "customer_name": "Ahmed",
}


@pytest.fixture
def server(config, transport, clock):
    return create_server(config_loader=lambda: config, transport=transport, now=clock.now)


def store(mock_app):
    return mock_app.state.store


async def call(client, tool, args):
    result = await client.call_tool(tool, args)
    return result


def error_text(result) -> str:
    assert result.is_error, result
    return " ".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def create(client, **overrides):
    result = await call(client, "create_cash_payment", {**CREATE_ARGS, **overrides})
    assert not result.is_error, error_text(result)
    return result.structured_content


# ------------------------------------------------------------------ discovery
async def test_lists_four_tools_with_safety_annotations(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "create_cash_payment",
        "get_payment_status",
        "cancel_unpaid_payment",
        "refund_payment",
    }
    assert tools["get_payment_status"].annotations.read_only_hint is True
    for name in ("cancel_unpaid_payment", "refund_payment"):
        assert tools[name].annotations.destructive_hint is True
        assert "REQUIRES HUMAN CONFIRMATION" in tools[name].description
        assert "confirmed_by_user" in tools[name].input_schema["properties"]
        assert "ctx" not in tools[name].input_schema["properties"]
    assert "EGP" in tools["create_cash_payment"].description


# ------------------------------------------------------------------ demo flow
async def test_demo_flow_create_pay_check(server, mock_app, clock):
    """Milestone 2 demo: 150 EGP for order 42 -> customer pays at an outlet -> 'has it been paid?' -> yes."""
    async with Client(server) as client:
        created = await create(client)
        assert created["status"] == "awaiting_payment"
        assert created["amount_egp"] == "150.00"
        assert created["fawry_fees_egp"] == "5.00"
        assert created["total_to_pay_egp"] == "155.00"
        assert created["environment"] == "mock"
        ref = created["reference_number"]
        assert ref in created["customer_instructions"]
        assert "155.00 EGP" in created["customer_instructions"]

        before = (await call(client, "get_payment_status", {"order_id": "42"})).structured_content
        assert before["is_paid"] is False
        assert "not paid yet" in before["summary"]

        clock.advance(2 * HOUR_MS)
        store(mock_app).kiosk_pay(ref)

        after = (await call(client, "get_payment_status", {"order_id": "42"})).structured_content
        assert after["is_paid"] is True
        assert after["status"] == "paid"
        assert after["paid_at_utc"] is not None
        assert "is paid" in after["summary"]


async def test_mobile_is_normalized_and_expiry_sent(server, mock_app, clock):
    async with Client(server) as client:
        created = await create(client, expiry_hours=24)
    order = store(mock_app).get_by_merchant_ref(MERCHANT, "42")
    assert order.customer_mobile == "01012345678"
    assert order.payment_expiry_ms == clock.now_ms + 24 * HOUR_MS
    assert created["expires_at_utc"].endswith("+00:00")


# ------------------------------------------------------------------ input validation
@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_egp": 0},
        {"amount_egp": 10.005},
        {"customer_mobile": "0201234567"},
        {"customer_email": "nope"},
        {"order_id": "order 42"},
        {"expiry_hours": 0},
        {"language": "fr-fr"},
    ],
)
async def test_invalid_input_is_a_tool_error(server, mock_app, overrides):
    async with Client(server) as client:
        result = await call(client, "create_cash_payment", {**CREATE_ARGS, **overrides})
    assert result.is_error
    assert store(mock_app).all() == []


# ------------------------------------------------------------------ failure meanings
async def test_duplicate_order_id(server):
    async with Client(server) as client:
        await create(client)
        result = await call(client, "create_cash_payment", CREATE_ARGS)
    assert "already exists" in error_text(result)


async def test_status_of_unknown_order(server):
    async with Client(server) as client:
        result = await call(client, "get_payment_status", {"order_id": "nope"})
    assert "No Fawry payment exists for order nope" in error_text(result)


async def test_bad_credentials_explain_what_to_fix(config, transport, clock):
    from masr_pay_mcp.providers.fawry.config import Credentials, FawryConfig

    bad = FawryConfig(config.base_url, "mock", Credentials(MERCHANT, "wrong-key"))
    server = create_server(config_loader=lambda: bad, transport=transport, now=clock.now)
    async with Client(server) as client:
        result = await call(client, "create_cash_payment", CREATE_ARGS)
    text = error_text(result)
    assert "9946" in text and "FAWRY_SECURE_KEY" in text
    assert "wrong-key" not in text


async def test_missing_config_is_reported(transport):
    def broken():
        raise ConfigError("Missing FAWRY_SECURE_KEY.")

    server = create_server(config_loader=broken, transport=transport)
    async with Client(server) as client:
        result = await call(client, "get_payment_status", {"order_id": "42"})
    assert "isn't configured" in error_text(result)


# ------------------------------------------------------------------ cancel
async def test_cancel_requires_confirmation_flag(server, mock_app):
    async with Client(server) as client:
        await create(client)
        result = await call(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert "needs explicit confirmation" in error_text(result)
    assert store(mock_app).get_by_merchant_ref(MERCHANT, "42").status == "UNPAID"


async def test_cancel_unpaid(server, mock_app):
    async with Client(server) as client:
        await create(client)
        result = await call(
            client, "cancel_unpaid_payment", {"order_id": "42", "confirmed_by_user": True}
        )
    assert not result.is_error, error_text(result)
    assert result.structured_content["status"] == "canceled"
    assert store(mock_app).get_by_merchant_ref(MERCHANT, "42").status == "CANCELED"


async def test_cancel_paid_order_points_to_refund(server, mock_app):
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        result = await call(
            client, "cancel_unpaid_payment", {"order_id": "42", "confirmed_by_user": True}
        )
    assert "refund_payment" in error_text(result)
    assert store(mock_app).get_by_merchant_ref(MERCHANT, "42").status == "PAID"


# ------------------------------------------------------------------ refund
async def test_refund_requires_confirmation_flag(server, mock_app):
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        result = await call(client, "refund_payment", {"order_id": "42", "reason": "Returned"})
    assert "needs explicit confirmation" in error_text(result)
    assert store(mock_app).get_by_merchant_ref(MERCHANT, "42").status == "PAID"


async def test_full_refund_defaults_to_order_amount(server, mock_app):
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        result = await call(
            client,
            "refund_payment",
            {"order_id": "42", "reason": "Returned", "confirmed_by_user": True},
        )
    assert not result.is_error, error_text(result)
    assert result.structured_content["refunded_amount_egp"] == "150.00"  # fees not refunded
    assert result.structured_content["status"] == "refunded"


async def test_partial_refund_with_fraction_warns_about_rounding(server, mock_app):
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        result = await call(
            client,
            "refund_payment",
            {"order_id": "42", "reason": "Partial", "amount_egp": 20.5, "confirmed_by_user": True},
        )
    assert result.structured_content["refunded_amount_egp"] == "20.50"
    assert "round it down" in result.structured_content["summary"]


async def test_refund_more_than_order_amount_is_refused_before_calling_fawry(server, mock_app):
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        result = await call(
            client,
            "refund_payment",
            {"order_id": "42", "reason": "x", "amount_egp": 155, "confirmed_by_user": True},
        )
    assert "the order amount is 150.00 EGP" in error_text(result)
    assert store(mock_app).get_by_merchant_ref(MERCHANT, "42").status == "PAID"


async def test_refund_unpaid_points_to_cancel(server):
    async with Client(server) as client:
        await create(client)
        result = await call(
            client, "refund_payment", {"order_id": "42", "reason": "x", "confirmed_by_user": True}
        )
    assert "cancel_unpaid_payment" in error_text(result)


async def test_second_refund_is_refused(server, mock_app):
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        args = {"order_id": "42", "reason": "x", "amount_egp": 10, "confirmed_by_user": True}
        assert not (await call(client, "refund_payment", args)).is_error
        result = await call(client, "refund_payment", args)
    assert "one refund per payment" in error_text(result)


# ------------------------------------------------------------------ elicitation
def elicitation(action: str, confirm: bool = True, seen: list | None = None):
    async def callback(context, params):
        if seen is not None:
            seen.append(params.message)
        content = {"confirm": confirm} if action == "accept" else None
        return types.ElicitResult(action=action, content=content)

    return callback


async def test_refund_asks_the_human_when_client_supports_elicitation(server, mock_app):
    seen: list[str] = []
    async with Client(server, elicitation_callback=elicitation("accept", seen=seen)) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        result = await call(
            client,
            "refund_payment",
            {"order_id": "42", "reason": "Returned", "confirmed_by_user": True},
        )
    assert not result.is_error, error_text(result)
    assert len(seen) == 1
    assert "150.00 EGP" in seen[0] and ref in seen[0]


@pytest.mark.parametrize(
    ("action", "confirm"), [("decline", True), ("cancel", True), ("accept", False)]
)
async def test_human_saying_no_stops_the_refund(server, mock_app, action, confirm):
    async with Client(server, elicitation_callback=elicitation(action, confirm)) as client:
        ref = (await create(client))["reference_number"]
        store(mock_app).kiosk_pay(ref)
        result = await call(
            client,
            "refund_payment",
            {"order_id": "42", "reason": "Returned", "confirmed_by_user": True},
        )
    assert "did not confirm" in error_text(result)
    assert store(mock_app).get_by_merchant_ref(MERCHANT, "42").status == "PAID"


async def test_human_saying_no_stops_the_cancel(server, mock_app):
    async with Client(server, elicitation_callback=elicitation("decline")) as client:
        await create(client)
        result = await call(
            client, "cancel_unpaid_payment", {"order_id": "42", "confirmed_by_user": True}
        )
    assert "did not confirm" in error_text(result)
    assert store(mock_app).get_by_merchant_ref(MERCHANT, "42").status == "UNPAID"


@pytest.mark.parametrize(
    "mode", ["auto", "legacy"]
)  # 2026-07-28 input-required round vs mid-call request
async def test_elicitation_works_on_both_protocol_generations(server, mock_app, mode):
    seen: list[str] = []
    async with Client(
        server, mode=mode, elicitation_callback=elicitation("accept", seen=seen)
    ) as client:
        await create(client)
        result = await call(
            client, "cancel_unpaid_payment", {"order_id": "42", "confirmed_by_user": True}
        )
    assert not result.is_error, error_text(result)
    assert len(seen) == 1
    assert result.structured_content["confirmation"] == "asked the user directly"


async def test_without_elicitation_the_flag_is_recorded_as_the_confirmation(server):
    async with Client(server) as client:
        await create(client)
        result = await call(
            client, "cancel_unpaid_payment", {"order_id": "42", "confirmed_by_user": True}
        )
    assert result.structured_content["confirmation"] == "confirmed_by_user flag"


async def test_human_is_not_asked_about_an_action_that_would_fail(server):
    seen: list[str] = []
    async with Client(server, elicitation_callback=elicitation("accept", seen=seen)) as client:
        await create(client)  # unpaid: refund must fail the precheck
        result = await call(
            client, "refund_payment", {"order_id": "42", "reason": "x", "confirmed_by_user": True}
        )
        no_flag = await call(client, "refund_payment", {"order_id": "42", "reason": "x"})
    assert "cancel_unpaid_payment" in error_text(result)
    assert "needs explicit confirmation" in error_text(no_flag)
    assert seen == []
