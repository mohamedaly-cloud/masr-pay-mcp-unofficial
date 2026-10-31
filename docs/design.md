# Design

What this server is, the rules it never breaks, and the Fawry API facts it's built on. The
reasons behind individual choices are in [decisions.md](decisions.md).

## What it is
An **unofficial MCP server for FawryPay** that lets any MCP-capable AI assistant collect payments in Egypt, with a focus on **Pay-at-Fawry cash payments**: the agent creates a reference number, the customer pays cash at any Fawry outlet, the agent checks whether it was paid.

Example conversation the product must support:
> "Charge Ahmed 350 EGP for order 42, he'll pay cash." → reference number returned
> "Did Ahmed pay?" → status checked → "Yes, paid at 14:32"

Positioning (checked 2026-10-31):
- Audience: merchants contracted **directly with FawryPay** and the developers/agencies who build sales bots for them.
- Paymob offers its own cash-at-kiosk method, and Paymob's own llms.txt names those kiosk networks as **Aman and Masary**, not Fawry. Its MCP has no dedicated kiosk tool. Do not claim we are the only way an agent can take cash payments in Egypt; claim we are the MCP server for FawryPay's own API and the Fawry outlet network.
- Paymob lists InstaPay only as a settlement/payout rail, not a checkout method (llms.txt, checked 2026-10-31).

