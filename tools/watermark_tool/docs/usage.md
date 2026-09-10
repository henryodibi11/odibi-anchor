# Watermark Tool — Usage

## Signature

```python
watermark_debug_context(
    source,
    target,
    watermark_col: str | None = None,
    source_watermark_col: str | None = None,
    target_watermark_col: str | None = None,
    lookback_days: int = 30,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict | str
```

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | str or DataFrame | Yes | — | Source table name or DataFrame |
| `target` | str or DataFrame | Yes | — | Target table name or DataFrame |
| `watermark_col` | str | Yes* | — | Watermark column (same name in both tables) |
| `source_watermark_col` | str | Alt | — | Watermark column name in source (when names differ) |
| `target_watermark_col` | str | Alt | — | Watermark column name in target (when names differ) |
| `lookback_days` | int | No | 30 | Window for cadence and gap analysis |
| `subject` | str | No | auto | Display label |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

*Either `watermark_col` OR both `source_watermark_col` + `target_watermark_col` must be provided.

## Basic Usage

```python
# Same watermark column in both tables
ctx = anchor("watermark", source_df, target_df, watermark_col="event_time")

# With table names (requires active Spark session)
ctx = anchor("watermark",
         "catalog.schema.source_orders",
         "catalog.schema.target_orders",
         watermark_col="updated_at")
```

## Different Column Names

```python
# Source uses "created_at", target uses "loaded_at"
ctx = anchor("watermark", source_df, target_df,
         source_watermark_col="created_at",
         target_watermark_col="loaded_at")
```

## Custom Lookback

```python
# Only analyze last 7 days for cadence detection
ctx = anchor("watermark", source_df, target_df,
         watermark_col="event_time", lookback_days=7)
```

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | str | Always `"watermark"` |
| `summary` | str | One-line staleness verdict |
| `metrics.staleness` | str | `"up_to_date"`, `"stale"`, `"very_stale"`, `"watermark_null"` |
| `metrics.lag_hours` | float | Hours between source max and target max watermark |
| `metrics.lag_days` | float | Days of lag |
| `metrics.pending_count` | int | Source rows newer than target watermark |
| `metrics.source_watermark` | str | Max watermark value in source |
| `metrics.target_watermark` | str | Max watermark value in target |
| `metrics.typical_cadence` | str | Detected load frequency: "hourly", "daily", "weekly" |
| `metrics.gaps` | list | Missing time periods in target |
| `findings` | list[str] | Lag description and pending count |
| `risks` | list[str] | Warnings about staleness or null watermarks |
| `suggested_next_actions` | list[str] | Recommended actions |

## Interpreting Staleness

| Value | Meaning | Action |
|-------|---------|--------|
| `up_to_date` | Target ≤ 1 cadence behind source | No action needed |
| `stale` | Target 1–3 cadences behind | Check scheduler, review recent runs |
| `very_stale` | Target 3+ cadences behind | Pipeline likely broken, investigate |
| `watermark_null` | Target watermark is all NULL | Initial load may have failed |

## Direct Import

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor")

from tools.watermark_tool.watermark_impl import watermark_debug_context

ctx = watermark_debug_context(
    source_df, target_df,
    watermark_col="event_time",
    subject="orders_pipeline",
)
```
