# Decisions ("Why" log)

Short, plain-English reasons for the non-obvious choices, so each one can be defended in a
review or an interview. Newest milestone first.

## Milestone 2: MCP server, stdio

### SDK and dependencies
- **Official MCP Python SDK 2.3 (`mcp>=2.3,<3`), class `MCPServer`.** Answers the open question
  in docs/design.md: v2 imports are `from mcp.server import MCPServer` and `from mcp import Client`.
  FastMCP was folded into the official SDK and renamed `MCPServer`, so there's no separate
  FastMCP to evaluate. The SDK lists `2026-07-28` as its modern (stateless) protocol and still
  negotiates older revisions with clients that need them.
- **`httpx2` for the Fawry client.** The MCP SDK already depends on it, so it adds nothing to
  the install. It also replaces the dev-only `httpx` from milestone 1 (Starlette's `TestClient`
  prefers it), so that dependency is gone. **No new third-party dependencies in milestone 2.**
- **`anyio`'s pytest plugin for async tests** instead of adding `pytest-asyncio`: anyio already
  comes with the SDK.
- **Lint: `ruff format` owns line length; E501 is off.** Tool descriptions are long prose
  strings that the formatter deliberately doesn't wrap.

### Statelessness
- **Credentials are read from the environment on every tool call** and a new `FawryClient` is
  built per call. Nothing is cached, so rotating a key needs no restart, and the same code moves
  to Lambda in milestone 3, where credentials will come from per-request headers instead.
- **`Credentials.secure_key` has `repr=False`**, so the key can't leak through a traceback, an
  error message or a debug print. A test checks this.
- **The default environment is `mock`.** A server started with no `FAWRY_ENV` can never hit
  production by accident; production has to be asked for by name.

### Client
- **Every response that carries order data has its signature verified, and the client fails
  closed.** A response that doesn't verify (tampered, wrong key, or missing fields) raises
  `ResponseSignatureError` and the tool reports "can't be trusted". The alternative, logging a
  warning and continuing, would let a spoofed "PAID" through, which is exactly what an attacker
  would want.
- **Field names are normalized in one place.** `merchantRefNum`/`merchantRefNumber` and
  `referenceNumber`/`fawryRefNumber` become one `Payment` dataclass. Statuses map to a small
  normalized set (`NEW`/`UNPAID`/`PENDING` → `awaiting_payment`), and the raw value is kept too.
- **The client always checks Fawry's `statusCode`**, not only the HTTP status (milestone 1
  decision V7).
- **No automatic retries.** A charge isn't idempotent from our side, so retrying a timed-out
  POST could create confusion. Errors tell the model it's safe to *check the status* again,
  which is the idempotent way to recover.

### Tools
- **The tools take `order_id`, not Fawry's reference number, even for cancel and refund.** The
  user says "refund order 42", not "refund 309418146". The tool looks up the reference itself,
  which also gives it a fresh, signed status to check before acting.
- **Inputs are validated before anything goes to Fawry**: amounts > 0 with at most two decimals,
  Egyptian mobiles (`+20`/`0020` forms are normalized to `01…`), order ids restricted to
  `[A-Za-z0-9._-]`, and expiry from just above 0 up to 720 hours. Invalid input becomes a tool
  error, never a Fawry 9946.
- **Results are structured (Pydantic models)** and include a plain-English `summary`, the
  `environment` (so the model can say "this was a test payment") and both UTC and Cairo times.
  Amounts are strings like `"150.00"` so they can't pick up float noise.
- **Errors are `ToolError`s written for the model**: what happened, whether anything changed,
  what to do next (e.g. "use refund_payment instead"). Secrets are never echoed back; a test
  checks that a wrong key doesn't appear in the error.
- **Fees aren't refunded by default.** A full refund is the order amount, not the payment
  amount (verify list V6).

