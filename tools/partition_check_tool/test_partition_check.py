"""Tests for partition_check_tool.

Tests use mocked Spark to avoid cluster dependency.
Covers: small files, balanced files, skewed partitions, empty partitions,
non-Delta error, non-existent table error, file size stats, query cost.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the tool module is importable
_TOOL_DIR = Path(__file__).resolve().parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

# Ensure odibi_anchor src is importable
_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from partition_check_impl import (
    _check_empty_partitions,
    _check_partition_skew,
    _check_small_files,
    _compute_file_size_stats,
    _estimate_query_cost,
    _extract_partition_values,
    _build_findings,
    _build_risks,
    _build_suggested_actions,
    _format_partition_values,
    _validate_delta_table,
    partition_check_context,
)


# ============================================================================
# Test data fixtures
# ============================================================================


def _make_files(sizes_mb: list[float], partition_values: dict | None = None) -> list[dict]:
    """Create mock file metadata from MB sizes."""
    pv = partition_values or {}
    return [
        {
            "path": f"dbfs:/table/part-{i:05d}.parquet",
            "size_bytes": int(size * 1024 * 1024),
            "partition_values": pv,
        }
        for i, size in enumerate(sizes_mb)
    ]


def _make_partitioned_files(partition_data: dict[str, list[float]]) -> list[dict]:
    """Create files across multiple partitions.

    partition_data: {"market=PJM": [10, 20, 30], "market=MISO": [5, 5]}
    """
    files = []
    for part_str, sizes_mb in partition_data.items():
        # Parse partition string
        pv = {}
        for kv in part_str.split(","):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                pv[k.strip()] = v.strip()
        for i, size in enumerate(sizes_mb):
            path_parts = "/".join(f"{k}={v}" for k, v in pv.items())
            files.append({
                "path": f"dbfs:/table/{path_parts}/part-{i:05d}.parquet",
                "size_bytes": int(size * 1024 * 1024),
                "partition_values": pv,
            })
    return files


# ============================================================================
# Tests: _extract_partition_values
# ============================================================================


class TestExtractPartitionValues:
    def test_single_partition(self):
        path = "dbfs:/table/market=PJM/part-00000.parquet"
        assert _extract_partition_values(path) == {"market": "PJM"}

    def test_multiple_partitions(self):
        path = "dbfs:/table/market=PJM/year=2024/part-00000.parquet"
        assert _extract_partition_values(path) == {"market": "PJM", "year": "2024"}

    def test_no_partitions(self):
        path = "dbfs:/table/part-00000.parquet"
        assert _extract_partition_values(path) == {}

    def test_ignores_internal_prefixes(self):
        path = "dbfs:/table/_delta_log/00000.json"
        assert _extract_partition_values(path) == {}

    def test_handles_special_chars_in_value(self):
        path = "dbfs:/table/state=New%20York/part-00000.parquet"
        assert _extract_partition_values(path) == {"state": "New%20York"}


# ============================================================================
# Tests: _check_small_files
# ============================================================================


class TestCheckSmallFiles:
    def test_all_small(self):
        files = _make_files([1, 2, 3, 5, 10])  # All < 128MB
        result = _check_small_files(files, 128)
        assert result["count"] == 5
        assert result["pct"] == 1.0
        assert result["total_files"] == 5

    def test_none_small(self):
        files = _make_files([200, 256, 300])  # All > 128MB
        result = _check_small_files(files, 128)
        assert result["count"] == 0
        assert result["pct"] == 0.0

    def test_mixed(self):
        files = _make_files([1, 50, 100, 200, 300])  # 3 small, 2 large
        result = _check_small_files(files, 128)
        assert result["count"] == 3
        assert result["pct"] == 0.6

    def test_empty_files_list(self):
        result = _check_small_files([], 128)
        assert result["count"] == 0
        assert result["total_files"] == 0

    def test_custom_threshold(self):
        files = _make_files([10, 50, 100])
        result = _check_small_files(files, 64)
        assert result["count"] == 2  # 10 and 50 are < 64MB
        assert result["threshold_mb"] == 64


# ============================================================================
# Tests: _compute_file_size_stats
# ============================================================================


class TestComputeFileSizeStats:
    def test_basic_stats(self):
        files = _make_files([10, 20, 30, 40, 50])
        result = _compute_file_size_stats(files)
        assert result["avg_mb"] == 30.0
        assert result["median_mb"] == 30.0
        assert result["min_mb"] == 10.0
        assert result["max_mb"] == 50.0

    def test_single_file(self):
        files = _make_files([100])
        result = _compute_file_size_stats(files)
        assert result["avg_mb"] == 100.0
        assert result["median_mb"] == 100.0

    def test_empty(self):
        result = _compute_file_size_stats([])
        assert result["avg_mb"] == 0.0
        assert result["max_mb"] == 0.0

    def test_percentiles(self):
        # 10 files: 1-10 MB
        files = _make_files([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        result = _compute_file_size_stats(files)
        # p10 should be near 1.9, p90 near 9.1
        assert 1.5 < result["p10_mb"] < 2.5
        assert 8.5 < result["p90_mb"] < 9.5


# ============================================================================
# Tests: _check_partition_skew
# ============================================================================


class TestCheckPartitionSkew:
    def test_balanced_partitions(self):
        files = _make_partitioned_files({
            "market=PJM": [100, 100, 100],
            "market=MISO": [100, 100, 100],
            "market=ERCOT": [100, 100, 100],
        })
        result = _check_partition_skew(files, 10.0)
        assert result["skewed"] is False
        assert result["skew_ratio"] == 1.0
        assert result["partition_count"] == 3

    def test_skewed_partitions(self):
        files = _make_partitioned_files({
            "market=PJM": [500, 500, 500],    # 1500MB
            "market=MISO": [10, 10],           # 20MB
        })
        result = _check_partition_skew(files, 10.0)
        assert result["skewed"] is True
        assert result["skew_ratio"] == 75.0  # 1500/20
        assert result["partition_count"] == 2
        assert result["largest_partition"]["values"] == {"market": "PJM"}
        assert result["smallest_partition"]["values"] == {"market": "MISO"}

    def test_single_partition(self):
        files = _make_files([100, 200, 300], partition_values={"year": "2024"})
        result = _check_partition_skew(files, 10.0)
        assert result["skewed"] is False
        assert result["partition_count"] == 1

    def test_unpartitioned(self):
        files = _make_files([100, 200, 300])  # No partition values
        result = _check_partition_skew(files, 10.0)
        assert result["skewed"] is False
        assert result["partition_count"] == 1

    def test_empty_files(self):
        result = _check_partition_skew([], 10.0)
        assert result["skewed"] is False
        assert result["partition_count"] == 0

    def test_custom_threshold(self):
        files = _make_partitioned_files({
            "market=PJM": [100],     # 100MB
            "market=MISO": [15],     # 15MB
        })
        # Ratio is 6.67x — not skewed at 10x threshold
        result = _check_partition_skew(files, 10.0)
        assert result["skewed"] is False

        # But skewed at 5x threshold
        result = _check_partition_skew(files, 5.0)
        assert result["skewed"] is True


# ============================================================================
# Tests: _check_empty_partitions
# ============================================================================


class TestCheckEmptyPartitions:
    def test_no_empty(self):
        files = _make_partitioned_files({
            "market=PJM": [100],
            "market=MISO": [50],
        })
        result = _check_empty_partitions(files)
        assert result["empty_count"] == 0

    def test_with_empty(self):
        files = _make_partitioned_files({
            "market=PJM": [100],
            "market=MISO": [0],  # 0 bytes = empty
        })
        result = _check_empty_partitions(files)
        assert result["empty_count"] == 1
        assert result["empty_partitions"][0]["values"] == {"market": "MISO"}

    def test_empty_input(self):
        result = _check_empty_partitions([])
        assert result["empty_count"] == 0


# ============================================================================
# Tests: _estimate_query_cost
# ============================================================================


class TestEstimateQueryCost:
    def test_optimal_layout(self):
        file_metrics = {"avg_mb": 128.0}
        small_files = {"pct": 0.0, "total_files": 100}
        partition = {"skew_ratio": 1.0}
        result = _estimate_query_cost(file_metrics, small_files, partition)
        assert result["scan_overhead_ratio"] == 1.0

    def test_many_small_files(self):
        file_metrics = {"avg_mb": 5.0}
        small_files = {"pct": 0.9, "total_files": 1000}
        partition = {"skew_ratio": 1.0}
        result = _estimate_query_cost(file_metrics, small_files, partition)
        assert result["scan_overhead_ratio"] > 1.0
        assert "file listing overhead" in result["explanation"]

    def test_skewed_partitions(self):
        file_metrics = {"avg_mb": 128.0}
        small_files = {"pct": 0.0, "total_files": 100}
        partition = {"skew_ratio": 20.0}
        result = _estimate_query_cost(file_metrics, small_files, partition)
        assert result["scan_overhead_ratio"] > 1.0
        assert "skew" in result["explanation"].lower()

    def test_no_files(self):
        file_metrics = {"avg_mb": 0}
        small_files = {"pct": 0.0, "total_files": 0}
        partition = {"skew_ratio": 1.0}
        result = _estimate_query_cost(file_metrics, small_files, partition)
        assert result["scan_overhead_ratio"] == 1.0


# ============================================================================
# Tests: _build_findings
# ============================================================================


class TestBuildFindings:
    def test_healthy_table(self):
        findings = _build_findings(
            file_metrics={"median_mb": 200, "min_mb": 100, "max_mb": 300},
            small_file_metrics={"count": 0, "pct": 0.0, "threshold_mb": 128},
            partition_metrics={"skewed": False},
            empty_metrics={"empty_count": 0},
            total_files=100,
            total_size_mb=20000,
        )
        assert len(findings) == 1
        assert "healthy" in findings[0]

    def test_small_files_finding(self):
        findings = _build_findings(
            file_metrics={"median_mb": 5, "min_mb": 0.001, "max_mb": 200},
            small_file_metrics={"count": 80, "pct": 0.8, "threshold_mb": 128},
            partition_metrics={"skewed": False},
            empty_metrics={"empty_count": 0},
            total_files=100,
            total_size_mb=500,
        )
        assert any("80" in f and "below 128MB" in f for f in findings)


# ============================================================================
# Tests: _format_partition_values
# ============================================================================


class TestFormatPartitionValues:
    def test_single(self):
        assert _format_partition_values({"market": "PJM"}) == "market=PJM"

    def test_multiple(self):
        result = _format_partition_values({"market": "PJM", "year": "2024"})
        assert "market=PJM" in result
        assert "year=2024" in result

    def test_empty(self):
        assert _format_partition_values({}) == "(unpartitioned)"


# ============================================================================
# Tests: _validate_delta_table (mocked)
# ============================================================================


class TestValidateDeltaTable:
    def test_valid_delta_table(self):
        mock_spark = MagicMock()
        mock_row = MagicMock()
        mock_row.asDict.return_value = {
            "format": "delta",
            "numFiles": 100,
            "sizeInBytes": 1024 * 1024 * 500,
            "partitionColumns": ["market"],
            "location": "dbfs:/table",
        }
        mock_spark.sql.return_value.collect.return_value = [mock_row]

        result = _validate_delta_table(mock_spark, "catalog.schema.my_table")
        assert result["format"] == "delta"
        assert result["numFiles"] == 100

    def test_non_delta_table(self):
        mock_spark = MagicMock()
        mock_row = MagicMock()
        mock_row.asDict.return_value = {"format": "parquet"}
        mock_spark.sql.return_value.collect.return_value = [mock_row]

        with pytest.raises(ValueError, match="not Delta"):
            _validate_delta_table(mock_spark, "catalog.schema.parquet_table")

    def test_nonexistent_table(self):
        mock_spark = MagicMock()
        mock_spark.sql.side_effect = Exception("Table not found")

        with pytest.raises(ValueError, match="does not exist"):
            _validate_delta_table(mock_spark, "catalog.schema.missing")


# ============================================================================
# Tests: partition_check_context (integration with mocked Spark)
# ============================================================================


class TestPartitionCheckContext:
    def _mock_spark_for_table(self, files_data: list[dict], detail: dict):
        """Create a fully mocked Spark session for partition_check_context."""
        mock_spark = MagicMock()

        # Mock DESCRIBE DETAIL
        mock_detail_row = MagicMock()
        mock_detail_row.asDict.return_value = detail
        mock_spark.sql.return_value.collect.return_value = [mock_detail_row]

        # Mock read.table().inputFiles()
        mock_df = MagicMock()
        mock_df.inputFiles.return_value = [f["path"] for f in files_data]
        mock_spark.read.table.return_value = mock_df

        # Mock Hadoop filesystem for file sizes
        mock_jvm = MagicMock()
        mock_spark._jvm = mock_jvm
        mock_spark._jsc = MagicMock()
        mock_spark._jsc.hadoopConfiguration.return_value = MagicMock()

        # Map paths to sizes for the filesystem mock
        path_to_size = {f["path"]: f["size_bytes"] for f in files_data}

        def mock_get_file_status(jpath):
            # jpath is a mock, so we track it via the mock_jvm.org... chain
            status = MagicMock()
            # Default to first file size (simplified mock)
            status.getLen.return_value = 0
            return status

        # For simplicity in integration test, patch _collect_file_metadata
        return mock_spark, files_data

    @patch(
        "partition_check_impl._collect_file_metadata"
    )
    @patch(
        "partition_check_impl._validate_delta_table"
    )
    def test_healthy_table(self, mock_validate, mock_collect):
        mock_validate.return_value = {
            "format": "delta",
            "numFiles": 5,
            "sizeInBytes": 5 * 200 * 1024 * 1024,
            "partitionColumns": [],
            "location": "dbfs:/table",
        }
        mock_collect.return_value = _make_files([200, 200, 200, 200, 200])

        mock_spark = MagicMock()
        result = partition_check_context(
            "catalog.schema.healthy_table",
            spark=mock_spark,
            output_format="dict",
        )

        assert result["kind"] == "partition_check"
        assert result["subject"] == "catalog.schema.healthy_table"
        assert result["metrics"]["total_files"] == 5
        assert result["metrics"]["small_files"]["count"] == 0
        assert "healthy" in result["summary"].lower() or "no action" in result["summary"].lower()

    @patch(
        "partition_check_impl._collect_file_metadata"
    )
    @patch(
        "partition_check_impl._validate_delta_table"
    )
    def test_small_files_detected(self, mock_validate, mock_collect):
        mock_validate.return_value = {
            "format": "delta",
            "numFiles": 100,
            "sizeInBytes": 500 * 1024 * 1024,
            "partitionColumns": ["market"],
            "location": "dbfs:/table",
        }
        # 80 small files + 20 large
        small = _make_files([5] * 80, {"market": "PJM"})
        large = _make_files([200] * 20, {"market": "PJM"})
        mock_collect.return_value = small + large

        mock_spark = MagicMock()
        result = partition_check_context(
            "catalog.schema.small_files_table",
            spark=mock_spark,
            output_format="dict",
        )

        assert result["metrics"]["small_files"]["count"] == 80
        assert result["metrics"]["small_files"]["pct"] == 0.8
        assert "OPTIMIZE" in result["summary"]

    @patch(
        "partition_check_impl._collect_file_metadata"
    )
    @patch(
        "partition_check_impl._validate_delta_table"
    )
    def test_partition_skew_detected(self, mock_validate, mock_collect):
        mock_validate.return_value = {
            "format": "delta",
            "numFiles": 50,
            "sizeInBytes": 2000 * 1024 * 1024,
            "partitionColumns": ["market"],
            "location": "dbfs:/table",
        }
        mock_collect.return_value = _make_partitioned_files({
            "market=PJM": [500, 500, 500],     # 1500MB
            "market=MISO": [10],                # 10MB — 150x skew
        })

        mock_spark = MagicMock()
        result = partition_check_context(
            "catalog.schema.skewed_table",
            spark=mock_spark,
            output_format="dict",
        )

        assert result["metrics"]["partition_skew"]["skewed"] is True
        assert result["metrics"]["partition_skew"]["skew_ratio"] == 150.0
        assert "skew" in result["summary"].lower()

    @patch(
        "partition_check_impl._collect_file_metadata"
    )
    @patch(
        "partition_check_impl._validate_delta_table"
    )
    def test_markdown_output(self, mock_validate, mock_collect):
        mock_validate.return_value = {
            "format": "delta",
            "numFiles": 5,
            "sizeInBytes": 1000 * 1024 * 1024,
            "partitionColumns": [],
            "location": "dbfs:/table",
        }
        mock_collect.return_value = _make_files([200, 200, 200, 200, 200])

        mock_spark = MagicMock()
        result = partition_check_context(
            "catalog.schema.table",
            spark=mock_spark,
            output_format="markdown",
        )

        assert isinstance(result, str)
        assert "Partition Diagnostics" in result

    def test_missing_table_raises(self):
        with pytest.raises(ValueError, match="table is required"):
            partition_check_context(output_format="dict")

    @patch(
        "partition_check_impl._validate_delta_table"
    )
    def test_non_delta_raises(self, mock_validate):
        mock_validate.side_effect = ValueError("not Delta")
        mock_spark = MagicMock()

        with pytest.raises(ValueError, match="not Delta"):
            partition_check_context(
                "catalog.schema.csv_table",
                spark=mock_spark,
                output_format="dict",
            )


# ============================================================================
# Run tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
