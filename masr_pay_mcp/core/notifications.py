"""Payment-update hints from a provider's webhook ("order 42 changed, go look").

A hint is **untrusted**. Checking a webhook's own signature would need the merchant's secret
key on hand when it arrives, which means storing it (docs/design.md rule 2 forbids that). So the
only thing kept is *which order* may have changed: merchant code, order id and time, no
customer data, no amounts, deleted after 7 days. When the assistant asks "what got paid?", each
order is re-checked with the provider through the normal signed and verified status call, using
the caller's own credentials. A forged hint costs one extra status check and changes no answer.

The webhook URL carries an HMAC of the merchant code, so only URLs this server handed to the
provider (in the charge request) are accepted. Random spam is refused before anything is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

TTL = timedelta(days=7)
MAX_RECENT = 20
HINTS_TABLE_ENV = "MASR_PAY_HINTS_TABLE"
NOTIFY_PREFIX = "/notify"
MERCHANT_CODE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def notify_token(secret: bytes, merchant_code: str) -> str:
    # "notify:" keeps these MACs apart from confirmation tokens made with the same secret.
    mac = hmac.new(secret, f"notify:{merchant_code}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def notify_path(secret: bytes, merchant_code: str) -> str:
    return f"{NOTIFY_PREFIX}/{quote(merchant_code, safe='')}/{notify_token(secret, merchant_code)}"


def token_is_valid(secret: bytes, merchant_code: str, token: str) -> bool:
    if not MERCHANT_CODE.fullmatch(merchant_code):
        return False
    return hmac.compare_digest(notify_token(secret, merchant_code), token)


class HintStore(Protocol):
    async def put(self, merchant_code: str, merchant_ref: str, at: datetime) -> None: ...

    async def recent(self, merchant_code: str, since: datetime) -> list[str]:
        """Distinct order ids hinted since ``since``, newest first, at most MAX_RECENT."""
        ...


def _distinct(refs: list[str]) -> list[str]:
    return list(dict.fromkeys(refs))[:MAX_RECENT]


class MemoryHintStore:
    """For tests and local runs. One process only."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, datetime]] = []

    async def put(self, merchant_code: str, merchant_ref: str, at: datetime) -> None:
        self.items.append((merchant_code, merchant_ref, at))

    async def recent(self, merchant_code: str, since: datetime) -> list[str]:
        hits = [(at, ref) for code, ref, at in self.items if code == merchant_code and at >= since]
        return _distinct([ref for _, ref in sorted(hits, reverse=True)])


class DynamoHintStore:
    """DynamoDB table: pk = merchant code, sk = "<ISO time>#<order id>", ``expires`` = TTL."""

    def __init__(self, table_name: str, client: Any = None, clock: Callable[[], float] = time.time):
        if client is None:
            import boto3  # in the Lambda Python runtime already; only needed on AWS

            client = boto3.client("dynamodb")
        self._table, self._db, self._clock = table_name, client, clock

    async def put(self, merchant_code: str, merchant_ref: str, at: datetime) -> None:
        import anyio

        item = {
            "pk": {"S": merchant_code},
            "sk": {"S": f"{at.isoformat()}#{merchant_ref}"},
            "ref": {"S": merchant_ref},
            "expires": {"N": str(int(self._clock() + TTL.total_seconds()))},
        }
        await anyio.to_thread.run_sync(lambda: self._db.put_item(TableName=self._table, Item=item))

    async def recent(self, merchant_code: str, since: datetime) -> list[str]:
        import anyio

        def query() -> list[str]:
            page = self._db.query(
                TableName=self._table,
                KeyConditionExpression="pk = :pk AND sk >= :since",
                ExpressionAttributeValues={
                    ":pk": {"S": merchant_code},
                    ":since": {"S": since.isoformat()},
                },
                ScanIndexForward=False,  # newest first
                Limit=MAX_RECENT * 5,  # repeats of one order collapse below
            )
            return [item["ref"]["S"] for item in page.get("Items", [])]

        return _distinct(await anyio.to_thread.run_sync(query))
