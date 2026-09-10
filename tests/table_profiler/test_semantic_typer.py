"""Unit tests for semantic type inference module."""

from __future__ import annotations

import pytest

from tools.table_profiler_tool.lib.models import Inference, SemanticType
from tools.table_profiler_tool.lib.semantic_typer import infer_semantic_type


# ---------------------------------------------------------------------------
# Tests — one per major semantic type
# ---------------------------------------------------------------------------


class TestEmptyInput:
    """Edge case: no values provided."""

    def test_empty_list_returns_unknown(self):
        result = infer_semantic_type([], "some_col")
        assert result.value == SemanticType.UNKNOWN
        assert result.confidence == 0.0
        assert "no_values" in result.evidence
        assert result.sample_size == 0


class TestEmail:
    """EMAIL detection via pattern."""

    def test_clear_emails(self):
        values = [
            "alice@example.com",
            "bob.smith@company.org",
            "test+tag@mail.co.uk",
            "user123@domain.net",
            "jane_doe@sub.domain.com",
        ]
        result = infer_semantic_type(values, "contact_email")
        assert result.value == SemanticType.EMAIL
        assert result.confidence >= 0.85

    def test_email_without_name_hint(self):
        values = ["a@b.com", "x@y.org", "z@w.net", "m@n.io", "p@q.co"]
        result = infer_semantic_type(values, "field_1")
        assert result.value == SemanticType.EMAIL
        # "field_1" matches spreadsheet-artifact pattern → confidence penalized (0.60×)
        assert result.confidence >= 0.45


class TestPhone:
    """PHONE detection — ambiguous type."""

    def test_phone_with_hint(self):
        values = [
            "555-123-4567",
            "(212) 555-0199",
            "+1 800 555 0100",
            "312.555.0142",
            "555 867 5309",
        ]
        result = infer_semantic_type(values, "phone_number")
        assert result.value == SemanticType.PHONE
        assert result.confidence >= 0.80

    def test_phone_without_hint_lower_confidence(self):
        values = [
            "555-123-4567",
            "555-234-5678",
            "555-345-6789",
            "555-456-7890",
            "555-567-8901",
        ]
        result = infer_semantic_type(values, "col_x")
        # Phone is ambiguous — without hint, confidence should be lower
        if result.value == SemanticType.PHONE:
            assert result.confidence < 0.80


