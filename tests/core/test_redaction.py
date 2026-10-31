import logging

from masr_pay_mcp.core.redaction import MASK, RedactingFilter, redact_mapping, redact_text

SIG = "a" * 64


def test_redact_mapping_masks_sensitive_keys_at_any_depth():
    body = {
        "merchantCode": "MOCKMERCHANT0001",
        "secureKey": "k",
        "signature": SIG,
        "customerEmail": "ahmed@example.com",
        "customerMobile": "01012345678",
        "nested": {"customerMail": "x@y.com", "items": [{"Signature": "s"}]},
        "amount": 350,
    }
    out = redact_mapping(body)
    assert out["merchantCode"] == "MOCKMERCHANT0001"
    assert out["amount"] == 350
    for key in ("secureKey", "signature", "customerEmail", "customerMobile"):
        assert out[key] == MASK
    assert out["nested"]["customerMail"] == MASK
    assert out["nested"]["items"][0]["Signature"] == MASK
    assert body["signature"] == SIG  # input is not mutated


def test_redact_text_query_string():
    line = (
        "GET /ECommerceWeb/Fawry/payments/status/v2?merchantCode=M&merchantRefNumber=ORDER-42"
        f"&signature={SIG} HTTP/1.1"
    )
    out = redact_text(line)
    assert SIG not in out
    assert "signature=***" in out
    assert "merchantRefNumber=ORDER-42" in out


def test_redact_text_json_and_pii():
    text = (
        '{"secureKey": "super-secret", "customerMail": "a@b.co", '
        '"note": "call +201012345678 or 01012345678, mail ahmed@example.com"}'
    )
    out = redact_text(text)
    for secret in ("super-secret", "a@b.co", "01012345678", "ahmed@example.com"):
        assert secret not in out


def test_redact_text_leaves_reference_numbers_alone():
    assert redact_text("referenceNumber 123456789 amount 350.00") == (
        "referenceNumber 123456789 amount 350.00"
    )


def test_redacting_filter_redacts_args_and_keeps_their_shape():
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "%s %s %d", ("GET", f"/x?signature={SIG}", 200), None
    )
    assert RedactingFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 3
    assert record.args[2] == 200
    assert SIG not in record.getMessage()


def test_redacting_filter_handles_mapping_args_and_msg():
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "body=%(body)s mail ahmed@example.com", None, None
    )
    record.args = {"body": {"signature": SIG, "amount": 1}}
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert SIG not in message
    assert "ahmed@example.com" not in message
    assert "'amount': 1" in message


def test_redacting_filter_handles_non_string_args_like_urls():
    import httpx2

    url = httpx2.URL(f"http://fawry/status/v2?merchantRefNumber=42&signature={SIG}")
    record = logging.LogRecord(
        "httpx2",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", url, "HTTP/1.1", 200, "OK"),
        None,
    )
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert SIG not in message
    assert "merchantRefNumber=42" in message
    assert '"HTTP/1.1 200 OK"' in message
