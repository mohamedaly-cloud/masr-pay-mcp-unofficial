"""MCP tools for Pay-at-Fawry cash payments (unofficial; not affiliated with Fawry).

Four tools: create a cash payment reference, check its status, cancel it
before it's paid, refund it after it's paid. Descriptions are written for the
model: what the tool does, when to use it, units (EGP), and what failures mean.

Statelessness: every call loads credentials fresh (``config.load_config``)
and builds a new ``FawryClient``. Nothing survives between calls.
"""

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field

from masr_pay_mcp import __version__
from masr_pay_mcp.providers.fawry.client import (
    FawryClient,
    FawryError,
    FawryUnavailable,
    NormalizedStatus,
    Payment,
    ResponseSignatureError,
)
from masr_pay_mcp.providers.fawry.config import ConfigError, FawryConfig, load_config
from masr_pay_mcp.providers.fawry.signatures import format_amount

log = logging.getLogger(__name__)

try:
    CAIRO: ZoneInfo | None = ZoneInfo("Africa/Cairo")
except ZoneInfoNotFoundError:  # no tz database (some minimal containers): fall back to UTC only
    CAIRO = None

SERVER_INSTRUCTIONS = """\
Unofficial MCP server for FawryPay "Pay at Fawry" cash payments in Egypt (not affiliated with Fawry).

Typical flow:
1. create_cash_payment: get a 9-digit Fawry reference number for an order. Give the customer the
   reference number, the total to pay in EGP and the deadline. They pay cash at any Fawry outlet.
2. get_payment_status: check whether the customer has paid. Look it up by the same order_id.
3. cancel_unpaid_payment / refund_payment: destructive. Only after the human explicitly confirms.

All amounts are Egyptian pounds (EGP) with at most two decimals. The order_id is the merchant's own
order number and must be unique per payment.
"""


# ----------------------------------------------------------------- input types
def _normalize_mobile(value: object) -> object:
    """Accept common spellings of an Egyptian mobile and convert to 01xxxxxxxxx."""
    if not isinstance(value, str):
        return value
    digits = re.sub(r"[\s\-().]", "", value)
    for prefix in ("+20", "0020"):
        if digits.startswith(prefix):
            digits = "0" + digits[len(prefix) :]
    if digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits


def _two_decimals(value: Decimal) -> Decimal:
    return Decimal(format_amount(value))  # raises ValueError for >2 decimals


OrderId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=50,
        pattern=r"^[A-Za-z0-9._-]+$",
        description=(
            "The merchant's own order number, e.g. '42' or 'ORD-2026-0042' (letters, digits, "
            ". _ - only). It identifies the payment in every later call."
        ),
    ),
]
Mobile = Annotated[
    str,
    BeforeValidator(_normalize_mobile),
    Field(
        pattern=r"^01[0125]\d{8}$",
        description="Customer's Egyptian mobile, e.g. 01012345678 (+20 forms are accepted).",
    ),
]
AmountEGP = Annotated[
    Decimal,
    Field(
        gt=0,
        description="Amount in Egyptian pounds (EGP), at most two decimals, e.g. 350 or 99.50.",
    ),
    AfterValidator(_two_decimals),
]
LanguageCode = Annotated[
    Literal["en-gb", "ar-eg"],
    Field(description="Language for Fawry's customer-facing messages: 'en-gb' or 'ar-eg'."),
]
ConfirmedByUser = Annotated[
    bool,
    Field(
        description=(
            "Set to true ONLY if the human user explicitly confirmed this exact action in the "
            "conversation just now. Never set it on your own initiative."
        )
    ),
]


# ----------------------------------------------------------------- output models
class CashPaymentCreated(BaseModel):
    order_id: str
    reference_number: str = Field(description="The 9-digit number the customer pays with at Fawry.")
    status: NormalizedStatus
    raw_status: str = Field(description="Status exactly as Fawry returned it.")
    amount_egp: str
    fawry_fees_egp: str | None
    total_to_pay_egp: str = Field(
        description="What the customer pays at the outlet (amount + fees)."
    )
    expires_at_utc: str | None
    expires_at_cairo: str | None
    customer_instructions: str = Field(description="A ready-to-send message for the customer.")
    environment: str = Field(
        description="'mock', 'staging' or 'production'. Only production is real money."
    )


