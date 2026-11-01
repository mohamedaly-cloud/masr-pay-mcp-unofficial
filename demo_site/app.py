"""The public "try it" page: a visitor chats with Claude, and Claude uses the MCP tools.

    GET  /           the page (demo_site/index.html)
    POST /api/chat   {"session", "text", "history"} -> {"reply", "steps", "history"}
    POST /api/pay    {"reference"} -> the mock's kiosk-pay ("the customer paid at a Fawry outlet")

Claude reaches the tools the way any MCP client would: over Streamable HTTP to the hosted MCP
server, with the mock's public demo merchant. Fake money only. Not part of the PyPI package.

Spending is capped three ways: a daily message limit kept in DynamoDB, small per-reply limits
(max_tokens, tool rounds, message and history size), and the Anthropic workspace's own spend
limit. Each visitor's order ids get a session prefix, so visitors can't see each other's orders.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

log = logging.getLogger("demo_site")

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 1024
MAX_TOOL_ROUNDS = 5
MAX_TEXT_CHARS = 500
MAX_HISTORY_MESSAGES = 40
MAX_HISTORY_BYTES = 60_000
# The mock's public demo merchant (mock_fawry.app defaults; fake, published in the README).
DEMO_HEADERS = {
    "X-Fawry-Merchant-Code": "MOCKMERCHANT0001",
    "X-Fawry-Secure-Key": "mock-secure-key-not-a-real-fawry-key",
}
SESSION_RE = re.compile(r"^[a-z0-9]{8}$")
REFERENCE_RE = re.compile(r"^\d{9}$")

SYSTEM = """\
You are the cashier assistant of "Ahmed's Shop", a pretend shop in Egypt, on the public demo page \
of masr-pay-mcp-unofficial: an unofficial MCP server for FawryPay "Pay at Fawry" cash payments, \
not affiliated with Fawry. The visitor plays the shop owner. Everything here is a test: a fake \
Fawry, fake money, a fake merchant.

Scope: only help with this demo, meaning creating Fawry cash payments, checking them, cancelling \
or refunding them, and short questions about how Pay at Fawry or this MCP server works. For \
anything else, answer in one sentence that this demo only handles Fawry payments, and suggest \
something to try, such as "Charge Ahmed 150 EGP for order 42, he'll pay cash."

Style: short and plain, at most three sentences unless you are listing payment details. Answer \
in the visitor's language (Arabic or English). If a payment needs details the visitor didn't \
give (mobile, email), use clearly fake ones such as 01000000000 and customer@example.com and say \
so, instead of asking.

Refunds and cancellations: the first call only returns a preview. Show it and ask the visitor to \
confirm. Call again with the confirmation token only after they clearly say yes.

Order ids are prefixed with this visitor's session code automatically. That's expected; you can \
call orders by their short number."""


class BadRequest(ValueError):
    pass


# ----------------------------------------------------------------- input checks
def _parse_chat(body: Any) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(body, dict):
        raise BadRequest("Expected a JSON object.")
    session, text, history = body.get("session"), body.get("text"), body.get("history", [])
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise BadRequest("Bad session id.")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("Type a message first.")
    if len(text) > MAX_TEXT_CHARS:
        raise BadRequest(f"Keep messages under {MAX_TEXT_CHARS} characters.")
    if not isinstance(history, list) or not all(
        isinstance(m, dict) and m.get("role") in ("user", "assistant") for m in history
    ):
        raise BadRequest("Bad chat history.")
    if len(history) > MAX_HISTORY_MESSAGES or len(json.dumps(history)) > MAX_HISTORY_BYTES:
        raise BadRequest("This chat is long enough. Start a new chat to keep going.")
    return session, text.strip(), history


def scoped_args(args: dict[str, Any], session: str) -> dict[str, Any]:
    """Prefix order ids with the visitor's session, so nobody touches another visitor's orders."""
    order_id = args.get("order_id")
    if isinstance(order_id, str) and not order_id.startswith(f"{session}-"):
        return {**args, "order_id": f"{session}-{order_id}"}
    return args


def _tool_text(result: Any) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    return " ".join(getattr(c, "text", "") for c in result.content)


# ----------------------------------------------------------------- daily cap
def dynamodb_daily_slot(table: str, limit: int) -> Callable[[], bool]:
    """One message = one slot; False once today's limit is used up (resets at midnight UTC)."""
    import boto3  # in the Lambda Python runtime

    db = boto3.client("dynamodb")

    def take() -> bool:
        now = datetime.now(UTC)
        try:
            db.update_item(
                TableName=table,
                Key={"day": {"S": now.date().isoformat()}},
                UpdateExpression="ADD messages :one SET expires = :expires",
                ConditionExpression="attribute_not_exists(messages) OR messages < :max",
                ExpressionAttributeValues={
                    ":one": {"N": "1"},
                    ":max": {"N": str(limit)},
                    ":expires": {"N": str(int((now + timedelta(days=3)).timestamp()))},
                },
            )
        except db.exceptions.ConditionalCheckFailedException:
            return False
        return True

    return take