Why it exists: Paymob (Fawry's main competitor) ships an official MCP server; Fawry has none publicly.

## Rules
1. **Name and branding:** always "unofficial". No Fawry logo, no wording implying affiliation or endorsement.
2. **Never store credentials or funds.** Stateless: merchant code and secure key arrive per request (headers/env in local mode), are used, and are discarded. No database.
3. **Never log secrets.** Redact `secureKey`, signatures, customer email and mobile in all logs.
4. **Refunds and cancellations require human confirmation** (mark these tools as destructive; describe the confirmation requirement in the tool description).
5. **Never use credentials found in public repos.** We develop against our own mock until official sandbox credentials are issued by FawryPay.
6. **Spec fidelity:** endpoint paths and signature recipes come from Fawry's official docs. Where only third-party sources exist, the code says so in a comment and the item stays on the "verify" list below.

## Stack
| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | One language end to end; matches AWS Lambda tooling |
| MCP | Official MCP Python SDK v2.x (spec 2026-07-28). FastMCP acceptable only if it supports the 2026-07-28 spec (check before adopting) | Spec removed sessions/handshake → stateless fits Lambda |
| Mock Fawry API | FastAPI app (`mock_fawry/`) | Prism cannot verify signatures or simulate kiosk payments |
| Tests | pytest (+ in-memory MCP client tests) | Fast, deterministic |
| Debugging | MCP Inspector (`npx -y @modelcontextprotocol/inspector`) | See raw JSON-RPC |
| Deploy | AWS Lambda + API Gateway (Streamable HTTP), AWS SAM | Serverless, cheap at low traffic, no servers to run |
| CI/CD | GitHub Actions: lint, test, `sam deploy` on main | |
| Listing | `mcp-publisher` to the official MCP Registry (milestone 4) | |

## Architecture: provider-neutral core
The repo may hold more than one Egyptian payment provider: Fawry now; InstaPay via Kashier later, once Kashier launches it (support confirmed on 2026-10-31 it is not launched yet). Build the core so a new provider is a new folder, not a rewrite.
- `core/` owns everything provider-neutral: the `PaymentProvider` interface (create_payment, get_status, cancel, refund), normalized status enum (`AWAITING_PAYMENT`, `PAID`, `EXPIRED`, `CANCELED`, `REFUNDED`, `FAILED`) with raw provider status passed through, confirmation tokens, audit logging, guardrails, redaction.
- `providers/fawry/` implements the interface for FawryPay. Later `providers/kashier/` (InstaPay request-to-pay and QR).
- MCP tool names stay provider-neutral where possible and take a `provider` argument only once there are two providers. v1 exposes Fawry only.
- The repo name is provider-neutral and includes "unofficial" (`egypt-payments-mcp` is taken by a Paymob-based server).

## Fawry API reference
Base URLs: staging `https://atfawry.fawrystaging.com`, production `https://www.atfawry.com`.
All signatures: SHA-256, hex-encoded, of the plain concatenation (no separators). Amounts in signatures are formatted with exactly two decimals (`580.55`, `20.00`).

Confidence labels: **[official]** read directly on developer.fawrystaging.com · **[secondary]** consistent across multiple third-party SDKs/specs, official page not machine-readable · **[third-party]** single third-party source, must verify.

### 1. Create Pay-at-Fawry charge [official]
`POST /ECommerceWeb/Fawry/payments/charge`
- Required: `merchantCode`, `merchantRefNum`, `paymentMethod` = `"PayAtFawry"`, `customerMobile` (`01xxxxxxxxx`), `customerEmail`, `amount` (xx.xx), `description`, `language` (`"ar-eg"` | `"en-gb"`), `chargeItems[]` (`itemId`, `description`, `price`, `quantity`), `signature`
- Optional: `customerProfileId`, `customerName`, `paymentExpiry` (epoch **milliseconds**), `orderWebHookUrl`, `currencyCode` (`"EGP"`)
- **Request signature:** `merchantCode + merchantRefNum + customerProfileId + paymentMethod + amount(2dp) + secureKey` (use `""` when `customerProfileId` is absent)
- Response fields: `type` ("ChargeResponse"), `referenceNumber`, `merchantRefNumber`, `orderAmount`, `paymentAmount`, `fawryFees`, `paymentMethod`, `orderStatus`, `paymentTime`, `customerMobile`, `customerMail`, `customerProfileId`, `signature`, `statusCode`, `statusDescription`
- **Response signature (verify it in our client):** `referenceNumber (if exists) + merchantRefNum + paymentAmount(2dp) + orderAmount(2dp) + orderStatus + paymentMethod + fawryFees(2dp, if exists) + shippingFees(2dp, if exists) + authNumber (if exists) + customerMail (if exists) + customerMobile (if exists) + secureKey`

### 2. Get payment status v2 [secondary]
`GET /ECommerceWeb/Fawry/payments/status/v2?merchantCode=…&merchantRefNumber=…&signature=…`
- **Signature:** `merchantCode + merchantRefNumber + secureKey`
- Response includes `requestId`, `fawryRefNumber`, `merchantRefNumber`, `paymentAmount`, `orderAmount`, `fawryFees`, `orderStatus`, `paymentMethod`, customer fields
- Note the field name differences: request uses `merchantRefNumber` here but `merchantRefNum` in charge; status response uses `fawryRefNumber` while charge uses `referenceNumber`. Normalize in `client.py`.

### 3. Refund (endpoint and fields [official], signature [third-party])
`POST /ECommerceWeb/Fawry/payments/refund`
- Fields: `merchantCode`, `referenceNumber` (Fawry's reference), `refundAmount`, `reason`, `signature`
- **Signature:** `merchantCode + referenceNumber + refundAmount(2dp) + reason + secureKey`
- Checkout.com reports that Fawry allows only one refund action per payment and rounds refunds down to whole EGP. Verify; until verified, the mock enforces "one refund per order" and the tool description warns about it.

### 4. Cancel unpaid order [third-party]
`POST /ECommerceWeb/api/orders/cancel-unpaid-order`
- Fields: `merchantAccount`, `orderRefNo`, `lang` (`"ar-eg"` | `"en-gb"`), `signature`
- **Signature:** `orderRefNo + merchantAccount + lang + secureKey`

### Order statuses
Seen across sources: `NEW`, `UNPAID`, `PENDING`, `PAID`, `EXPIRED`, `CANCELED`, `REFUNDED`, `FAILED`. Sources disagree (the official charge sample shows `PENDING`, status samples show `NEW`/`UNPAID`). Treat `NEW`/`UNPAID`/`PENDING` as "awaiting payment" in the MCP layer; pass the raw value through as well.

### Error codes [official]
| Code | Meaning |
|---|---|
| 200 | Operation done successfully |
| 9901 | Merchant code blank or invalid |
| 9938 | Order not found |
| 9946 | Blank or invalid signature |
| 9935 | Refunded amount greater than paid amount |
| 9954 | Order is not paid |

### Known traps in Fawry's own docs
- The sample signatures on the docs pages are **placeholders, not real hashes of the sample inputs**. The same value `3f527d02…5131` appears on the charge, status and refund pages, which have different inputs, so it cannot be a computed signature for all of them. Tested 291 recipe variants (both merchantRefNum values shown, with/without customerProfileId, method casing, amount formats, with/without key, duplicated merchantCode, status and refund recipes): zero matches. The documented *formula* is not shown to be wrong; only the sample values are unusable. **Do not use doc sample signatures as test vectors.** Generate our own vectors from the recipe and document them in `tests/vectors.json`. Re-check against real sandbox responses once credentials exist.
- Bugs in the docs' sample code (verified by reading the snippets):
  - JavaScript: `merchantCode.concat(merchantCode, …)` puts `merchantCode` in the string twice; it also creates a `jsSHA` object without ever feeding it the string, and imports `js-sha256` but uses `jsSHA`. Do not copy it.
  - Python: `hashlib.sha256(<str>)` raises `TypeError` in Python 3 (always `.encode("utf-8")`), and a `//` comment in the same snippet is a `SyntaxError`.

## Security and trust features
1. **Two-step confirmation for destructive tools (stateless).** `cancel_unpaid_payment` and `refund_payment` never execute on the first call. First call returns a preview (what will happen, amount, order) plus a `confirmation_token`: an HMAC-signed token encoding tool name, arguments hash and expiry (5 minutes), signed with a server secret. Second call must pass the token; the server verifies signature, expiry and that arguments match. No database needed. Document the design in `docs/decisions.md`.
2. **Audit log of every tool call.** Structured JSON log line per call: timestamp, tool, merchantRefNum, amount, outcome, Fawry status code, request id. Redact secrets and customer PII per rule 3. Locally write to **stderr only** (stdout carries MCP JSON-RPC on stdio); on Lambda, CloudWatch Logs. No database.
3. **Guardrails.** Configurable max amount per payment (default 50,000 EGP); refunds can never exceed the paid amount; `merchantRefNum` is the merchant's `order_id` (decided 2026-10-31, see docs/decisions.md) and the server refuses a create when that order already has a payment, to prevent duplicate charges; verify Fawry response signatures and refuse to report "PAID" if verification fails.
4. **Threat model** in `docs/threat-model.md`: prompt injection via customer chat ("refund me now"), tool poisoning, credential leakage, replayed confirmation tokens, spoofed status responses. One mitigation per threat, mapped to code.
5. **Supply-chain hygiene in CI:** `pip-audit` on every PR; pin all dependency versions; pin MCP Inspector to a version patched for CVE-2025-49596.

## MCP tools
| Tool | Purpose | Notes |
|---|---|---|
| `create_cash_payment` | Create Pay-at-Fawry reference | Inputs: amount, order id, customer mobile/email, description, expiry hours (default 72), language |
| `get_payment_status` | Check one order | Normalized status + raw status |
| `cancel_unpaid_payment` | Cancel before payment | Destructive, needs confirmation |
| `refund_payment` | Refund a paid order | Destructive, needs confirmation, one refund per order |

Tool descriptions are written for the model: say what the tool does, when to use it, units (EGP), and failure meanings.

## Mock server (`mock_fawry/`)
- Implements endpoints 1–4 with the exact paths above, validates signatures with a configurable `MOCK_SECURE_KEY`, returns Fawry-shaped JSON including error codes.
- Charge returns a 9-digit `referenceNumber`, status `UNPAID`, honors `paymentExpiry`.
- Status reflects state transitions: `UNPAID → PAID` (via admin), `UNPAID → EXPIRED` (time passes `paymentExpiry`), `UNPAID → CANCELED`, `PAID → REFUNDED`.
- Admin endpoints (mock only, prefixed `/__admin`): `POST /__admin/kiosk-pay/{referenceNumber}` simulates a customer paying cash at a Fawry outlet; `POST /__admin/reset`; `GET /__admin/orders`.
- Signs its responses with the official response-signature recipe so our client's verification is exercised.

## Milestones
1. **Core + Fawry signatures + mock + tests**: `core/provider.py` interface and status enum; `providers/fawry/signatures.py` with unit tests and our own vectors; FastAPI mock with admin kiosk-pay; pytest green.
2. **MCP server locally**: four tools over stdio, with two-step confirmation, audit log and guardrails from "Security and trust features"; `docs/threat-model.md`; MCP Inspector walkthrough; demo script: create 150 EGP reference for order 42 → kiosk-pay → "has it been paid?" → yes → refund attempt shows preview and requires confirmation.
3. **AWS deploy**: SAM template, Lambda + API Gateway Streamable HTTP, GitHub Actions CI/CD with `pip-audit`, CloudWatch audit logs, API Gateway throttling, least-privilege IAM, mock deployed alongside for public demos, one-line monthly cost estimate in the README.
4. **Launch**: README with demo GIF, `docs/openapi.yaml`, `docs/decisions.md`, publish to MCP Registry, then email FawryPay developer support with the repo link to request sandbox access.

## Roadmap
- **InstaPay via Kashier: on hold.** Kashier support confirmed on 2026-10-31 that InstaPay is documented but **not yet launched or activated** by Kashier. Do not build against it until Kashier announces launch and test access is confirmed. Alternatives checked 2026-10-31: Paymob (InstaPay for payouts only), PayTabs, noon payments, Fawry, Pay Sky and Egyptian gateway guides show no InstaPay checkout; A-Pay claims "direct IPN connectivity" with a sandbox but is listed as India-headquartered, high-risk/crypto-friendly, with no named licence, so don't use it.
- **Payment-received notifications:** built and parked on the `notifications` branch; see decisions.md.
- **Merchant sign-in (OAuth):** not planned for v1; see decisions.md.

## Conventions
- Small PRs, one milestone per branch, conventional commits.
- Every PR description ends with a "Why" section in plain English.
- New dependencies outside the stack table need a written reason in `docs/decisions.md`.

## Open questions
- Exact v2 package/import names for the official Python SDK (check its README).
- Whether FawryPay will issue sandbox credentials to a developer outside Egypt.
- Server-to-server notification v2 (webhook) payload and signature (needed only if notifications ship).
