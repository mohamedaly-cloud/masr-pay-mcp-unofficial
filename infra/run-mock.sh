#!/bin/sh
# Started by the Lambda Web Adapter, which forwards API Gateway requests to port 8080.
export PYTHONPATH="$LAMBDA_TASK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export MOCK_HOST=127.0.0.1 MOCK_PORT=8080
exec python3 -m mock_fawry