# ----------------------------------------------------------------- app
def create_app(
    *,
    claude: Callable[[], anthropic.AsyncAnthropic],
    connect_mcp: Callable[[], AbstractAsyncContextManager[Client]],
    mock_http: Callable[[], httpx2.AsyncClient],
    take_slot: Callable[[], bool],
) -> Starlette:
    page = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(page)

    async def chat(request: Request) -> JSONResponse:
        try:
            session, text, history = _parse_chat(await request.json())
        except (BadRequest, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not await anyio.to_thread.run_sync(take_slot):
            return JSONResponse(
                {"error": "The demo hit today's limit. Try again tomorrow, or watch the GIF."},
                status_code=429,
            )
        messages = [*history, {"role": "user", "content": text}]
        steps: list[dict[str, Any]] = []
        try:
            async with connect_mcp() as mcp:
                tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.input_schema,
                    }
                    for t in (await mcp.list_tools()).tools
                ]
                for _ in range(MAX_TOOL_ROUNDS):
                    response = await claude().messages.create(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        output_config={"effort": "low"},
                        system=SYSTEM,
                        tools=tools,
                        messages=messages,
                    )
                    log.info(
                        "chat session=%s stop=%s in=%s out=%s",
                        session,
                        response.stop_reason,
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": [
                                b.model_dump(mode="json", exclude_unset=True)
                                for b in response.content
                            ],
                        }
                    )
                    if response.stop_reason != "tool_use":
                        break
                    results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        args = scoped_args(dict(block.input), session)
                        result = await mcp.call_tool(block.name, args)
                        steps.append(
                            {
                                "tool": block.name,
                                "args": args,
                                "ok": not result.is_error,
                                "result": result.structured_content or _tool_text(result),
                            }
                        )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": _tool_text(result),
                                "is_error": bool(result.is_error),
                            }
                        )
                    messages.append({"role": "user", "content": results})
        except anthropic.BadRequestError:
            return JSONResponse(
                {"error": "This chat can't continue. Start a new chat."}, status_code=400
            )
        except (anthropic.APIError, httpx2.HTTPError, OSError) as exc:
            log.warning("chat session=%s failed: %s", session, type(exc).__name__)
            return JSONResponse(
                {"error": "The demo is resting right now. Try again in a minute."}, status_code=503
            )

        if response.stop_reason == "refusal":
            reply = "I can only help with Fawry payments in this demo."
        else:
            reply = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "tool_use":  # ran out of tool rounds
                reply = reply or "That took too many steps. Try a simpler request."
        return JSONResponse({"reply": reply, "steps": steps, "history": messages})

    async def pay(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = None
        reference = body.get("reference") if isinstance(body, dict) else None
        if not isinstance(reference, str) or not REFERENCE_RE.match(reference):
            return JSONResponse({"error": "Bad reference number."}, status_code=400)
        async with mock_http() as http:
            paid = await http.post(f"/__admin/kiosk-pay/{reference}")
        if paid.status_code != 200:
            return JSONResponse({"error": "That reference can't be paid now."}, status_code=409)
        return JSONResponse({"paid": True})

    return Starlette(
        routes=[
            Route("/", index, methods=["GET"]),
            Route(
                "/og.png", lambda r: FileResponse(Path(__file__).parent / "og.png"), methods=["GET"]
            ),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/pay", pay, methods=["POST"]),
        ]
    )


def claude_client() -> Callable[[], anthropic.AsyncAnthropic]:
    """The Claude client, rebuilt at most every 5 minutes so a newly saved key is picked up.

    The key comes from Secrets Manager on Lambda; locally the SDK reads ANTHROPIC_API_KEY.
    """
    arn = os.environ.get("ANTHROPIC_API_KEY_SECRET_ARN", "").strip()
    cached: dict[str, Any] = {"at": float("-inf"), "client": None}

    def get() -> anthropic.AsyncAnthropic:
        if time.monotonic() - cached["at"] > 300:
            key = None
            if arn:
                import boto3

                secret = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
                key = secret["SecretString"].strip()
            cached.update(
                at=time.monotonic(),
                client=anthropic.AsyncAnthropic(api_key=key, timeout=20, max_retries=1),
            )
        return cached["client"]

    return get


def app_from_env() -> Starlette:
    mcp_url, mock_url = os.environ["DEMO_MCP_URL"], os.environ["DEMO_MOCK_URL"].rstrip("/")

    @asynccontextmanager
    async def connect_mcp() -> AsyncIterator[Client]:
        async with (
            httpx2.AsyncClient(headers=DEMO_HEADERS, timeout=25) as http,
            Client(streamable_http_client(mcp_url, http_client=http)) as client,
        ):
            yield client

    table = os.environ.get("DEMO_USAGE_TABLE", "").strip()
    limit = int(os.environ.get("DEMO_DAILY_MESSAGES", "400"))
    return create_app(
        claude=claude_client(),
        connect_mcp=connect_mcp,
        mock_http=lambda: httpx2.AsyncClient(base_url=mock_url, timeout=10),
        # shortcut: without a table (local runs) there is no daily cap; the Lambda always has one.
        take_slot=dynamodb_daily_slot(table, limit) if table else lambda: True,
    )
