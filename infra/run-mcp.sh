#!/bin/sh
# Started by the Lambda Web Adapter, which forwards API Gateway requests to port 8080.
export PYTHONPATH="$LAMBDA_TASK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m masr_pay_mcp.web
