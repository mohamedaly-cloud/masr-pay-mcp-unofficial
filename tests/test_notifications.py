"""Payment notifications: Fawry (the mock) -> /notify -> hint store -> list_recent_payment_updates.

Notifications are untrusted hints; every reported status is re-checked with Fawry.
"""

from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from masr_pay_mcp import web
from masr_pay_mcp.core import notifications
from masr_pay_mcp.core.notifications import DynamoHintStore, MemoryHintStore
from tests.conftest import MERCHANT
from tests.test_web import CREDS, SECRET, connect

pytestmark = pytest.mark.anyio

CREATE = {
    "amount_egp": 150,
    "order_id": "42",
    "customer_mobile": "01012345678",
    "customer_email": "ahmed@example.com",
    "description": "Order 42",
}


@pytest.fixture
def hints():
    return MemoryHintStore()


@pytest.fixture
def app(monkeypatch, transport, clock, mock_app, hints):
    monkeypatch.setenv("MASR_PAY_CONFIRM_SECRET", SECRET)
    monkeypatch.setenv("FAWRY_BASE_URL", "http://mock-fawry")
    application = web.create_app(transport=transport, now=clock.now, hints=hints)

    async def deliver(url, payload):  # the mock's webhook call, routed in-process to our app
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application)) as http:
            (await http.post(url, json=payload)).raise_for_status()

    mock_app.state.settings.notify = deliver
    return application


async def kiosk_pay(transport, reference):
    async with httpx2.AsyncClient(transport=transport, base_url="http://mock-fawry") as http:
        (await http.post(f"/__admin/kiosk-pay/{reference}")).raise_for_status()


async def post_notification(app, path, body):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as http:
        return await http.post(path, json=body)


SECRET_BYTES = SECRET.encode()


# ------------------------------------------------------------------ end to end
async def test_paid_order_shows_up_without_naming_it(app, transport, mock_app):
    async with connect(app, CREDS) as client:
        created = await client.call_tool("create_cash_payment", CREATE)
        reference = created.structured_content["reference_number"]
        # Our server told Fawry where to report changes, on a URL only it can mint.
        url = mock_app.state.store.get_by_reference(reference).order_webhook_url
        assert url == f"https://test{notifications.notify_path(SECRET_BYTES, MERCHANT)}"

        await kiosk_pay(transport, reference)  # the mock notifies us, like Fawry would

        result = await client.call_tool("list_recent_payment_updates", {})
    assert not result.is_error, result
    [update] = result.structured_content["updates"]
    assert update["order_id"] == "42" and update["is_paid"] is True
    assert "1 of them paid" in result.structured_content["summary"]


async def test_forged_notification_changes_no_answer(app, hints):
    path = notifications.notify_path(SECRET_BYTES, MERCHANT)
    # A valid URL, but for an order Fawry has never heard of.
    response = await post_notification(
        app, path, {"merchantRefNumber": "999", "orderStatus": "PAID"}
    )
    assert response.status_code == 200
    async with connect(app, CREDS) as client:
        result = await client.call_tool("list_recent_payment_updates", {})
    assert result.structured_content["updates"] == []  # Fawry was asked, and doesn't know it


async def test_forged_paid_status_is_not_believed(app, hints):
    async with connect(app, CREDS) as client:
        await client.call_tool("create_cash_payment", CREATE)
        path = notifications.notify_path(SECRET_BYTES, MERCHANT)
        await post_notification(app, path, {"merchantRefNumber": "42", "orderStatus": "PAID"})
        result = await client.call_tool("list_recent_payment_updates", {})
    [update] = result.structured_content["updates"]
    assert update["is_paid"] is False and update["status"] == "awaiting_payment"


