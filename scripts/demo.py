"""Milestone 2 demo, end to end over the real stdio transport.

    python scripts/demo.py

1. Starts the Fawry mock on a free local port.
2. Launches the MCP server as a subprocess over stdio, the same way an MCP host
   (a desktop AI app, MCP Inspector) would, pointed at the mock.
3. "Charge Ahmed 150 EGP for order 42, he'll pay cash" -> create_cash_payment
4. The customer pays cash at a Fawry outlet          -> mock /__admin/kiosk-pay
5. "Has it been paid?"                                -> get_payment_status -> yes
6. "Refund him"                -> refund_payment previews and returns a confirmation token
7. "Yes"                       -> refund_payment again with the token -> refunded

The server's audit log (one JSON line per tool call) goes to stderr.

Everything is local and fake. No real money, no real credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time

import httpx2
import uvicorn
from mcp import Client, StdioServerParameters

from mock_fawry.app import MockSettings, create_app

MERCHANT = "DEMOMERCHANT01"
SECURE_KEY = "demo-secure-key-not-real"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_mock(port: int) -> uvicorn.Server:
    app = create_app(MockSettings(merchant_code=MERCHANT, secure_key=SECURE_KEY))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock didn't start")
        time.sleep(0.05)
    return server


def show(title: str, data: dict) -> None:
    print(f"\n=== {title}")
    print(json.dumps(data, indent=2, ensure_ascii=False))


async def run(mock_url: str) -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "masr_pay_mcp"],
        env={
            **os.environ,
            "FAWRY_ENV": "mock",
            "FAWRY_BASE_URL": mock_url,
            "FAWRY_MERCHANT_CODE": MERCHANT,
            "FAWRY_SECURE_KEY": SECURE_KEY,
            # The merchant runs this assistant, so refunds are on (they are off by default).
            "MASR_PAY_ENABLE_DESTRUCTIVE_TOOLS": "true",
            "FAWRY_LOG_LEVEL": os.environ.get("FAWRY_LOG_LEVEL", "WARNING"),
        },
    )
    async with Client(server) as mcp:
        tools = [t.name for t in (await mcp.list_tools()).tools]
        print("Tools:", ", ".join(tools))

        print('\nUser: "Charge Ahmed 150 EGP for order 42, he\'ll pay cash."')
        created = await mcp.call_tool(
            "create_cash_payment",
            {
                "amount_egp": 150,
                "order_id": "42",
                "customer_name": "Ahmed",
                "customer_mobile": "01012345678",
                "customer_email": "ahmed@example.com",
                "description": "Order 42",
            },
        )
        if created.is_error:
            raise SystemExit(f"create_cash_payment failed: {created.content}")
        show("create_cash_payment", created.structured_content)
        reference = created.structured_content["reference_number"]

        status = await mcp.call_tool("get_payment_status", {"order_id": "42"})
        show("get_payment_status (before paying)", status.structured_content)

        print(f"\n... Ahmed pays cash at a Fawry outlet with reference {reference} ...")
        async with httpx2.AsyncClient(base_url=mock_url) as http:
            (await http.post(f"/__admin/kiosk-pay/{reference}")).raise_for_status()

        print('\nUser: "Did Ahmed pay?"')
        status = await mcp.call_tool("get_payment_status", {"order_id": "42"})
        show("get_payment_status (after paying)", status.structured_content)
        if not status.structured_content["is_paid"]:
            raise SystemExit("Demo failed: the order should be paid by now.")
        print(f"\nAssistant: {status.structured_content['summary']}")

        refund_args = {"order_id": "42", "reason": "Customer returned the item"}
        print('\nUser: "He returned it, refund him."')
        preview = await mcp.call_tool("refund_payment", refund_args)
        if preview.is_error or preview.structured_content["executed"]:
            raise SystemExit(f"Demo failed: the first refund call must only preview: {preview}")
        show("refund_payment (step 1: preview, nothing changed)", preview.structured_content)
        print(f"\nAssistant: {preview.structured_content['summary']}")

        print('\nUser: "Yes, refund 150 EGP."')
        refund = await mcp.call_tool(
            "refund_payment",
            {**refund_args, "confirmation_token": preview.structured_content["confirmation_token"]},
        )
        if refund.is_error:
            raise SystemExit(f"refund_payment failed: {refund.content}")
        show("refund_payment (step 2: confirmed with the token)", refund.structured_content)
        print(f"\nAssistant: {refund.structured_content['summary']}")


def main() -> None:
    port = free_port()
    mock = start_mock(port)
    try:
        asyncio.run(run(f"http://127.0.0.1:{port}"))
    finally:
        mock.should_exit = True


if __name__ == "__main__":
    main()