### Human confirmation for cancel and refund (rule 4)
Three layers. Each one alone can be bypassed; together they are hard to get wrong:
1. **`destructiveHint: true`** makes MCP hosts show their own approval prompt.
2. **Two-step HMAC confirmation token** (docs/design.md "Security and trust" #1). Call 1 without a
   token changes nothing: it returns a preview and a token. Call 2 with the same arguments plus
   the token does the action. The token is `<expiry>.<HMAC-SHA256(secret, tool + exact args +
   expiry)>`, valid 5 minutes. It replaced an earlier `confirmed_by_user: bool` flag:
   - *Why the token beats the flag:* the flag was one call, so a model could set it without
     ever showing the human anything. The token can't exist before a preview has been
     produced, and it binds approval to the exact order, reference, amount and reason. "Approve
     10 EGP, execute 150 EGP" fails (tested).
   - *Why it stays stateless:* the server stores nothing. It recomputes the HMAC from call 2's
     arguments and compares in constant time. Single use isn't enforced with a store; a
     replay inside the 5 minutes finds the order already refunded or cancelled, because the
     status is re-checked on every call. See `docs/threat-model.md` T3.
   - *Where the secret comes from:* `MASR_PAY_CONFIRM_SECRET` (at least 32 chars), or a random
     key per process when it's unset. That's right for stdio. On Lambda (milestone 3) it must
     be set, or tokens fail closed across instances.
   - *What's bound:* tool name, `order_id`, the Fawry reference, the resolved amount (so "full
     refund" means the amount the human saw) and the reason (refund) or language (cancel).
3. **MCP elicitation**: when the client supports it, the server asks the human directly,
   showing the exact amount, order and reference. Decline, cancel, or an unticked box means
   nothing happens. It's asked only on call 2: call 1 changes nothing, so nobody is asked
   about a preview.

How layer 3 is built matters for milestone 3. Under protocol 2026-07-28 a server can't push a
request to the client mid-call (a first version using `ctx.elicit()` failed with
`NoBackChannelError`). The SDK's answer is **resolvers**: tool parameters annotated
`Annotated[T, Resolve(fn)]` that run before the tool body. Each destructive tool has two:
- **`plan`** checks the flag, looks up the order and checks its status and amount. It fails fast,
  so the human is never asked about something that would fail anyway (there's a test for this).
- **`answer`** returns an `Elicit(question)`. The SDK turns it into an `InputRequiredResult`
  round on 2026-07-28, or a mid-call `elicitation/create` on older protocols. Both are tested.

Resolvers may run again on the retry round. That's safe because `plan` is read-only: the money
moves only in the tool body, which runs once, after an explicit yes. `plan` also verifies the
token, so a bad token is refused before the human is asked anything. Each result records how
confirmation was obtained (`asked the user directly` or `confirmation token`).

### Refunds and cancellations are opt-in, with their own limit
- **Off by default (`MASR_PAY_ENABLE_DESTRUCTIVE_TOOLS`).** The whole confirmation design
  assumes that the human saying "yes" is the merchant. If the server is connected to a
  customer-facing sales bot, that human is the customer, who could approve their own refund.
  The safest default for a payments tool is that it can't move money out unless the merchant
  asked for that. When the tools are disabled they're unregistered, not just refused, so a
  model can't even be tempted.
- **Only an explicit `true`/`1`/`yes`/`on` enables them.** Anything unrecognized (a typo such as
  `ture`) stops the server at startup rather than guessing either way.
- **A per-refund cap, default 2,000 EGP (`MASR_PAY_MAX_REFUND_EGP`).** It's lower than the
  50,000 EGP payment cap because the costs aren't symmetric: a wrong charge just goes unpaid,
  but a wrong refund is money gone. Larger refunds go through the merchant's Fawry dashboard.
  The 2,000 figure is a starting point, chosen so a typical retail order fits under it; set it
  per merchant.
- **No daily total yet.** A cap on all refunds per day needs a running total, which needs state
  (a database). It doesn't fit v1's no-database rule. Revisit in milestone 3 (DynamoDB or
  CloudWatch metrics).

### Audit log
- **One JSON line per tool call on the `masr_pay_mcp.audit` logger**, written to stderr (stdout
  is the MCP channel) and, from milestone 3, CloudWatch. Fields: `ts`, `tool`, `request_id`,
  `merchant_ref`, `amount_egp`, `outcome` (`ok` / `preview` / `error`), `raw_status`,
  `environment`, `provider_code` (Fawry's statusCode on errors) and `error`.
- **One line per call, including refusals.** A destructive call that is refused in its
  pre-check (not paid, already refunded, bad token) is logged from the pre-check. One that gets
  past it is logged from the tool body. A refused refund is exactly what you want in an audit
  trail when someone asks "did the bot try to refund order 42?".
- **Double redaction.** The entry goes through `redact_mapping` before it's serialized, and the
  line goes through the handler's `RedactingFilter`. Customer mobile and email are never put in
  the entry in the first place.
- **`FAWRY_LOG_LEVEL` can't silence it.** The audit logger is pinned to INFO, because an audit
  trail you can switch off by accident isn't one.
- **A context manager, not SDK middleware.** The SDK's middleware API is marked as liable to
  change in 2.x minors, and it sees raw JSON-RPC, not Fawry status codes.

### Guardrails
- **A per-payment limit, default 50,000 EGP (`MASR_PAY_MAX_PAYMENT_EGP`).** It's read once at
  startup, so a typo fails the server at boot rather than on a customer's payment. It's checked
  before anything goes to Fawry.
- **The refund cap moved to `core/guardrails.py`.** Same rule as before (≤ order amount, V6), but
  now provider-neutral and unit-tested on its own.
- **`merchantRefNum` is the merchant's `order_id`, not a server-generated value (decided
  2026-10-31; docs/design.md updated).** The original plan was a server-generated unique reference.
  In a stateless server (no database, rule 2), a generated reference couldn't be found again
  from "did Ahmed pay for order 42?" without storing a mapping. Options considered:
  - *Random generated reference:* needs a database. Rejected (rule 2).
  - *Deterministic hash of merchant + order_id:* stateless, but it hides the merchant's own order
    number in Fawry's dashboard and reports, which makes support and reconciliation harder.
    Rejected.
  - *`order_id` as `merchantRefNum`, plus our own duplicate check (chosen).* Before charging,
    `create_cash_payment` looks the order up. If Fawry already has a payment for it, the tool
    refuses and names the existing reference. So duplicate protection doesn't depend on how
    Fawry treats a repeated `merchantRefNum` (V8, unverified). Two creates racing each other can
    both pass the check, and Fawry's uniqueness is the backstop for that rare case. The cost
    is one extra status call per create, which is cheap next to a double charge. Tested:
    `test_duplicate_order_id_is_refused_before_charging` proves Fawry never sees the second
    charge.

### MCP Inspector and pip-audit (run 2026-10-31)
- **Inspector pinned to 2.11.0**, past the CVE-2025-49596 fix (0.14.1) and more than two weeks
  old, so a bad release has had time to be noticed (2.13.0 was 3 days old). The full flow ran in
  CLI mode through `scripts/inspector_cli.sh`; results are in `docs/inspector.md`.
- **`pip-audit`: the project's dependencies have no known vulnerabilities.** The only findings
  were in `pip` 24.0 itself, the copy bundled into the venv by the system Python, and upgrading
  pip in the venv cleared them. CI (milestone 3) runs `pip-audit` on every PR and should
  upgrade pip first, or it will report the same false alarm about the tool rather than the app.

### Logging check (rule 3)
I ran the stdio demo with server logs at INFO/DEBUG and grepped for the key, the customer's mobile
and email, and anything shaped like a 64-hex signature. That **found a real leak**: `httpx2` logs
each request URL at INFO, and the status v2 URL carries `?signature=…`. The redacting filter missed
it because `httpx2` passes the URL as a `URL` object, not a `str`. Fixed twice over: the filter now
redacts any non-numeric argument through its string form (with a regression test using a real
`httpx2.URL`), and `httpx2`/`httpcore2` loggers are set to WARNING. A re-run found zero leaks. Lesson
for interviews: redaction needs an end-to-end check against real log output, not just unit tests.

### Not done in milestone 2 (on purpose)
- **HTTP transport and per-request credentials from headers:** milestone 3.
- **Switching the Fawry client and server to `core/provider.py`'s `PaymentProvider`
  interface:** the interface exists, but the server still calls `FawryClient` directly. That's
  a pure refactor with no behaviour change, so it's kept as its own PR to keep this one
  reviewable.

## Milestone 1: mock, signatures, tests

### Dependencies outside the stack table (awaiting director approval)
| Package | Used for | Why it's needed |
|---|---|---|
| `uvicorn` | Running the FastAPI mock locally (`python -m mock_fawry`) | FastAPI is only a framework; something has to serve HTTP. Uvicorn is the server FastAPI documents. On Lambda (milestone 3) it won't be used. |
| ~~`httpx`~~ (dev only) | FastAPI's `TestClient` | Replaced by `httpx2` in milestone 2 (see above). |
| `ruff` (dev only) | Lint and format | One fast tool in place of flake8 + isort + black; the "lint" step in CI. Enables the `S` (bandit) rules so things like hard-coded secrets get flagged. |
| `hatchling` (build only) | Packaging from `pyproject.toml` | Standard, no-config build backend. |

Starlette warned that `TestClient` prefers `httpx2` over `httpx`; settled in milestone 2 by
switching to `httpx2`.

### Signatures
- **One function per recipe, each split into `*_plain` + hash.** Most signature bugs are
  field-order bugs. Testing the plain string separately tells us *which* field is wrong
  instead of just "hash mismatch".
- **Test vectors are computed outside our code.** Every hash in `tests/vectors.json` came from
  `printf '%s' "$plain" | sha256sum`. If the vectors were generated with `signatures.py`, the
  tests would only check that the code agrees with itself. Fawry's doc sample signatures
  aren't used because they're placeholders, not hashes of the sample inputs: the same value
  appears on pages with different inputs (see docs/design.md, "Known traps"). The documented formula
  hasn't been shown to be wrong. Re-check against real sandbox responses once we have credentials.
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
| V10 | Fawry's default expiry when `paymentExpiry` is omitted (the mock: never expires). Our tool always sends one (default 72 h). | `state._apply_expiry` |
| V11 | The status response has `paymentTime` in epoch milliseconds and `refundedAmount` after a refund | `client._payment_from` |
| V12 | Status names beyond the documented ones (`CANCELLED` spelling, `PARTIALLY_REFUNDED`) are mapped defensively; anything else becomes `unknown`, with the raw value kept | `client.NORMALIZED_STATUS` |
| V13 | Fawry accepts amounts as JSON numbers (we send `150.0`, not `"150.00"`) | `client.create_pay_at_fawry_charge` |
