"""MCP tools end to end: in-memory MCP client -> our server -> mock Fawry (in-process)."""

import json
import logging
from decimal import Decimal

import httpx2
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
        assert "confirmation_token" in tools[name].input_schema["properties"]
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
class _CountCharges(httpx2.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner, self.charges = inner, 0

    async def handle_async_request(self, request):
        self.charges += request.url.path.endswith("/payments/charge")
        return await self.inner.handle_async_request(request)


async def test_duplicate_order_id_is_refused_before_charging(config, transport, clock):
    counting = _CountCharges(transport)
    server = create_server(config_loader=lambda: config, transport=counting, now=clock.now)
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        result = await call(client, "create_cash_payment", {**CREATE_ARGS, "amount_egp": 999})
    text = error_text(result)
    assert "already has a Fawry payment" in text and ref in text
    assert counting.charges == 1  # our pre-check stopped it; Fawry never saw a second charge


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


# ------------------------------------------------------------------ two-step confirmation
async def two_step(client, tool, args):
    """Preview, then confirm with the token: what a model does after the human says yes."""
    preview = await call(client, tool, args)
    if preview.is_error:
        return preview
    assert preview.structured_content["executed"] is False
    token = preview.structured_content["confirmation_token"]
    return await call(client, tool, {**args, "confirmation_token": token})


async def paid_order(client, mock_app):
    ref = (await create(client))["reference_number"]
    store(mock_app).kiosk_pay(ref)
    return ref


def status_of(mock_app, order_id="42"):
    return store(mock_app).get_by_merchant_ref(MERCHANT, order_id).status


# ------------------------------------------------------------------ cancel
async def test_cancel_first_call_is_only_a_preview(server, mock_app):
    async with Client(server) as client:
        ref = (await create(client))["reference_number"]
        result = await call(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert not result.is_error, error_text(result)
    preview = result.structured_content
    assert preview["executed"] is False
    assert preview["confirmation_token"]
    assert preview["confirmation_expires_at_utc"].endswith("+00:00")
    assert preview["reference_number"] == ref and preview["amount_egp"] == "155.00"
    assert "nothing changed" in preview["summary"]
    assert status_of(mock_app) == "UNPAID"


async def test_cancel_unpaid(server, mock_app):
    async with Client(server) as client:
        await create(client)
        result = await two_step(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert not result.is_error, error_text(result)
    assert result.structured_content["executed"] is True
    assert result.structured_content["status"] == "canceled"
    assert result.structured_content["confirmation_token"] is None
    assert status_of(mock_app) == "CANCELED"


async def test_cancel_paid_order_points_to_refund(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        result = await call(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert "refund_payment" in error_text(result)
    assert status_of(mock_app) == "PAID"


# ------------------------------------------------------------------ refund
async def test_refund_first_call_is_only_a_preview(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        result = await call(client, "refund_payment", {"order_id": "42", "reason": "Returned"})
    preview = result.structured_content
    assert preview["executed"] is False and preview["confirmation_token"]
    assert preview["refund_amount_egp"] == "150.00"
    assert "one refund per payment" in preview["summary"]
    assert status_of(mock_app) == "PAID"


async def test_full_refund_defaults_to_order_amount(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        result = await two_step(client, "refund_payment", {"order_id": "42", "reason": "Returned"})
    assert not result.is_error, error_text(result)
    assert result.structured_content["refund_amount_egp"] == "150.00"  # fees not refunded
    assert result.structured_content["status"] == "refunded"
    assert result.structured_content["executed"] is True


async def test_partial_refund_with_fraction_warns_about_rounding(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        result = await two_step(
            client, "refund_payment", {"order_id": "42", "reason": "Partial", "amount_egp": 20.5}
        )
    assert result.structured_content["refund_amount_egp"] == "20.50"
    assert "round it down" in result.structured_content["summary"]


async def test_refund_more_than_order_amount_is_refused_before_calling_fawry(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        result = await call(
            client, "refund_payment", {"order_id": "42", "reason": "x", "amount_egp": 155}
        )
    assert "the order amount is 150.00 EGP" in error_text(result)
    assert status_of(mock_app) == "PAID"


async def test_refund_unpaid_points_to_cancel(server):
    async with Client(server) as client:
        await create(client)
        result = await call(client, "refund_payment", {"order_id": "42", "reason": "x"})
    assert "cancel_unpaid_payment" in error_text(result)


async def test_second_refund_is_refused(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        args = {"order_id": "42", "reason": "x", "amount_egp": 10}
        assert not (await two_step(client, "refund_payment", args)).is_error
        result = await call(client, "refund_payment", args)
    assert "one refund per payment" in error_text(result)


# ------------------------------------------------------------------ token attacks
async def test_token_for_a_smaller_refund_cannot_authorise_a_bigger_one(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        small = await call(
            client, "refund_payment", {"order_id": "42", "reason": "x", "amount_egp": 10}
        )
        token = small.structured_content["confirmation_token"]
        result = await call(
            client,
            "refund_payment",
            {"order_id": "42", "reason": "x", "amount_egp": 150, "confirmation_token": token},
        )
    assert "doesn't match this exact action" in error_text(result)
    assert status_of(mock_app) == "PAID"


async def test_token_cannot_be_reused_with_another_reason(server, mock_app):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        preview = await call(client, "refund_payment", {"order_id": "42", "reason": "Returned"})
        token = preview.structured_content["confirmation_token"]
        result = await call(
            client,
            "refund_payment",
            {"order_id": "42", "reason": "Customer asked", "confirmation_token": token},
        )
    assert "doesn't match" in error_text(result)
    assert status_of(mock_app) == "PAID"


async def test_cancel_token_cannot_be_used_for_a_refund(server, mock_app):
    async with Client(server) as client:
        await create(client)
        cancel_preview = await call(client, "cancel_unpaid_payment", {"order_id": "42"})
        token = cancel_preview.structured_content["confirmation_token"]
        store(mock_app).kiosk_pay(cancel_preview.structured_content["reference_number"])
        result = await call(
            client, "refund_payment", {"order_id": "42", "reason": "x", "confirmation_token": token}
        )
    assert "doesn't match" in error_text(result)
    assert status_of(mock_app) == "PAID"


@pytest.mark.parametrize("token", ["", "garbage", "9999999999.abc", "1.2.3"])
async def test_forged_tokens_are_refused(server, mock_app, token):
    async with Client(server) as client:
        await paid_order(client, mock_app)
        result = await call(
            client, "refund_payment", {"order_id": "42", "reason": "x", "confirmation_token": token}
        )
    assert result.is_error
    assert status_of(mock_app) == "PAID"


async def test_expired_token_is_refused(server, mock_app, clock):
    async with Client(server) as client:
        await create(client)
        preview = await call(client, "cancel_unpaid_payment", {"order_id": "42"})
        clock.advance(6 * 60_000)
        result = await call(
            client,
            "cancel_unpaid_payment",
            {
                "order_id": "42",
                "confirmation_token": preview.structured_content["confirmation_token"],
            },
        )
    assert "expired" in error_text(result)
    assert status_of(mock_app) == "UNPAID"


async def test_token_from_another_server_secret_is_refused(config, transport, clock, mock_app):
    def make(secret):
        return create_server(
            config_loader=lambda: config, transport=transport, now=clock.now, confirm_secret=secret
        )

    async with Client(make(b"a" * 32)) as a, Client(make(b"b" * 32)) as b:
        await create(a)
        token = (await call(a, "cancel_unpaid_payment", {"order_id": "42"})).structured_content[
            "confirmation_token"
        ]
        result = await call(
            b, "cancel_unpaid_payment", {"order_id": "42", "confirmation_token": token}
        )
    assert "doesn't match" in error_text(result)
    assert status_of(mock_app) == "UNPAID"


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
        ref = await paid_order(client, mock_app)
        result = await two_step(client, "refund_payment", {"order_id": "42", "reason": "Returned"})
    assert not result.is_error, error_text(result)
    assert len(seen) == 1  # asked once: on the confirming call, not on the preview
    assert "150.00 EGP" in seen[0] and ref in seen[0]


@pytest.mark.parametrize(
    ("action", "confirm"), [("decline", True), ("cancel", True), ("accept", False)]
)
async def test_human_saying_no_stops_the_refund(server, mock_app, action, confirm):
    async with Client(server, elicitation_callback=elicitation(action, confirm)) as client:
        await paid_order(client, mock_app)
        result = await two_step(client, "refund_payment", {"order_id": "42", "reason": "Returned"})
    assert "did not confirm" in error_text(result)
    assert status_of(mock_app) == "PAID"


async def test_human_saying_no_stops_the_cancel(server, mock_app):
    async with Client(server, elicitation_callback=elicitation("decline")) as client:
        await create(client)
        result = await two_step(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert "did not confirm" in error_text(result)
    assert status_of(mock_app) == "UNPAID"


@pytest.mark.parametrize(
    "mode", ["auto", "legacy"]
)  # 2026-07-28 input-required round vs mid-call request
async def test_elicitation_works_on_both_protocol_generations(server, mock_app, mode):
    seen: list[str] = []
    async with Client(
        server, mode=mode, elicitation_callback=elicitation("accept", seen=seen)
    ) as client:
        await create(client)
        result = await two_step(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert not result.is_error, error_text(result)
    assert len(seen) == 1
    assert result.structured_content["confirmation"] == "asked the user directly"


async def test_without_elicitation_the_token_is_recorded_as_the_confirmation(server):
    async with Client(server) as client:
        await create(client)
        result = await two_step(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert result.structured_content["confirmation"] == "confirmation token"


async def test_human_is_not_asked_about_an_action_that_would_fail_or_a_preview(server, mock_app):
    seen: list[str] = []
    async with Client(server, elicitation_callback=elicitation("accept", seen=seen)) as client:
        await create(client)  # unpaid: refund must fail the precheck
        failing = await call(client, "refund_payment", {"order_id": "42", "reason": "x"})
        preview = await call(client, "cancel_unpaid_payment", {"order_id": "42"})
    assert "cancel_unpaid_payment" in error_text(failing)
    assert preview.structured_content["executed"] is False
    assert seen == []


# ------------------------------------------------------------------ guardrails
async def test_payment_above_the_limit_is_refused_before_calling_fawry(server, mock_app):
    async with Client(server) as client:
        result = await call(client, "create_cash_payment", {**CREATE_ARGS, "amount_egp": 50_000.01})
    assert "limit of 50000.00 EGP" in error_text(result)
    assert store(mock_app).all() == []


async def test_payment_limit_is_configurable(config, transport, clock, mock_app):
    server = create_server(
        config_loader=lambda: config,
        transport=transport,
        now=clock.now,
        max_payment_egp=Decimal("100"),
    )
    async with Client(server) as client:
        result = await call(client, "create_cash_payment", CREATE_ARGS)  # 150 EGP
    assert "limit of 100.00 EGP" in error_text(result)
    assert store(mock_app).all() == []


# ------------------------------------------------------------------ audit log
def audit_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "masr_pay_mcp.audit"]


async def test_every_tool_call_writes_one_redacted_audit_line(server, mock_app, caplog):
    caplog.set_level(logging.INFO, logger="masr_pay_mcp.audit")
    async with Client(server) as client:
        await paid_order(client, mock_app)
        await call(client, "get_payment_status", {"order_id": "42"})
        await two_step(client, "refund_payment", {"order_id": "42", "reason": "Returned"})
        await call(client, "cancel_unpaid_payment", {"order_id": "42"})  # refused: refunded
    lines = audit_lines(caplog)
    assert [(line["tool"], line["outcome"]) for line in lines] == [
        ("create_cash_payment", "ok"),
        ("get_payment_status", "ok"),
        ("refund_payment", "preview"),
        ("refund_payment", "ok"),
        ("cancel_unpaid_payment", "error"),
    ]
    assert lines[0]["amount_egp"] == "150.00" and lines[0]["merchant_ref"] == "42"
    assert lines[3]["raw_status"] == "REFUNDED"
    assert all(line["request_id"] and line["ts"] for line in lines)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("ahmed@example.com", "01012345678", "fake-test-key"):
        assert secret not in text


async def test_audit_records_fawry_error_code(config, transport, clock, caplog):
    from masr_pay_mcp.providers.fawry.config import Credentials, FawryConfig

    caplog.set_level(logging.INFO, logger="masr_pay_mcp.audit")
    bad = FawryConfig(config.base_url, "mock", Credentials(MERCHANT, "wrong-key"))
    server = create_server(config_loader=lambda: bad, transport=transport, now=clock.now)
    async with Client(server) as client:
        await call(client, "create_cash_payment", CREATE_ARGS)
    (line,) = audit_lines(caplog)
    assert line["outcome"] == "error" and line["provider_code"] == 9946
    assert "wrong-key" not in json.dumps(line)


# ------------------------------------------------------------------ spoofed responses (threat T4)
class _ForgePaid(httpx2.AsyncBaseTransport):
    """Rewrites status responses to say PAID without re-signing them, like a man-in-the-middle."""

    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if "/status/v2" not in request.url.path:
            return response
        body = json.loads(await response.aread())
        body["orderStatus"] = "PAID"
        return httpx2.Response(response.status_code, json=body)


async def test_spoofed_paid_status_is_never_reported_as_paid(config, transport, clock):
    honest = create_server(config_loader=lambda: config, transport=transport, now=clock.now)
    forged = create_server(
        config_loader=lambda: config, transport=_ForgePaid(transport), now=clock.now
    )
    async with Client(honest) as a, Client(forged) as b:
        await create(a)
        result = await call(b, "get_payment_status", {"order_id": "42"})
    assert "can't be trusted" in error_text(result)
