"""Shared pytest fixtures for Table Profiler tests."""

from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture
def clean_fact_df() -> pd.DataFrame:
    """Return a small fact-like DataFrame with clean types and no duplicates."""

    return pd.DataFrame(
        {
            "invoice_id": ["INV-001", "INV-002", "INV-003", "INV-004"],
            "customer_id": ["CUST-1", "CUST-2", "CUST-1", "CUST-3"],
            "amount": [100.0, 200.5, 150.25, 75.0],
            "status": ["Open", "Paid", "Open", "Cancelled"],
            "line_number": [1, 1, 2, 1],
        }
    )


@pytest.fixture
def dirty_excel_like_df() -> pd.DataFrame:
    """Return a messy DataFrame that mimics an Excel-loaded ingestion artifact."""

    return pd.DataFrame(
        {
            "project_code": ["PRJ-001", "PRJ-001 ", " PRJ-002", None, "PRJ-003"],
            "capacity_text": ["100.5MW", "200 MW", "N/A", "150", "See Note"],
            "status": ["Active", "ACTIVE", "active", "Withdrawn", "TBD"],
            "entered_on": ["2024-01-15", "01/16/2024", "1/17/24", None, "2024-01-19"],
        }
    )
