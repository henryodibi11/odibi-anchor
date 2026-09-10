from odibi_anchor.operational import REDACTED, redact


def test_recursive_fields_headers_text_and_signed_urls_are_redacted():
    value = {
        "authorization": "Bearer visible",
        "nested": [{"password": "visible"}, "token=visible"],
        "url": "https://user:pass@example.test/path?ok=yes&X-Amz-Signature=visible",
    }
    cleaned, audit = redact(value)
    rendered = str(cleaned)
    assert "=visible" not in rendered and "user:pass" not in rendered
    assert cleaned["authorization"] == REDACTED
    assert audit["count"] == 5
    assert audit["categories"] == sorted(audit["categories"])


def test_authorization_assignment_redacts_scheme_and_token():
    cleaned, _ = redact("authorization: Bearer top-secret")
    assert cleaned == "authorization: <redacted>"


def test_embedded_signed_urls_and_non_http_userinfo_are_redacted():
    cleaned, audit = redact(
        "failed https://host/path?X-Amz-Signature=aws-secret and "
        "s3://user:password@bucket/path and "
        "https://blob/path?sv=1&sp=r&sig=azure-secret"
    )
    assert "aws-secret" not in cleaned
    assert "azure-secret" not in cleaned
    assert "user:password" not in cleaned
    assert audit["count"] >= 5
