"""One structured audit line per tool call (docs/design.md "Security and trust" #2).

JSON on the ``masr_pay_mcp.audit`` logger: stderr locally (stdout carries MCP JSON-RPC), CloudWatch
on Lambda. Every line passes through ``redact_mapping`` here and ``RedactingFilter`` on the
handler, so secrets and customer PII never reach it (rule 3). No database.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from masr_pay_mcp.core.redaction import redact_mapping, redact_text

audit_log = logging.getLogger("masr_pay_mcp.audit")


@contextmanager
def audited(
    tool: str,
    *,
    request_id: object,
    order_id: str,
    amount: Decimal | None = None,
    log_success: bool = True,
) -> Iterator[dict[str, Any]]:
    """Record one tool call. The body may add fields (``outcome``, ``raw_status``, ...).

    An exception is logged as ``outcome: "error"`` and re-raised. ``log_success=False`` is for
    pre-checks that only need a line when they refuse; the tool body logs the success.
    """
    entry: dict[str, Any] = {
        "tool": tool,
        "request_id": None if request_id is None else str(request_id),
        "merchant_ref": order_id,
        "amount_egp": None if amount is None else f"{amount:.2f}",
    }
    try:
        yield entry
    except Exception as exc:
        entry["outcome"] = "error"
        entry["error"] = redact_text(str(exc))[:500]
        code = _provider_code(exc)
        if code is not None:
            entry["provider_code"] = code
        _emit(entry)
        raise
    entry.setdefault("outcome", "ok")
    if log_success:
        _emit(entry)


def _provider_code(exc: BaseException) -> object:
    """Fawry's statusCode (e.g. 9946), if the error, or what caused it, carries one."""
    seen: BaseException | None = exc
    while seen is not None:
        code = getattr(seen, "status_code", None)
        if code is not None:
            return code
        seen = seen.__cause__
    return None


def _emit(entry: dict[str, Any]) -> None:
    line = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **entry}
    audit_log.info("%s", json.dumps(redact_mapping(line), ensure_ascii=False, default=str))