class PaymentStatus(BaseModel):
    order_id: str
    reference_number: str | None
    status: NormalizedStatus = Field(
        description="awaiting_payment | paid | expired | canceled | refunded | failed | unknown"
    )
    raw_status: str = Field(description="Status exactly as Fawry returned it.")
    is_paid: bool
    paid_at_utc: str | None
    paid_at_cairo: str | None
    amount_egp: str
    total_to_pay_egp: str
    fawry_fees_egp: str | None
    refunded_amount_egp: str | None
    summary: str = Field(description="One-sentence plain-English answer.")
    environment: str


class CancelResult(BaseModel):
    order_id: str
    reference_number: str
    status: NormalizedStatus
    raw_status: str
    summary: str
    confirmation: str = Field(description="How the human's confirmation was obtained.")
    environment: str


class RefundResult(BaseModel):
    order_id: str
    reference_number: str
    refunded_amount_egp: str
    status: NormalizedStatus
    raw_status: str
    summary: str
    confirmation: str = Field(description="How the human's confirmation was obtained.")
    environment: str


class _Confirmation(BaseModel):
    confirm: bool = Field(description="Yes, go ahead")


class _Plan(BaseModel):
    """What a destructive tool will act on, worked out (and checked) before asking the human."""

    reference_number: str
    payment_amount: Decimal
    amount: Decimal  # refund: the amount to refund; cancel: the amount that won't be collected


# ----------------------------------------------------------------- helpers
def _egp(value: Decimal | None) -> str | None:
    return None if value is None else format_amount(value)


