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
Milestone 1 of 4: signature recipes, a local mock of the Fawry API, and tests. The MCP tools
come in milestone 2. Until official sandbox credentials are issued, all development runs
against the mock.

## Layout
```
fawry_mcp/signatures.py   Fawry SHA-256 signature recipes, one function each
fawry_mcp/redaction.py    keeps secrets and customer PII out of logs
mock_fawry/               FastAPI mock of Fawry staging (in-memory orders)
tests/                    pytest; tests/vectors.json holds our own signature vectors
docs/decisions.md         why things are the way they are, plus the "verify" list
```

## Develop
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

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
