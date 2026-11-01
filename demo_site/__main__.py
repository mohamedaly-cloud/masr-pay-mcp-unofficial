"""Run the "try it" page: ``python -m demo_site`` (port 8080).

Needs DEMO_MCP_URL and DEMO_MOCK_URL, and ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY_SECRET_ARN on
Lambda). Optional: DEMO_USAGE_TABLE and DEMO_DAILY_MESSAGES for the daily cap.
"""

from __future__ import annotations

import os

import uvicorn

from demo_site.app import app_from_env
from masr_pay_mcp.__main__ import configure_logging


def main() -> None:
    configure_logging()
    uvicorn.run(
        app_from_env(),
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8080")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