def _iso_utc(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat(timespec="seconds")


def _iso_cairo(moment: datetime | None) -> str | None:
    if moment is None or CAIRO is None:
        return None
    return moment.astimezone(CAIRO).isoformat(timespec="seconds")


def _human_cairo(moment: datetime) -> str:
    if CAIRO is None:
        return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return moment.astimezone(CAIRO).strftime("%Y-%m-%d %H:%M (Cairo time)")


@contextmanager
def _fawry_errors(order_id: str, *, not_found: str | None = None) -> Iterator[None]:
    """Turn every anticipated failure into a ToolError the model can explain to the user."""
    try:
        yield
    except ConfigError as exc:
        raise ToolError(f"The payment server isn't configured: {exc}") from exc
    except FawryError as exc:
        if exc.status_code == 9938 and not_found:
            raise ToolError(not_found) from exc
        raise ToolError(
            f"Fawry refused the request (code {exc.status_code}): {exc.meaning}"
        ) from exc
    except ResponseSignatureError as exc:
        log.warning("order_id=%s: response signature check failed", order_id)
        raise ToolError(
            f"{exc} The answer can't be trusted, so it was ignored. Try again later; if it keeps "
            "happening, the server's FAWRY_SECURE_KEY may not match the merchant account."
        ) from exc
    except FawryUnavailable as exc:
        raise ToolError(
            f"{exc} Nothing was confirmed; it's safe to check the status again."
        ) from exc


def _require_flag(confirmed_by_user: bool, action: str) -> None:
    if not confirmed_by_user:
        raise ToolError(
            f"Not done: {action} needs explicit confirmation. Ask the user to confirm, then call "
            "again with confirmed_by_user=true. Nothing was changed."
        )


def _ask_human(ctx: Context, message: str) -> _Confirmation | Elicit[_Confirmation]:
    """Ask the human directly (MCP elicitation) when the client supports it.

    Third of three confirmation layers for destructive tools, after the host's
    approval prompt (destructiveHint) and the model-attested
    ``confirmed_by_user`` flag. The SDK delivers the question in whatever form
    the negotiated protocol uses: an ``InputRequiredResult`` round on
    2026-07-28, a mid-call ``elicitation/create`` request on older revisions.
    If the client can't elicit, the first two layers stand alone.
    """
    if not _client_can_elicit(ctx):
        return _Confirmation(confirm=True)
    return Elicit(message, _Confirmation)


def _client_can_elicit(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    return caps is not None and caps.elicitation is not None


def _check_human_answer(answer: ElicitationResult[_Confirmation]) -> None:
    if answer.action != "accept" or not answer.data.confirm:
        raise ToolError("The user did not confirm, so nothing was changed.")


def _confirmation_method(ctx: Context) -> str:
    return "asked the user directly" if _client_can_elicit(ctx) else "confirmed_by_user flag"


def _status_summary(order_id: str, p: Payment) -> str:
    total = format_amount(p.payment_amount)
    match p.status:
        case "paid":
            when = f" on {_human_cairo(p.paid_at)}" if p.paid_at else ""
            return f"Order {order_id} is paid: {total} EGP received{when}."
        case "awaiting_payment":
            return (
                f"Order {order_id} is not paid yet. The customer still needs to pay {total} EGP at "
                f"a Fawry outlet with reference {p.reference_number}."
            )
        case "expired":
            return f"Order {order_id} expired unpaid. Create a new payment if the customer still wants to pay."
        case "canceled":
            return f"Order {order_id} was cancelled before it was paid."
        case "refunded":
            amount = f"{format_amount(p.refunded_amount)} EGP " if p.refunded_amount else ""
            return f"Order {order_id} was paid and then refunded {amount}".rstrip() + "."
        case "failed":
            return f"Order {order_id} failed at Fawry."
        case _:
            return f"Order {order_id} has a status this server doesn't recognise: {p.raw_status!r}."


# ----------------------------------------------------------------- server
def create_server(
    *,
    config_loader: Callable[[], FawryConfig] = load_config,
    transport: httpx2.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MCPServer:
    """Build the MCP server. The keyword arguments exist so tests can point it at the mock."""
    mcp = MCPServer(
        name="masr-pay-mcp-unofficial",
        title="Egyptian payments: FawryPay cash (unofficial)",
        description="Unofficial MCP server for Egyptian payment providers, starting with FawryPay Pay-at-Fawry. Not affiliated with Fawry.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )

    def client_and_env() -> tuple[FawryClient, str]:
        config = config_loader()  # fresh every call: no credentials kept between calls
        return FawryClient(
            config.base_url, config.credentials, transport=transport
        ), config.environment

    # ------------------------------------------------------------ create
    @mcp.tool(
        title="Create a Pay-at-Fawry cash payment",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def create_cash_payment(
        amount_egp: AmountEGP,
        order_id: OrderId,
        customer_mobile: Mobile,
        customer_email: Annotated[
            str,
            Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", description="Customer's email address."),
        ],
        description: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="What the payment is for, e.g. 'Order 42'.",
            ),
        ],
        customer_name: Annotated[
            str | None, Field(max_length=100, description="Optional customer name.")
        ] = None,
        expiry_hours: Annotated[
            float,
            Field(gt=0, le=720, description="Hours the customer has to pay (default 72, max 720)."),
        ] = 72,
        language: LanguageCode = "en-gb",
    ) -> CashPaymentCreated:
        """Create a Fawry reference number so a customer can pay an order in CASH at any Fawry outlet in Egypt.

        Use it when the user wants to charge someone who will pay cash (e.g. "charge Ahmed 350 EGP for
        order 42, he'll pay cash"). Returns a 9-digit reference number, the total the customer pays
        (order amount plus any Fawry fees, in EGP) and the deadline; pass these on to the customer.
        Nothing is charged until the customer pays at the outlet.

        Each order_id can be used for only one payment. Failures: a rejected merchant code or signature
        means the server's credentials are wrong (not the customer's fault); a duplicate order_id means
        that order already has a payment, so use get_payment_status instead.
        """
        expires_at = now() + timedelta(hours=expiry_hours)
        with _fawry_errors(order_id):
            client, environment = client_and_env()
            payment = await client.create_pay_at_fawry_charge(
                merchant_ref_num=order_id,
                amount=amount_egp,
                customer_mobile=customer_mobile,
                customer_email=customer_email,
                description=description,
                language=language,
                customer_name=customer_name,
                payment_expiry=expires_at,
            )
        log.info("create_cash_payment order_id=%s -> %s", order_id, payment.raw_status)
        total = format_amount(payment.payment_amount)
        return CashPaymentCreated(
            order_id=order_id,
            reference_number=payment.reference_number or "",
            status=payment.status,
            raw_status=payment.raw_status,
            amount_egp=format_amount(payment.order_amount),
            fawry_fees_egp=_egp(payment.fawry_fees),
            total_to_pay_egp=total,
            expires_at_utc=_iso_utc(expires_at),
            expires_at_cairo=_iso_cairo(expires_at),
            customer_instructions=(
                f"Please pay {total} EGP in cash at any Fawry outlet using reference number "
                f"{payment.reference_number}, before {_human_cairo(expires_at)}."
            ),
            environment=environment,
        )

    # ------------------------------------------------------------ status
    @mcp.tool(
        title="Check a Pay-at-Fawry payment",
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    async def get_payment_status(order_id: OrderId) -> PaymentStatus:
        """Check whether the customer has paid an order (e.g. "did Ahmed pay for order 42?").

        Look up by the same order_id used in create_cash_payment. Returns a normalized status
        (awaiting_payment, paid, expired, canceled, refunded, failed or unknown), the raw Fawry status,
        when it was paid, and amounts in EGP. Safe to call as often as needed; it changes nothing.
        "Order not found" means no payment was ever created for that order_id.
        """
        with _fawry_errors(
            order_id,
            not_found=f"No Fawry payment exists for order {order_id}. Check the order id, or create one.",
        ):
            client, environment = client_and_env()
            p = await client.get_payment_status(order_id)
        log.info("get_payment_status order_id=%s -> %s", order_id, p.raw_status)
        return PaymentStatus(
            order_id=order_id,
            reference_number=p.reference_number,
            status=p.status,
            raw_status=p.raw_status,
            is_paid=p.is_paid,
            paid_at_utc=_iso_utc(p.paid_at),
            paid_at_cairo=_iso_cairo(p.paid_at),
            amount_egp=format_amount(p.order_amount),
            total_to_pay_egp=format_amount(p.payment_amount),
            fawry_fees_egp=_egp(p.fawry_fees),
            refunded_amount_egp=_egp(p.refunded_amount),
            summary=_status_summary(order_id, p),
            environment=environment,
        )

    # ------------------------------------------------------------ cancel
    # Resolvers run before the tool body: plan (flag + status check) -> ask the human.
    # On 2026-07-28 they may run again on the retry round; both are read-only.
    async def cancel_plan(order_id: str, confirmed_by_user: bool) -> _Plan:
        _require_flag(confirmed_by_user, "cancelling a payment")
        with _fawry_errors(
            order_id,
            not_found=f"No Fawry payment exists for order {order_id}, so there's nothing to cancel.",
        ):
            client, _ = client_and_env()
            before = await client.get_payment_status(order_id)
        if not before.is_awaiting_payment:
            hint = " Use refund_payment instead." if before.is_paid else ""
            raise ToolError(
                f"Order {order_id} can't be cancelled: its status is {before.status} "
                f"({before.raw_status}).{hint} Nothing was changed."
            )
        return _Plan(
            reference_number=before.reference_number or "",
            payment_amount=before.payment_amount,
            amount=before.payment_amount,
        )

    async def cancel_question(
        order_id: str, plan: Annotated[_Plan, Resolve(cancel_plan)], ctx: Context
    ) -> _Confirmation | Elicit[_Confirmation]:
        return _ask_human(
            ctx,
            f"Cancel the unpaid Fawry payment for order {order_id} (reference {plan.reference_number}, "
            f"{format_amount(plan.payment_amount)} EGP)? The customer won't be able to pay with this "
            "reference any more.",
        )

    @mcp.tool(
        title="Cancel an unpaid Pay-at-Fawry payment (destructive)",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
        ),
    )
    async def cancel_unpaid_payment(
        order_id: OrderId,
        plan: Annotated[_Plan, Resolve(cancel_plan)],
        answer: Annotated[ElicitationResult[_Confirmation], Resolve(cancel_question)],
        ctx: Context,
        confirmed_by_user: ConfirmedByUser = False,
        language: LanguageCode = "en-gb",
    ) -> CancelResult:
        """Cancel a payment that has NOT been paid yet, so its reference number can no longer be used.

        DESTRUCTIVE: REQUIRES HUMAN CONFIRMATION. Before calling, tell the user which order and
        reference will be cancelled and get an explicit "yes"; then call with confirmed_by_user=true.
        The server may also ask the user directly. It can't be undone: a new payment would need a
        new order_id.

        Only works while the status is awaiting_payment. A paid order must be refunded instead
        (refund_payment); an expired or cancelled one needs nothing.
        """
        _check_human_answer(answer)
        with _fawry_errors(order_id):
            client, environment = client_and_env()
            await client.cancel_unpaid(reference_number=plan.reference_number, language=language)
            after = await client.get_payment_status(order_id)
        log.info("cancel_unpaid_payment order_id=%s -> %s", order_id, after.raw_status)
        return CancelResult(
            order_id=order_id,
            reference_number=plan.reference_number,
            status=after.status,
            raw_status=after.raw_status,
            summary=f"Order {order_id} is now {after.status}; reference {plan.reference_number} can't be paid.",
            confirmation=_confirmation_method(ctx),
            environment=environment,
        )

    # ------------------------------------------------------------ refund
    async def refund_plan(
        order_id: str, confirmed_by_user: bool, amount_egp: Decimal | None
    ) -> _Plan:
        _require_flag(confirmed_by_user, "a refund")
        with _fawry_errors(
            order_id,
            not_found=f"No Fawry payment exists for order {order_id}, so there's nothing to refund.",
        ):
            client, _ = client_and_env()
            before = await client.get_payment_status(order_id)
        if before.status == "refunded":
            raise ToolError(
                f"Order {order_id} was already refunded; Fawry allows one refund per payment. "
                "Nothing was changed."
            )
        if not before.is_paid:
            hint = " Use cancel_unpaid_payment instead." if before.is_awaiting_payment else ""
            raise ToolError(
                f"Order {order_id} can't be refunded: it isn't paid (status {before.status}).{hint} "
                "Nothing was changed."
            )
        amount = amount_egp if amount_egp is not None else before.order_amount
        if amount > before.order_amount:
            raise ToolError(
                f"Can't refund {format_amount(amount)} EGP: the order amount is "
                f"{format_amount(before.order_amount)} EGP. Nothing was changed."
            )
        return _Plan(
            reference_number=before.reference_number or "",
            payment_amount=before.payment_amount,
            amount=amount,
        )

    async def refund_question(
        order_id: str, reason: str, plan: Annotated[_Plan, Resolve(refund_plan)], ctx: Context
    ) -> _Confirmation | Elicit[_Confirmation]:
        return _ask_human(
            ctx,
            f"Refund {format_amount(plan.amount)} EGP for order {order_id} (reference "
            f"{plan.reference_number}), reason: {reason}? Fawry allows only one refund per payment.",
        )

    @mcp.tool(
        title="Refund a paid Pay-at-Fawry payment (destructive)",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
        ),
    )
    async def refund_payment(
        order_id: OrderId,
        reason: Annotated[
            str, Field(min_length=1, max_length=200, description="Why the money is returned.")
        ],
        plan: Annotated[_Plan, Resolve(refund_plan)],
        answer: Annotated[ElicitationResult[_Confirmation], Resolve(refund_question)],
        ctx: Context,
        confirmed_by_user: ConfirmedByUser = False,
        amount_egp: Annotated[
            AmountEGP | None,
            Field(
                description="Amount to refund in EGP. Leave empty to refund the full order amount."
            ),
        ] = None,
    ) -> RefundResult:
        """Refund money for an order the customer has ALREADY PAID.

        DESTRUCTIVE: REQUIRES HUMAN CONFIRMATION. Before calling, tell the user the order, the amount in
        EGP and the reason, and get an explicit "yes"; then call with confirmed_by_user=true. The server
        may also ask the user directly.

        Fawry is reported to allow only ONE refund per payment, so a partial refund uses up the only
        refund. It's also reported to round refunds down to whole EGP. Both are unconfirmed; warn the
        user. Fees aren't refunded: the maximum is the order amount. Fails if the order isn't paid (use
        cancel_unpaid_payment for an unpaid one) or was already refunded.
        """
        _check_human_answer(answer)
        with _fawry_errors(order_id):
            client, environment = client_and_env()
            await client.refund(
                reference_number=plan.reference_number, amount=plan.amount, reason=reason
            )
            after = await client.get_payment_status(order_id)
        log.info("refund_payment order_id=%s -> %s", order_id, after.raw_status)
        amount = plan.amount
        rounding_note = (
            " Fawry may round it down to whole EGP." if amount != amount.to_integral_value() else ""
        )
        return RefundResult(
            order_id=order_id,
            reference_number=plan.reference_number,
            refunded_amount_egp=format_amount(amount),
            status=after.status,
            raw_status=after.raw_status,
            summary=f"Refund of {format_amount(amount)} EGP for order {order_id} accepted by Fawry.{rounding_note}",
            confirmation=_confirmation_method(ctx),
            environment=environment,
        )

    return mcp