class TestUUID:
    """UUID detection — highly specific pattern."""

    def test_uuids(self):
        values = [
            "550e8400-e29b-41d4-a716-446655440000",
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
            "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        ]
        result = infer_semantic_type(values, "request_id")
        assert result.value == SemanticType.UUID
        assert result.confidence >= 0.90


class TestURL:
    """URL detection."""

    def test_urls(self):
        values = [
            "https://www.example.com/page",
            "http://api.service.io/v2/data",
            "https://docs.python.org/3/library/re.html",
            "http://localhost:8080/health",
            "https://github.com/user/repo",
        ]
        result = infer_semantic_type(values, "endpoint_url")
        assert result.value == SemanticType.URL
        assert result.confidence >= 0.85


class TestZipCode:
    """ZIP_CODE detection — ambiguous without hint."""

    def test_zip_with_hint(self):
        values = ["60601", "10001", "90210", "30301", "20001"]
        result = infer_semantic_type(values, "zip_code")
        assert result.value == SemanticType.ZIP_CODE
        assert result.confidence >= 0.80

    def test_zip_without_hint_ambiguous(self):
        values = ["60601", "10001", "90210", "30301", "20001"]
        result = infer_semantic_type(values, "field_a")
        # Without hint, 5-digit numbers are ambiguous (ZIP vs numeric_string)
        assert result.value in (SemanticType.ENUM, SemanticType.ZIP_CODE, SemanticType.NUMERIC_STRING)
        assert any(
            hypothesis.value in (SemanticType.ZIP_CODE, SemanticType.NUMERIC_STRING)
            for hypothesis in result.runner_ups
        ) or result.value in (SemanticType.ZIP_CODE, SemanticType.NUMERIC_STRING)


class TestDateString:
    """DATE_STRING detection."""

    def test_iso_dates(self):
        values = [
            "2024-01-15",
            "2024-02-20",
            "2024-03-10",
            "2024-04-05",
            "2024-05-30",
        ]
        result = infer_semantic_type(values, "created_date")
        assert result.value == SemanticType.DATE_STRING
        assert result.confidence >= 0.85

    def test_us_format_dates(self):
        values = ["01/15/2024", "02/20/2024", "03/10/2024", "04/05/2024", "12/31/2023"]
        result = infer_semantic_type(values, "entry_date")
        assert result.value == SemanticType.DATE_STRING
        assert result.confidence >= 0.85


class TestBooleanString:
    """BOOLEAN_STRING detection — ambiguous type."""

    def test_yes_no_with_hint(self):
        values = ["Yes", "No", "Yes", "No", "Yes", "No", "Yes", "No"]
        result = infer_semantic_type(values, "is_active")
        assert result.value == SemanticType.BOOLEAN_STRING
        assert result.confidence >= 0.80

    def test_boolean_without_hint_lower_confidence(self):
        values = ["Y", "N", "Y", "N", "Y", "N", "Y", "N"]
        result = infer_semantic_type(values, "col_z")
        if result.value == SemanticType.BOOLEAN_STRING:
            assert result.confidence < 0.80


class TestEnum:
    """ENUM detection via low cardinality."""

    def test_low_cardinality_strings(self):
        values = (
            ["Open"] * 30
            + ["Closed"] * 25
            + ["Pending"] * 20
            + ["Cancelled"] * 15
            + ["In Progress"] * 10
        )
        result = infer_semantic_type(values, "status")
        assert result.value == SemanticType.ENUM
        assert result.confidence >= 0.80
        assert "distinct_count=5" in result.evidence


class TestCode:
    """CODE detection — structured identifiers."""

    def test_project_codes(self):
        values = [
            "PRJ-001",
            "PRJ-002",
            "PRJ-003",
            "PRJ-004",
            "PRJ-005",
        ]
        result = infer_semantic_type(values, "project_code")
        assert result.value == SemanticType.CODE
        assert result.confidence >= 0.85


class TestNumericString:
    """NUMERIC_STRING detection — ambiguous type."""

    def test_numeric_with_hint(self):
        values = ["12345", "67890", "11111", "99999", "54321"]
        result = infer_semantic_type(values, "account_number")
        # numeric_string requires name hint — "number" is a hint
        assert result.value in (SemanticType.NUMERIC_STRING, SemanticType.ZIP_CODE)

    def test_large_numbers_clearly_numeric(self):
        values = ["1234567", "9876543", "1111111", "2222222", "3333333"]
        result = infer_semantic_type(values, "transaction_number")
        assert result.value == SemanticType.NUMERIC_STRING


class TestIPAddress:
    """IP_ADDRESS detection."""

    def test_ipv4_addresses(self):
        values = [
            "192.168.1.1",
            "10.0.0.1",
            "172.16.254.1",
            "255.255.255.0",
            "8.8.8.8",
        ]
        result = infer_semantic_type(values, "host_ip")
        assert result.value == SemanticType.IP_ADDRESS
        assert result.confidence >= 0.85


class TestInferenceContract:
    """Verify Inference output contract shape."""

    def test_inference_fields_present(self):
        result = infer_semantic_type(["test@email.com"] * 5, "email")
        assert isinstance(result, Inference)
        assert isinstance(result.value, SemanticType)
        assert isinstance(result.confidence, float)
        assert isinstance(result.evidence, list)
        assert isinstance(result.counter_signals, list)
        assert isinstance(result.sample_size, int)
        assert isinstance(result.method, str)
        assert result.sample_size == 5
        assert result.method in ("regex+name_hint", "cardinality_heuristic")


class TestSpreadsheetArtifacts:
    """Spreadsheet artifact headers should reduce confidence on semantic inference."""

    def test_enum_fallback_penalized_for_unnamed_column(self):
        values = ["Alpha", "Beta", "Gamma", "Alpha", "Beta"] * 20

        normal = infer_semantic_type(values, "category_name")
        artifact = infer_semantic_type(values, "Unnamed__1")

        assert normal.value == SemanticType.ENUM
        assert artifact.value == SemanticType.ENUM
        assert artifact.confidence == pytest.approx(round(normal.confidence * 0.60, 3))
        assert "spreadsheet_artifact_column_name" in artifact.counter_signals
        assert artifact.method == "cardinality_heuristic"


class TestStateCodeRequiresNameHint:
    """STATE_CODE must not fire for generic short-code columns without a state hint."""

    def test_type_fuel_not_state_code(self):
        values = ["NG", "CO"] * 100
        result = infer_semantic_type(values, "Type__Fuel")

        assert result.value != SemanticType.STATE_CODE
        assert result.value == SemanticType.ENUM


class TestCountryCodeRequiresNameHint:
    """B2 corpus fix: country_code must not fire without a geographic name hint."""

    def test_market_column_not_country_code(self):
        """Energy market codes (MISO, SPP, PJM) must not be classified as country_code."""
        result = infer_semantic_type(["MISO", "SPP", "PJM", "ERCOT"] * 25, "market")
        assert result.value != SemanticType.COUNTRY_CODE, (
            f"'market' col with ISO-like codes should not be country_code, got {result.value}"
        )

    def test_opco_column_not_country_code(self):
        """OPCO codes (DEC, DEP, DUK) must not be classified as country_code."""
        result = infer_semantic_type(["DEC", "DEP", "DUK", "DEC"] * 25, "OPCO")
        assert result.value != SemanticType.COUNTRY_CODE, (
            f"'OPCO' column should not be country_code, got {result.value}"
        )

    def test_short_name_col_not_country_code(self):
        """Utility short names (AEP, DTE, ATC) must not be classified as country_code."""
        result = infer_semantic_type(
            ["AEP", "DTE", "ATC", "PPL", "AEP"] * 20, "Short Name"
        )
        assert result.value != SemanticType.COUNTRY_CODE, (
            f"'Short Name' col should not be country_code, got {result.value}"
        )

    def test_country_hint_still_classifies_correctly(self):
        """Column named 'country_code' with ISO-2 values should still get country_code."""
        result = infer_semantic_type(["US", "DE", "FR", "GB", "CA"] * 20, "country_code")
        assert result.value == SemanticType.COUNTRY_CODE, (
            f"'country_code' column should be country_code, got {result.value}"
        )

    def test_nation_hint_still_classifies_correctly(self):
        """Column named 'nation' should still yield country_code."""
        result = infer_semantic_type(["US", "DE", "FR", "GB"] * 25, "nation")
        assert result.value == SemanticType.COUNTRY_CODE, (
            f"'nation' column should be country_code, got {result.value}"
        )


class TestCompetingHypotheses:
    """Runner-up hypotheses and verification hints stay available to agents."""

    def test_zip_column_surfaces_numeric_string_runner_up(self):
        values = ["60601", "10001", "90210", "30301", "20001"]
        result = infer_semantic_type(values, "zip_code")

        assert result.value == SemanticType.ZIP_CODE
        assert result.runner_ups
        assert any(h.value == SemanticType.NUMERIC_STRING for h in result.runner_ups)
        assert "verify" not in result.verification_hint.lower()
        assert "leading zeros" in next(
            h.verification_hint for h in result.runner_ups if h.value == SemanticType.NUMERIC_STRING
        )

    def test_country_like_codes_without_hint_preserve_blocked_runner_up(self):
        values = ["US", "DE", "FR", "GB", "CA"] * 10
        result = infer_semantic_type(values, "market")

        assert result.value == SemanticType.ENUM
        assert any(h.value == SemanticType.COUNTRY_CODE for h in result.runner_ups)
        blocked = next(h for h in result.runner_ups if h.value == SemanticType.COUNTRY_CODE)
        assert blocked.confidence == 0.0
        assert blocked.blocker_reason
        assert "geography" in blocked.verification_hint
