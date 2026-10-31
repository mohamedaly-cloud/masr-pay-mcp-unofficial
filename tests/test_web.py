"""The hosted server over real Streamable HTTP: MCP client -> web app (in-process) -> mock Fawry.

Credentials travel in request headers, the way a remote MCP client sends them to Lambda.
"""

from contextlib import asynccontextmanager

import httpx2
import mcp.types as types
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from masr_pay_mcp import web
from tests.conftest import KEY, MERCHANT
from tests.test_server import elicitation

pytestmark = pytest.mark.anyio

SECRET = "s" * 40
CREDS = {"X-Fawry-Merchant-Code": MERCHANT, "X-Fawry-Secure-Key": KEY}


@pytest.fixture
def app(monkeypatch, transport, clock):
    monkeypatch.setenv("MASR_PAY_CONFIRM_SECRET", SECRET)
    monkeypatch.setenv("FAWRY_BASE_URL", "http://mock-fawry")
    # Credentials in the environment must be ignored over HTTP: only headers count.
    monkeypatch.setenv("FAWRY_MERCHANT_CODE", MERCHANT)
    monkeypatch.setenv("FAWRY_SECURE_KEY", KEY)
    return web.create_app(transport=transport, now=clock.now, enable_destructive_tools=True)


@asynccontextmanager
async def connect(app, headers, **client_options):
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as http,
        Client(
            streamable_http_client("http://test/mcp", http_client=http), **client_options
        ) as client,
    ):
        yield client


async def test_paid_order_and_refund_over_http(app, mock_app):
    async with connect(app, CREDS) as client:
        created = await client.call_tool(
            "create_cash_payment",
            {
                "amount_egp": 150,
                "order_id": "42",
                "customer_mobile": "01012345678",
                "customer_email": "ahmed@example.com",
                "description": "Order 42",
            },
        )
        assert not created.is_error, created
        mock_app.state.store.kiosk_pay(created.structured_content["reference_number"])

        status = await client.call_tool("get_payment_status", {"order_id": "42"})
        assert status.structured_content["is_paid"] is True

        # Separate HTTP requests, no session: the token alone carries the confirmation.
        args = {"order_id": "42", "reason": "returned"}
        preview = await client.call_tool("refund_payment", args)
        assert preview.structured_content["executed"] is False
        token = preview.structured_content["confirmation_token"]
        done = await client.call_tool("refund_payment", {**args, "confirmation_token": token})
        assert done.structured_content["executed"] is True, done


async def test_missing_credential_headers_are_named_and_env_is_not_used(app):
    async with connect(app, {}) as client:
        result = await client.call_tool("get_payment_status", {"order_id": "42"})
    text = " ".join(c.text for c in result.content if isinstance(c, types.TextContent))
    assert result.is_error
    assert "x-fawry-merchant-code" in text and "x-fawry-secure-key" in text
    assert KEY not in text


async def test_wrong_secure_key_header_is_refused(app):
    async with connect(app, {**CREDS, "X-Fawry-Secure-Key": "wrong-key"}) as client:
        result = await client.call_tool("get_payment_status", {"order_id": "42"})
    assert result.is_error
    assert "wrong-key" not in str(result.content)


def test_http_server_refuses_to_start_without_a_shared_secret():
    with pytest.raises(RuntimeError, match="MASR_PAY_CONFIRM_SECRET_ARN"):
        web.confirm_secret({})


def test_secret_is_read_from_secrets_manager():
    calls = []

    def fetch(SecretId):  # boto3 names it SecretId
        calls.append(SecretId)
        return {"SecretString": SECRET}

    assert web.confirm_secret({web.SECRET_ARN_ENV: "arn:secret"}, fetch=fetch) == SECRET.encode()
    assert calls == ["arn:secret"]


@pytest.mark.parametrize(
    ("mode", "confirmation"),
    [
        # 2026-07-28: the question rides back in the response (input required), no session needed.
        ("auto", "asked the user directly"),
        # Pre-2026 clients need a mid-call request, which stateless HTTP can't carry: token only.
        ("legacy", "confirmation token"),
    ],
)
async def test_elicitation_over_stateless_http(app, mode, confirmation):
    seen: list[str] = []
    callback = elicitation("accept", seen=seen)
    async with connect(app, CREDS, mode=mode, elicitation_callback=callback) as client:
        created = await client.call_tool(
            "create_cash_payment",
            {
                "amount_egp": 150,
                "order_id": "42",
                "customer_mobile": "01012345678",
                "customer_email": "ahmed@example.com",
                "description": "Order 42",
            },
        )
        assert not created.is_error, created
        preview = await client.call_tool("cancel_unpaid_payment", {"order_id": "42"})
        token = preview.structured_content["confirmation_token"]
        done = await client.call_tool(
            "cancel_unpaid_payment", {"order_id": "42", "confirmation_token": token}
        )
    assert done.structured_content["executed"] is True, done
    assert done.structured_content["confirmation"] == confirmation
    assert len(seen) == (1 if mode == "auto" else 0)


async def test_browser_get_is_answered_at_once_not_held_open(app):
    import anyio

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http,
    ):
        with anyio.fail_after(2):  # on Lambda this used to hang until the 28 s timeout
            response = await http.get("/mcp", headers={"Accept": "text/event-stream"})
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
