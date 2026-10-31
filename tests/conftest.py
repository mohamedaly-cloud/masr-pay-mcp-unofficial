"""Shared fixtures: a mock Fawry app reachable in-process through an httpx2 ASGI transport."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx2
import pytest

from masr_pay_mcp.providers.fawry.config import Credentials, FawryConfig
from mock_fawry.app import MockSettings, create_app

MERCHANT = "TESTMERCHANT01"
KEY = "fake-test-key"
T0 = 1_790_000_000_000  # an arbitrary "now", in epoch ms
HOUR_MS = 3_600_000


class FakeClock:
    """Epoch-milliseconds clock shared by the mock and the MCP server under test."""

    def __init__(self, now_ms: int = T0):
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.now_ms / 1000, tz=UTC)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def mock_app(clock):
    return create_app(
        MockSettings(
            merchant_code=MERCHANT, secure_key=KEY, fawry_fees=Decimal("5.00"), clock=clock
        )
    )


@pytest.fixture
def transport(mock_app):
    return httpx2.ASGITransport(app=mock_app)


@pytest.fixture
def credentials():
    return Credentials(merchant_code=MERCHANT, secure_key=KEY)


@pytest.fixture
def config(credentials):
    return FawryConfig(base_url="http://mock-fawry", environment="mock", credentials=credentials)


@pytest.fixture
def anyio_backend():
    # anyio's pytest plugin ships with the MCP SDK's anyio dependency; no pytest-asyncio needed.
    return "asyncio"
