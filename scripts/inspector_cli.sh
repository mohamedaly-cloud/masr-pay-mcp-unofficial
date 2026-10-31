#!/usr/bin/env bash
# Drive the MCP server through the pinned MCP Inspector CLI against the local mock.
# Run from the repo root, with the mock running: python -m mock_fawry
set -uo pipefail  # the Inspector CLI exits non-zero on tool errors, which we expect
SECRET=${SECRET:-inspector-run-secret-0123456789abcdef}
insp() {
  npx -y @modelcontextprotocol/inspector@2.11.0 --cli \
    -e FAWRY_ENV=mock -e FAWRY_MERCHANT_CODE=MOCKMERCHANT0001 \
    -e FAWRY_SECURE_KEY=mock-secure-key-not-a-real-fawry-key \
    -e MASR_PAY_CONFIRM_SECRET="$SECRET" -e FAWRY_LOG_LEVEL=WARNING -e MASR_PAY_ENABLE_DESTRUCTIVE_TOOLS=true \
    .venv/bin/python -m masr_pay_mcp --method tools/call "$@" 2>/dev/null
}
sc() { python3 -c "import json,sys; d=json.load(sys.stdin); print('isError=%s' % d.get('isError', False), json.dumps(d.get('structuredContent') or d['content'][0]['text'])[:400])"; }
curl -s -X POST http://127.0.0.1:8080/__admin/reset >/dev/null
ORDER=INSP-$RANDOM
echo "## create"; R=$(insp --tool-name create_cash_payment --tool-arg amount_egp=150 --tool-arg order_id=$ORDER --tool-arg customer_mobile=01012345678 --tool-arg customer_email=ahmed@example.com --tool-arg "description=Order 42"); echo "$R" | sc
REF=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin)['structuredContent']['reference_number'])")
echo "## duplicate create"; insp --tool-name create_cash_payment --tool-arg amount_egp=150 --tool-arg order_id=$ORDER --tool-arg customer_mobile=01012345678 --tool-arg customer_email=ahmed@example.com --tool-arg "description=again" | sc
echo "## over limit"; insp --tool-name create_cash_payment --tool-arg amount_egp=60000 --tool-arg order_id=$ORDER-big --tool-arg customer_mobile=01012345678 --tool-arg customer_email=ahmed@example.com --tool-arg "description=big" | sc
echo "## status before"; insp --tool-name get_payment_status --tool-arg order_id=$ORDER | sc
curl -s -X POST http://127.0.0.1:8080/__admin/kiosk-pay/$REF >/dev/null
echo "## status after kiosk-pay"; insp --tool-name get_payment_status --tool-arg order_id=$ORDER | sc
echo "## refund step 1 (preview)"; P=$(insp --tool-name refund_payment --tool-arg order_id=$ORDER --tool-arg reason=Returned); echo "$P" | sc
TOKEN=$(echo "$P" | python3 -c "import json,sys; print(json.load(sys.stdin)['structuredContent']['confirmation_token'])")
echo "## refund with token but different reason"; insp --tool-name refund_payment --tool-arg order_id=$ORDER --tool-arg reason=Other --tool-arg confirmation_token=$TOKEN | sc
echo "## refund step 2 (token)"; insp --tool-name refund_payment --tool-arg order_id=$ORDER --tool-arg reason=Returned --tool-arg confirmation_token=$TOKEN | sc
echo "## replay the token"; insp --tool-name refund_payment --tool-arg order_id=$ORDER --tool-arg reason=Returned --tool-arg confirmation_token=$TOKEN | sc
