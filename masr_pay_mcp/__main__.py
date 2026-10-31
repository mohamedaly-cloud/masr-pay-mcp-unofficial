"""Run the MCP server over stdio: ``python -m masr_pay_mcp`` (or ``masr-pay-mcp-unofficial``).

stdout belongs to the MCP protocol, so logs go to stderr, through the
redacting filter.
"""

from __future__ import annotations

import logging
import os
import sys

from masr_pay_mcp.core.redaction import RedactingFilter
from masr_pay_mcp.server import create_server


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("FAWRY_LOG_LEVEL", "INFO").upper())
    # httpx2 logs every request URL at INFO; the status v2 URL contains the signature. The
    # redacting filter masks it anyway; this just keeps the noise (and the risk) down.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)
    # The audit trail is a requirement, not debug output: FAWRY_LOG_LEVEL never silences it.
    logging.getLogger("masr_pay_mcp.audit").setLevel(logging.INFO)


def main() -> None:
    configure_logging()
    create_server().run("stdio")


if __name__ == "__main__":
    main()
