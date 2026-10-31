"""Smoke test the deployed stack over the internet (CI runs it after every deploy).

    python scripts/smoke_remote.py <McpUrl> <MockUrl>

Same story as scripts/demo.py, against Lambda: create a 150 EGP reference -> the mock's
kiosk-pay (which notifies our server, like Fawry) -> "what got paid recently?" -> "paid?" ->
refund preview -> refund with the token. Uses the mock's public demo
merchant (fake money) and a fresh order id per run, since the mock keeps orders in memory.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mock_fawry.app import DEFAULT_MERCHANT_CODE, DEFAULT_SECURE_KEY

HEADERS = {"X-Fawry-Merchant-Code": DEFAULT_MERCHANT_CODE, "X-Fawry-Secure-Key": DEFAULT_SECURE_KEY}


def check(ok: bool, what: str, result) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        raise SystemExit(f"{what}: {result}")


async def run(mcp_url: str, mock_url: str) -> None:
    order_id = f"smoke-{uuid.uuid4().hex[:12]}"
    async with (
        httpx2.AsyncClient(headers=HEADERS, timeout=30) as http,
        Client(streamable_http_client(mcp_url, http_client=http)) as client,
    ):
        tools = {t.name for t in (await client.list_tools()).tools}
        check("refund_payment" in tools, f"tools listed: {sorted(tools)}", tools)

        created = await client.call_tool(
            "create_cash_payment",
            {
                "amount_egp": 150,
                "order_id": order_id,
                "customer_mobile": "01012345678",
                "customer_email": "smoke@example.com",
                "description": "Smoke test",
            },
        )
        check(not created.is_error, "create_cash_payment", created)
        reference = created.structured_content["reference_number"]

        async with httpx2.AsyncClient(base_url=mock_url, timeout=30) as mock:
            paid = await mock.post(f"/__admin/kiosk-pay/{reference}")
        check(paid.status_code == 200, f"mock kiosk-pay {reference}", paid.text)

        recent = await client.call_tool("list_recent_payment_updates", {"hours": 1})
        listed = {u["order_id"]: u for u in recent.structured_content.get("updates", [])}
        check(
            listed.get(order_id, {}).get("is_paid") is True,
            "notification arrived: list_recent_payment_updates shows it paid",
            recent,
        )

        status = await client.call_tool("get_payment_status", {"order_id": order_id})
        check(status.structured_content.get("is_paid") is True, "get_payment_status: paid", status)

        args = {"order_id": order_id, "reason": "smoke test"}
        preview = await client.call_tool("refund_payment", args)
        check(preview.structured_content.get("executed") is False, "refund preview only", preview)
        token = preview.structured_content["confirmation_token"]
        done = await client.call_tool("refund_payment", {**args, "confirmation_token": token})
        check(done.structured_content.get("executed") is True, "refund with token", done)

    # Without credential headers the server must refuse, and say which headers are missing.
    async with Client(mcp_url) as anonymous:
        refused = await anonymous.call_tool("get_payment_status", {"order_id": order_id})
    check(refused.is_error, "no credential headers -> refused", refused)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    asyncio.run(run(sys.argv[1], sys.argv[2].rstrip("/")))
