"""The hosted server: an ASGI app for AWS Lambda behind API Gateway (milestone 3).

    MASR_PAY_CONFIRM_SECRET=... FAWRY_BASE_URL=... python -m masr_pay_mcp.web   # port 8080

On Lambda, the AWS Lambda Web Adapter runs that same command (``infra/run-mcp.sh``) and
turns API Gateway events into plain HTTP requests to it.

Streamable HTTP, stateless, JSON responses: every ``POST /mcp`` stands alone, so any Lambda
instance can answer any request. Merchant credentials come in each request's headers
(``providers/fawry/config.py``) and are dropped when the call ends.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from masr_pay_mcp.__main__ import configure_logging
from masr_pay_mcp.core import confirm
from masr_pay_mcp.server import create_server

SECRET_ARN_ENV = "MASR_PAY_CONFIRM_SECRET_ARN"  # noqa: S105 (a variable name, not a secret)


def confirm_secret(env: Mapping[str, str] | None = None, fetch=None) -> bytes:
    """The token key, shared by every Lambda instance: from Secrets Manager, or the env var.

    Unlike stdio, a random per-process key is refused here: with many instances, a token
    minted by one would be rejected by the next, so refunds would never go through.
    """
    env = os.environ if env is None else env
    if arn := env.get(SECRET_ARN_ENV, "").strip():
        if fetch is None:
            import boto3  # in the Lambda Python runtime already; only needed on AWS

            fetch = boto3.client("secretsmanager").get_secret_value
        env = {confirm.SECRET_ENV: fetch(SecretId=arn)["SecretString"]}
    if not env.get(confirm.SECRET_ENV, "").strip():
        raise RuntimeError(
            f"The HTTP server needs a shared confirmation secret: set {SECRET_ARN_ENV} "
            f"(Secrets Manager) or {confirm.SECRET_ENV}."
        )
    return confirm.load_secret(env)


def _use_post(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "error": "This is an MCP server (Streamable HTTP). Connect with an MCP client, which "
            "sends POST /mcp with X-Fawry-Merchant-Code and X-Fawry-Secure-Key headers.",
            "docs": "https://github.com/mohamedaly-cloud/masr-pay-mcp-unofficial",
        },
        status_code=405,
        headers={"Allow": "POST"},
    )


def create_app(**server_options) -> Starlette:
    server = create_server(confirm_secret=confirm_secret(), **server_options)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # DNS-rebinding protection guards servers on localhost. This one is public behind
        # API Gateway, whose Host header the SDK's localhost allow-list would reject.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # A GET would open the SDK's server-to-client SSE stream and hold a Lambda until it times out
    # (28 s each; the account has 5 concurrent Lambdas). A stateless server never pushes, and
    # the spec allows 405 here. Matched before the SDK's /mcp route.
    app.router.routes.insert(0, Route("/mcp", _use_post, methods=["GET"]))
    return app


def main() -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8080")),
        # log_config=None: uvicorn's loggers go through our root handler and its redacting filter.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
