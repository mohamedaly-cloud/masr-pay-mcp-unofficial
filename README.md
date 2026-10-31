# fawry-mcp-unofficial

An **unofficial** [MCP](https://modelcontextprotocol.io) server for FawryPay. It lets an AI
assistant take cash payments in Egypt through **Pay-at-Fawry**: the assistant creates a
reference number, the customer pays cash at any Fawry outlet, and the assistant checks whether
it was paid.

It's for merchants contracted **directly with FawryPay**, and for the developers and agencies
who build sales bots for them. It's an MCP server for FawryPay's own API, not the only way an
agent can take cash payments in Egypt.

> **Not affiliated with, endorsed by, or supported by Fawry.** "Fawry" and "FawryPay" are used
> only to describe the API this project talks to. The server is stateless: it never stores
> credentials or funds.

## Status
Milestone 2 of 4: four MCP tools over stdio, built on the official MCP Python SDK v2 (protocol
2026-07-28), tested against a local mock of the Fawry API. Until official sandbox credentials are
issued, all development runs against the mock.

## Tools
| Tool | What it does | Safety |
|---|---|---|
| `create_cash_payment` | Creates a Pay-at-Fawry reference for an order (amount in EGP, customer mobile/email, expiry in hours, default 72) | |
| `get_payment_status` | Is it paid? Normalized status (`awaiting_payment`, `paid`, `expired`, …) plus Fawry's raw status | read-only |
| `cancel_unpaid_payment` | Cancels a reference before it's paid | destructive, needs confirmation |
| `refund_payment` | Refunds a paid order (full by default; one refund per order) | destructive, needs confirmation |

A destructive tool needs three things: the host's approval prompt (`destructiveHint`), an explicit
`confirmed_by_user=true` from the model, and, when the client supports MCP elicitation, a direct
"yes" from the human, who sees the exact amount and reference.

## Layout
```
fawry_mcp/server.py       the four MCP tools
fawry_mcp/client.py       Fawry HTTP client: signs requests, verifies responses, normalizes fields
fawry_mcp/config.py       environment + credentials (read fresh on every call)
fawry_mcp/signatures.py   Fawry SHA-256 signature recipes, one function each
fawry_mcp/redaction.py    keeps secrets and customer PII out of logs
mock_fawry/               FastAPI mock of Fawry staging (in-memory orders)
tests/                    pytest; tests/vectors.json holds our own signature vectors
scripts/demo.py           milestone demo over real stdio
docs/decisions.md         why things are the way they are, plus the "verify" list
docs/inspector.md         MCP Inspector walkthrough
```

## Develop
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

## Run the demo
```bash
python scripts/demo.py   # mock + MCP server over stdio: create 150 EGP for order 42 -> pay -> "paid?"
```

## Use it from an MCP host
The server speaks stdio: `python -m fawry_mcp`, or the `fawry-mcp-unofficial` command. Example
a desktop AI app entry, pointed at the local mock:
```json
{
  "mcpServers": {
    "fawry-unofficial": {
      "command": "/path/to/repo/.venv/bin/fawry-mcp-unofficial",
      "env": {
        "FAWRY_ENV": "mock",
        "FAWRY_MERCHANT_CODE": "MOCKMERCHANT0001",
        "FAWRY_SECURE_KEY": "mock-secure-key-not-a-real-fawry-key"
      }
    }
  }
}
```
| Env var | Meaning |
|---|---|
| `FAWRY_MERCHANT_CODE`, `FAWRY_SECURE_KEY` | Required. Read on every call, never stored. |
| `FAWRY_ENV` | `mock` (default, `http://127.0.0.1:8080`), `staging` or `production` |
| `FAWRY_BASE_URL` | Overrides the URL picked by `FAWRY_ENV` |
| `FAWRY_LOG_LEVEL` | Default `INFO`. Logs go to stderr, redacted. |

See [docs/inspector.md](docs/inspector.md) to watch the raw MCP traffic.

## Run the mock
```bash
python -m mock_fawry          # http://127.0.0.1:8080, OpenAPI docs at /docs
```
| Env var | Default |
|---|---|
| `MOCK_MERCHANT_CODE` | `MOCKMERCHANT0001` |
| `MOCK_SECURE_KEY` | a built-in fake key |
| `MOCK_FAWRY_FEES` | `0` (EGP added to each order's payment amount) |
| `MOCK_HOST` / `MOCK_PORT` | `127.0.0.1` / `8080` |

Fawry endpoints (same paths as Fawry):
`POST /ECommerceWeb/Fawry/payments/charge`, `GET /ECommerceWeb/Fawry/payments/status/v2`,
`POST /ECommerceWeb/Fawry/payments/refund`, `POST /ECommerceWeb/api/orders/cancel-unpaid-order`.

Mock-only admin endpoints:
| Endpoint | Does |
|---|---|
| `POST /__admin/kiosk-pay/{referenceNumber}` | Simulates the customer paying cash at an outlet |
| `GET /__admin/orders` | Lists all orders |
| `POST /__admin/reset` | Clears all orders |

Order lifecycle in the mock: `UNPAID → PAID` (kiosk-pay), `UNPAID → EXPIRED` (after
`paymentExpiry`), `UNPAID → CANCELED`, `PAID → REFUNDED` (one refund per order).

## License
MIT