@pytest.mark.parametrize(
    "path",
    [
        f"/notify/{MERCHANT}/{'0' * 32}",  # made-up token
        notifications.notify_path(SECRET_BYTES, "OTHERMERCHANT").replace("OTHERMERCHANT", MERCHANT),
        notifications.notify_path(b"another-server-secret-xxxxxxxxxxxxxx", MERCHANT),
    ],
)
async def test_notification_urls_we_did_not_mint_are_refused(app, hints, path):
    response = await post_notification(app, path, {"merchantRefNumber": "42"})
    assert response.status_code == 404
    assert hints.items == []


@pytest.mark.parametrize("body", [{}, {"merchantRefNumber": "../../etc"}, ["42"], "x"])
async def test_notification_without_a_valid_order_id_is_rejected(app, hints, body):
    path = notifications.notify_path(SECRET_BYTES, MERCHANT)
    response = await post_notification(app, path, body)
    assert response.status_code == 400
    assert hints.items == []


async def test_notification_stores_only_the_order_id(app, hints):
    path = notifications.notify_path(SECRET_BYTES, MERCHANT)
    body = {"merchantRefNumber": "42", "customerMobile": "01012345678", "customerMail": "a@b.c"}
    assert (await post_notification(app, path, body)).status_code == 200
    [(merchant, ref, _at)] = hints.items
    assert (merchant, ref) == (MERCHANT, "42")


async def test_merchants_only_see_their_own_updates(app, hints):
    other = notifications.notify_path(SECRET_BYTES, "OTHERMERCHANT")
    await post_notification(app, other, {"merchantRefNumber": "42"})
    async with connect(app, CREDS) as client:
        result = await client.call_tool("list_recent_payment_updates", {})
    assert result.structured_content["updates"] == []


async def test_tool_exists_only_where_notifications_can_arrive(monkeypatch, transport):
    monkeypatch.setenv("MASR_PAY_CONFIRM_SECRET", SECRET)
    monkeypatch.delenv(notifications.HINTS_TABLE_ENV, raising=False)
    async with connect(web.create_app(transport=transport), CREDS) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert "list_recent_payment_updates" not in tools


# ------------------------------------------------------------------ stores
async def test_memory_store_newest_first_distinct_and_since():
    store = MemoryHintStore()
    t = datetime(2026, 10, 31, 12, tzinfo=UTC)
    for minutes, ref in [(0, "a"), (1, "b"), (2, "a"), (-120, "old")]:
        await store.put(MERCHANT, ref, t + timedelta(minutes=minutes))
    assert await store.recent(MERCHANT, t - timedelta(hours=1)) == ["a", "b"]


class FakeDynamo:
    def __init__(self):
        self.calls = []

    def put_item(self, **kwargs):
        self.calls.append(("put", kwargs))

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return {"Items": [{"ref": {"S": "b"}}, {"ref": {"S": "a"}}, {"ref": {"S": "b"}}]}


async def test_dynamo_store_writes_only_ids_with_a_ttl_and_queries_newest_first():
    db = FakeDynamo()
    store = DynamoHintStore("hints", client=db, clock=lambda: 1_000_000.0)
    at = datetime(2026, 10, 31, 12, tzinfo=UTC)
    await store.put(MERCHANT, "42", at)
    assert await store.recent(MERCHANT, at) == ["b", "a"]
    (_, put), (_, query) = db.calls
    assert put["Item"] == {
        "pk": {"S": MERCHANT},
        "sk": {"S": "2026-10-31T12:00:00+00:00#42"},
        "ref": {"S": "42"},
        "expires": {"N": str(1_000_000 + 7 * 24 * 3600)},
    }
    assert query["ScanIndexForward"] is False
    assert query["ExpressionAttributeValues"][":pk"] == {"S": MERCHANT}


def test_notify_token_depends_on_merchant_and_secret():
    assert notifications.token_is_valid(
        SECRET_BYTES, MERCHANT, notifications.notify_token(SECRET_BYTES, MERCHANT)
    )
    assert not notifications.token_is_valid(
        SECRET_BYTES, "OTHER", notifications.notify_token(SECRET_BYTES, MERCHANT)
    )
    assert not notifications.token_is_valid(SECRET_BYTES, "bad/code", "x")
