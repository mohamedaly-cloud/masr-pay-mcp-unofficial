"""Redaction for anything that might end up in a log line.

docs/design.md rule 3: never log ``secureKey``, signatures, customer email or
customer mobile. Use ``redact_mapping`` for structured data (request/response
bodies) and ``RedactingFilter`` on log handlers as a safety net for free text,
for example uvicorn access-log lines that contain ``?signature=...``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

MASK = "***"

# Compared case-insensitively. Covers both spellings Fawry uses for the email
# (customerEmail in requests, customerMail in responses).
SENSITIVE_KEYS = frozenset(
    k.lower()
    for k in (
        "secureKey",
        "secure_key",
        "signature",
        "customerEmail",
        "customer_email",
        "customerMail",
        "customer_mail",
        "customerMobile",
        "customer_mobile",
    )
)

_KEY_ALTERNATION = "|".join(sorted(map(re.escape, SENSITIVE_KEYS), key=len, reverse=True))
# key=value (query strings, form bodies)
_KV_RE = re.compile(rf"(?i)\b({_KEY_ALTERNATION})=([^&\s\"']*)")
# "key": "value" or 'key': 'value' (JSON / Python reprs)
_JSON_RE = re.compile(rf"(?i)([\"']({_KEY_ALTERNATION})[\"']\s*:\s*)([\"'])(.*?)(\3)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Egyptian mobile numbers: local 01xxxxxxxxx, or international +20 / 0020 / 20
# followed by 1xxxxxxxxx (the trunk 0 is dropped after the country code).
_EG_MOBILE_RE = re.compile(r"(?<!\d)(?:(?:\+|00)?20 ?1\d{9}|01\d{9})(?!\d)")
# Bare 64-char hex strings look like SHA-256 signatures.
_SHA256_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")


def redact_text(text: str) -> str:
    """Mask sensitive values in free text. Errs on the side of masking too much."""
    text = _KV_RE.sub(lambda m: f"{m.group(1)}={MASK}", text)
    text = _JSON_RE.sub(lambda m: f"{m.group(1)}{m.group(3)}{MASK}{m.group(5)}", text)
    text = _EMAIL_RE.sub(MASK, text)
    text = _EG_MOBILE_RE.sub(MASK, text)
    text = _SHA256_RE.sub(MASK, text)
    return text


def redact_mapping(value: Any) -> Any:
    """Return a deep copy of ``value`` with sensitive keys masked, at any depth."""
    if isinstance(value, Mapping):
        return {
            k: (MASK if isinstance(k, str) and k.lower() in SENSITIVE_KEYS else redact_mapping(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact_mapping(v) for v in value)
    return value


class RedactingFilter(logging.Filter):
    """Logging filter that redacts a record's message template and each argument.

    Arguments are redacted in place and keep their shape (tuple or dict),
    because some formatters unpack them. uvicorn's access-log formatter, for
    example, expects a 5-tuple. Attach the filter to *handlers*, not loggers,
    so it also covers records from child loggers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, Mapping):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        return True


def _redact_arg(arg: Any) -> Any:
    # Numbers keep their type so %d / %.2f still work. Everything else is redacted
    # through its string form. That includes objects such as httpx2.URL, whose
    # str() can carry ?signature=... (a live run caught exactly that).
    if arg is None or isinstance(arg, bool | int | float):
        return arg
    if isinstance(arg, Mapping | list | tuple):
        return redact_text(str(redact_mapping(arg)))
    return redact_text(str(arg))
