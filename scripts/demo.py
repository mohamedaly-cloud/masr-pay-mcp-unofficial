"""Milestone 2 demo, end to end over the real stdio transport.

    python scripts/demo.py

1. Starts the Fawry mock on a free local port.
2. Launches the MCP server as a subprocess over stdio, the same way an MCP host
   (a desktop AI app, MCP Inspector) would, pointed at the mock.
3. "Charge Ahmed 150 EGP for order 42, he'll pay cash" -> create_cash_payment
4. The customer pays cash at a Fawry outlet          -> mock /__admin/kiosk-pay
5. "Has it been paid?"                                -> get_payment_status -> yes

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
        args=["-m", "fawry_mcp"],
        env={
            **os.environ,
            "FAWRY_ENV": "mock",
            "FAWRY_BASE_URL": mock_url,
            "FAWRY_MERCHANT_CODE": MERCHANT,
            "FAWRY_SECURE_KEY": SECURE_KEY,
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


def main() -> None:
    port = free_port()
    mock = start_mock(port)
    try:
        asyncio.run(run(f"http://127.0.0.1:{port}"))
    finally:
        mock.should_exit = True


if __name__ == "__main__":
    main()
