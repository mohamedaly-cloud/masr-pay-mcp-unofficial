# MCP Inspector walkthrough

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) shows the raw JSON-RPC
between a client and our server: tool schemas, annotations, structured results and errors. Use it
to see exactly what an AI assistant sees.

> These steps haven't been run in the build environment (downloading the Inspector from npm was
> blocked there). `scripts/demo.py` covers the same stdio path automatically.

## 1. Start the mock (terminal 1)
```bash
. .venv/bin/activate
python -m mock_fawry            # http://127.0.0.1:8080, merchant MOCKMERCHANT0001
```

## 2. Start the Inspector with our server (terminal 2)
```bash
npx -y @modelcontextprotocol/inspector \
  -e FAWRY_ENV=mock \
  -e FAWRY_MERCHANT_CODE=MOCKMERCHANT0001 \
  -e FAWRY_SECURE_KEY=mock-secure-key-not-a-real-fawry-key \
  .venv/bin/python -m fawry_mcp
```
The Inspector opens in the browser with transport **STDIO** and our command filled in. Click
**Connect**.

## 3. What to check

| Step | Do | Expect |
|---|---|---|
| Tools → List | List the tools | 4 tools. `cancel_unpaid_payment` and `refund_payment` show `destructiveHint: true` and "REQUIRES HUMAN CONFIRMATION" in the description; `get_payment_status` shows `readOnlyHint: true`. No `ctx`, `plan` or `answer` in any input schema. |
| create_cash_payment | `amount_egp=150`, `order_id=42`, `customer_mobile=01012345678`, `customer_email=ahmed@example.com`, `description=Order 42` | `structuredContent` with a 9-digit `reference_number`, `status: awaiting_payment`, `total_to_pay_egp: "150.00"`, `environment: mock`. |
| get_payment_status | `order_id=42` | `is_paid: false`, a "not paid yet" summary. |
| Pay at the "outlet" | `curl -X POST http://127.0.0.1:8080/__admin/kiosk-pay/<reference_number>` | The mock returns the order with `orderStatus: PAID`. |
| get_payment_status | `order_id=42` | `is_paid: true`, `paid_at_cairo` set. |
| refund_payment without confirmation | `order_id=42`, `reason=test` | `isError: true`, "needs explicit confirmation… Nothing was changed." |
| refund_payment with confirmation | add `confirmed_by_user=true` | If your Inspector version supports elicitation, a confirmation form appears with the amount and reference. **Decline** → "did not confirm"; **Accept + confirm** → `status: refunded`, `confirmation: asked the user directly`. Without elicitation the refund goes ahead on the flag, and the result says `confirmation: confirmed_by_user flag`. |
| Error meaning | Restart the Inspector with a wrong `FAWRY_SECURE_KEY`, call `get_payment_status` | `isError: true` mentioning code 9946 and `FAWRY_SECURE_KEY`. The key itself never appears. |

## 4. Look at the raw messages
In the history pane, open a confirmed `refund_payment` call. What you see depends on the protocol
version your Inspector negotiates; the server handles both, and `tests/test_server.py` covers both:

- **2026-07-28 (stateless):** the first `tools/call` is answered with an `InputRequiredResult`
  carrying the confirmation question. The client then retries the same call with the user's
  answer in `inputResponses`. No server-to-client request is needed, which is what makes the
  server Lambda-friendly.
- **2025-11-25 and older (handshake):** after `initialize`, the server sends a mid-call
  `elicitation/create` request and waits for the answer.
