"""In-memory order store for the mock. Mock only: the real MCP server is stateless."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Status values the mock produces. Fawry also uses NEW / PENDING / FAILED in
# some places (see docs/design.md "Order statuses"); the mock sticks to these.
UNPAID = "UNPAID"
PAID = "PAID"
EXPIRED = "EXPIRED"
CANCELED = "CANCELED"
REFUNDED = "REFUNDED"

Clock = Callable[[], int]  # returns epoch milliseconds


def system_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class OrderNotFound(LookupError):
    pass


class DuplicateOrder(ValueError):
    pass


class InvalidTransition(ValueError):
    """The order is in a status that doesn't allow the requested action."""

    def __init__(self, order: Order, action: str):
        self.order = order
        self.action = action
        super().__init__(f"cannot {action} an order in status {order.status}")


@dataclass
class Order:
    merchant_code: str
    merchant_ref_num: str
    reference_number: str
    order_amount: Decimal
    fawry_fees: Decimal
    payment_method: str
    customer_mobile: str
    customer_email: str
    description: str
    language: str
    charge_items: list[dict[str, Any]]
    created_at_ms: int
    customer_name: str | None = None
    customer_profile_id: str | None = None
    payment_expiry_ms: int | None = None
    order_webhook_url: str | None = None
    status: str = UNPAID
    payment_time_ms: int | None = None
    refunded_amount: Decimal | None = None
    refund_reason: str | None = None
    history: list[tuple[int, str]] = field(default_factory=list)

    @property
    def payment_amount(self) -> Decimal:
        """What the customer pays at the outlet: order amount plus Fawry's fees."""
        return self.order_amount + self.fawry_fees

    def snapshot(self) -> dict[str, Any]:
        """Plain-dict view for the admin endpoints."""
        return {
            "referenceNumber": self.reference_number,
            "merchantCode": self.merchant_code,
            "merchantRefNum": self.merchant_ref_num,
            "orderStatus": self.status,
            "orderAmount": float(self.order_amount),
            "fawryFees": float(self.fawry_fees),
            "paymentAmount": float(self.payment_amount),
            "paymentMethod": self.payment_method,
            "customerName": self.customer_name,
            "customerMobile": self.customer_mobile,
            "customerMail": self.customer_email,
            "customerProfileId": self.customer_profile_id,
            "description": self.description,
            "createdAt": self.created_at_ms,
            "paymentExpiry": self.payment_expiry_ms,
            "paymentTime": self.payment_time_ms,
            "refundedAmount": (
                float(self.refunded_amount) if self.refunded_amount is not None else None
            ),
            "history": [{"at": at, "status": status} for at, status in self.history],
        }


class OrderStore:
    """Thread-safe in-memory orders, indexed by Fawry reference and by merchant reference."""

    def __init__(self, clock: Clock = system_clock_ms):
        self._clock = clock
        self._lock = threading.RLock()
        self._by_reference: dict[str, Order] = {}
        self._by_merchant_ref: dict[tuple[str, str], str] = {}

    def now_ms(self) -> int:
        return self._clock()

    # ------------------------------------------------------------ create
    def create(self, **fields: Any) -> Order:
        with self._lock:
            key = (fields["merchant_code"], fields["merchant_ref_num"])
            if key in self._by_merchant_ref:
                raise DuplicateOrder(f"merchantRefNum {key[1]!r} already exists")
            now = self.now_ms()
            order = Order(
                reference_number=self._new_reference_number(),
                created_at_ms=now,
                **fields,
            )
            order.history.append((now, order.status))
            self._by_reference[order.reference_number] = order
            self._by_merchant_ref[key] = order.reference_number
            return order

    def _new_reference_number(self) -> str:
        """Random 9-digit Fawry-style reference number (no leading zero), unique in the store."""
        while True:
            ref = str(100_000_000 + secrets.randbelow(900_000_000))
            if ref not in self._by_reference:
                return ref

    # ------------------------------------------------------------ read
    def get_by_reference(self, reference_number: str) -> Order:
        with self._lock:
            order = self._by_reference.get(reference_number)
            if order is None:
                raise OrderNotFound(reference_number)
            self._apply_expiry(order)
            return order

    def get_by_merchant_ref(self, merchant_code: str, merchant_ref_num: str) -> Order:
        with self._lock:
            ref = self._by_merchant_ref.get((merchant_code, merchant_ref_num))
            if ref is None:
                raise OrderNotFound(merchant_ref_num)
            return self.get_by_reference(ref)

    def all(self) -> list[Order]:
        with self._lock:
            orders = list(self._by_reference.values())
            for order in orders:
                self._apply_expiry(order)
            return orders

    # ------------------------------------------------------------ transitions
    def _apply_expiry(self, order: Order) -> None:
        """UNPAID -> EXPIRED once the clock is past paymentExpiry.

        Expiry is applied lazily whenever an order is read: no background timer
        is needed, and tests can move time with an injected clock.
        """
        if (
            order.status == UNPAID
            and order.payment_expiry_ms is not None
            and self.now_ms() > order.payment_expiry_ms
        ):
            self._set_status(order, EXPIRED, at=order.payment_expiry_ms)

    def _set_status(self, order: Order, status: str, at: int | None = None) -> None:
        order.status = status
        order.history.append((self.now_ms() if at is None else at, status))

    def kiosk_pay(self, reference_number: str) -> Order:
        """UNPAID -> PAID: the customer paid cash at a Fawry outlet."""
        with self._lock:
            order = self.get_by_reference(reference_number)
            if order.status != UNPAID:
                raise InvalidTransition(order, "pay")
            order.payment_time_ms = self.now_ms()
            self._set_status(order, PAID)
            return order

    def cancel(self, reference_number: str) -> Order:
        """UNPAID -> CANCELED."""
        with self._lock:
            order = self.get_by_reference(reference_number)
            if order.status != UNPAID:
                raise InvalidTransition(order, "cancel")
            self._set_status(order, CANCELED)
            return order

    def refund(self, reference_number: str, amount: Decimal, reason: str | None) -> Order:
        """PAID -> REFUNDED. The caller checks the status and amount first."""
        with self._lock:
            order = self.get_by_reference(reference_number)
            if order.status != PAID:
                raise InvalidTransition(order, "refund")
            order.refunded_amount = amount
            order.refund_reason = reason
            self._set_status(order, REFUNDED)
            return order

    def reset(self) -> int:
        with self._lock:
            count = len(self._by_reference)
            self._by_reference.clear()
            self._by_merchant_ref.clear()
            return count
