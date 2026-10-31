# Decisions ("Why" log)

Short, plain-English reasons for the non-obvious choices, so each one can be defended in a
review or an interview. Newest milestone first.

## Milestone 1: mock, signatures, tests

### Dependencies outside the stack table (awaiting director approval)
| Package | Used for | Why it's needed |
|---|---|---|
| `uvicorn` | Running the FastAPI mock locally (`python -m mock_fawry`) | FastAPI is only a framework; something has to serve HTTP. Uvicorn is the server FastAPI documents. On Lambda (milestone 3) it won't be used. |
| `httpx` (dev only) | FastAPI's `TestClient` | `TestClient` requires it. It's also the likely HTTP client for `client.py` in milestone 2. |
| `ruff` (dev only) | Lint and format | One fast tool in place of flake8 + isort + black; the "lint" step in CI. Enables the `S` (bandit) rules so things like hard-coded secrets get flagged. |
| `hatchling` (build only) | Packaging from `pyproject.toml` | Standard, no-config build backend. |

Starlette now warns that `TestClient` prefers `httpx2` over `httpx`. Tests pass either way;
we'll decide on the HTTP client in milestone 2 and settle this then.

### Signatures
- **One function per recipe, each split into `*_plain` + hash.** Most signature bugs are
  field-order bugs. Testing the plain string separately tells us *which* field is wrong
  instead of just "hash mismatch".
- **Test vectors are computed outside our code.** Every hash in `tests/vectors.json` came from
  `printf '%s' "$plain" | sha256sum`. If the vectors were generated with `signatures.py`, the
  tests would only check that the code agrees with itself. Fawry's own doc samples aren't
  used because they don't match Fawry's recipe (see docs/design.md, "Known traps").
- **Amounts: `Decimal`, never round.** `format_amount` rejects anything with more than two
  decimals instead of rounding. The signed amount must equal the amount in the body, so a
  silent rounding would just produce a confusing 9946 from Fawry. Floats go through `str()`
  first so `580.55` doesn't become `580.549999…`.
- **Constant-time comparison** (`hmac.compare_digest`) when checking signatures, so response
  time doesn't leak how many leading characters were right. It costs nothing, and it's the
  answer a reviewer expects.
- **Arabic text is tested.** A refund reason in Arabic is a realistic input; the vector proves
  we hash UTF-8 bytes (the docs' Python sample hashes a `str`, which fails on Python 3).

### Mock
- **FastAPI, not Prism.** Prism serves responses from an OpenAPI spec but can't check
  signatures, keep state, or simulate a customer paying at an outlet. All three are the point.
- **The mock reuses `fawry_mcp.signatures`.** Trade-off: a recipe bug would be shared by the
  mock and our client and could hide in end-to-end tests. Mitigation: the recipes themselves are
  pinned by the independent vectors above, so the shared code is already checked against
  something outside it.
- **Expiry is applied lazily when an order is read**, against an injectable clock. No
  background threads, deterministic tests ("advance the clock one hour"), and it fits Lambda
  later, where there's no long-lived process to run timers.
- **Error convention.** Documented Fawry codes (9901, 9938, 9946, 9935, 9954) come back as
  HTTP 200 with `statusCode` set, which is how Fawry's samples look. Cases Fawry doesn't
  document a code for come back as HTTP 4xx, `statusCode` = HTTP status, and a description
  starting with `[mock]`. We don't invent Fawry-looking codes, so nobody mistakes a mock
  behaviour for real Fawry behaviour. Consequence for milestone 2: the client must always
  check `statusCode`, not only the HTTP status.
- **The order of checks is merchant → body → signature → business rules**, so a bad
  signature can't be used to probe whether an order exists.
- **The mock's log handlers redact.** The status v2 endpoint is a GET with `signature` in the
  query string, so uvicorn's access log would otherwise print it (rule 3). `RedactingFilter`
  masks values in place and keeps the arguments' shape. A live run caught a first version that
  broke uvicorn's formatter, so the test now uses uvicorn's real `AccessFormatter`.
- **Admin endpoints have no auth.** They only move fake money in a mock. When the mock is
  deployed publicly (milestone 3), anyone can "pay" a demo order. That's acceptable for a
  demo, but revisit it if the demo is shared widely.

## Verify list
Things the code assumes but that Fawry hasn't confirmed. Each one is also marked in a code
comment. Settle them once sandbox access or official answers arrive.

| # | Assumption | Where |
|---|---|---|
| V1 | Status v2 signature recipe `merchantCode + merchantRefNumber + secureKey` [secondary] | `signatures.status_v2_*` |
| V2 | Status v2 responses are signed with the charge-response recipe, with `fawryRefNumber` in place of `referenceNumber` | `mock_fawry/app.py`, `signed_order_fields` |
| V3 | Refund signature recipe [third-party] | `signatures.refund_*` |
| V4 | Cancel-unpaid endpoint, fields and signature [third-party]; `orderRefNo` is Fawry's `referenceNumber` (not the merchant's) | `signatures.cancel_unpaid_*`, mock cancel handler |
| V5 | Only one refund per payment; refunds rounded down to whole EGP (Checkout.com). The mock enforces one refund and does **not** round | mock refund handler |
| V6 | A refund is capped at the order amount (Fawry's fees aren't refundable by the merchant) | mock refund handler |
| V7 | Business errors come back as HTTP 200 with a Fawry `statusCode` | mock `fawry_result` |
| V8 | Fawry's behaviour on a duplicate `merchantRefNum` (the mock answers 409) | mock charge handler |
| V9 | Refund and cancel response bodies (`ResponseDataModel` shape) and whether they're signed | mock |
| V10 | Fawry's default expiry when `paymentExpiry` is omitted (the mock: never expires) | `state._apply_expiry` |
