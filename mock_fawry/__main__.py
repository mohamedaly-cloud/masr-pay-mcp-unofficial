"""Run the mock locally: ``python -m mock_fawry`` (env: MOCK_SECURE_KEY, MOCK_MERCHANT_CODE,
MOCK_FAWRY_FEES, MOCK_HOST, MOCK_PORT).

Every log handler gets a redacting filter. Without it, uvicorn's access log
would print the status v2 query string, which contains ``signature=...``.
"""

from __future__ import annotations

import copy
import os

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from mock_fawry.app import DEFAULT_SECURE_KEY, create_app


def redacted_log_config() -> dict:
    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact"] = {"()": "fawry_mcp.redaction.RedactingFilter"}
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("redact")
    return config


def main() -> None:
    if os.environ.get("MOCK_SECURE_KEY", DEFAULT_SECURE_KEY) == DEFAULT_SECURE_KEY:
        print("mock_fawry: using the built-in fake secure key (set MOCK_SECURE_KEY to change it)")
    uvicorn.run(
        create_app(),
        host=os.environ.get("MOCK_HOST", "127.0.0.1"),
        port=int(os.environ.get("MOCK_PORT", "8080")),
        log_config=redacted_log_config(),
    )


if __name__ == "__main__":
    main()
