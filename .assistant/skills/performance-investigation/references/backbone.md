# Performance investigation workflow

> Preserved technique source for the native owner.

When the code produces correct results but is too slow, uses too much memory, or costs
too much. This is a different diagnostic process from debugging wrong data.

**Complements `debugging`**: that skill handles wrong results. This skill handles
correct but slow/expensive results.

## Triage — What Kind of Performance Problem?

| Symptom | Category | Jump to |
|---|---|---|
| Query/job takes too long | [Slow Execution](#slow-execution) | Execution plan, partition skew, shuffle |
| Out of memory (OOM) | [Memory Pressure](#memory-pressure) | Data size, spill, cache bloat |
| Cluster costs too high | [Resource Efficiency](#resource-efficiency) | Right-sizing, caching strategy |
| Performance degraded after change | [Regression](#performance-regression) | Before/after comparison |
| Pipeline worked at 1M rows, fails at 100M | [Scale Wall](#scale-wall) | Algorithm complexity, partition strategy |

---

## Slow Execution

### Diagnostic Sequence

```python
# 1. Baseline — how slow is it?
import time
start = time.time()
result = df.count()  # or whatever action is slow
elapsed = time.time() - start
print(f"Elapsed: {elapsed:.1f}s, Rows: {result:,}")

# 2. Execution plan — where is time spent?
df.explain(True)  # Shows physical plan with stage details

# 3. Check for shuffles (expensive!)
df.explain("cost")  # Shows estimated cost per operation
```

### Common Culprits

| Pattern | Symptom | Fix |
|---|---|---|
| **Full shuffle join** | Large JOIN with no broadcast | `F.broadcast(small_df)` for tables <10MB |
| **Partition skew** | One task takes 10x longer than others | Salt the skewed key or repartition |
| **Too many partitions** | 10,000+ tasks for small data | `df.coalesce(target_partitions)` |
| **Too few partitions** | Few tasks doing all the work | `df.repartition(num, "key")` |
| **Repeated computation** | Same expensive DF used multiple times | `.cache()` or `.persist()` + action to materialize |
| **withColumn chain** | 50+ sequential withColumn calls | Replace with single `.select()` with all expressions |
| **Collect on large data** | `.collect()` or `.toPandas()` on millions of rows | Keep in Spark, use `.show()` for sampling |
| **UDF bottleneck** | Python UDF on every row | Replace with built-in Spark functions or Pandas UDF |
| **Sort before write** | Global `.orderBy()` before save | Remove or use `.sortWithinPartitions()` |
| **Unnecessary actions** | `.count()` for validation between transforms | Remove intermediate counts in production |

### Spark UI Investigation

Use these tabs only when a classic Spark UI is actually available. On serverless
Databricks, prefer bounded query-profile and physical-plan evidence, including
`anchor("spark_diagnose", plan_text, query_profile=...)`; do not assume classic stage APIs
exist. Preserve aggregate metrics and relevant plan excerpts, not unbounded plans or logs.

```python
# After running the slow job, check Spark UI:
# 1. Stages tab — which stage is slowest?
# 2. Tasks tab — is one task much slower? (skew)
# 3. SQL tab — which operation dominates?
# 4. Storage tab — what's cached? Memory usage?
```

**Key metrics to record:**
- Total duration
- Slowest stage and its operation
- Shuffle read/write bytes
- Spill to disk (memory pressure indicator)
- Task duration distribution (max/median ratio >3 = skew)

---

## Memory Pressure

### Diagnostic Sequence

```python
# 1. How big is the data?
print(f"Rows: {df.count():,}")
print(f"Columns: {len(df.columns)}")
print(f"Partitions: {df.rdd.getNumPartitions()}")

# 2. Estimate memory footprint
# Rule of thumb: Spark memory per partition ≈ uncompressed size / num_partitions
# If partition > 256MB, it may cause OOM

# 3. Check for wide transformations that accumulate
df.explain(True)  # Look for BroadcastHashJoin vs SortMergeJoin
```

### Common Culprits

| Pattern | Cause | Fix |
|---|---|---|
| **OOM on join** | Broadcasting a table that's too large | Set `spark.sql.autoBroadcastJoinThreshold = -1` to force sort-merge |
| **OOM on collect** | `.collect()` pulls all data to driver | Never collect large data — sample first |
| **OOM on cache** | Caching more data than cluster memory | Use `.persist(StorageLevel.DISK_ONLY)` or don't cache |
| **Spill to disk** | Partitions too large for executor memory | Increase partitions: `df.repartition(target)` |
| **Driver OOM** | Aggregation result too large for driver | Use `.show()` instead of `.collect()`, or write to table |
| **String explosion** | String columns with huge values (JSON, HTML) | Filter or truncate before processing |

---

## Resource Efficiency

### Right-Sizing Checklist

| Check | Command | Action |
|---|---|---|
| Cluster too big for data? | Compare data size vs cluster memory | Downsize cluster |
| All-purpose vs job cluster? | Check cluster type | Job clusters for pipelines (auto-terminate) |
| Photon enabled? | Check cluster config | Enable for SQL-heavy workloads |
| Autoscaling? | Check min/max workers | Enable for variable workloads |
| Cache persisting after use? | Check Storage tab in Spark UI | `df.unpersist()` after use |

### Cost Optimization Patterns

| Expensive | Cheaper alternative |
|---|---|
| Full table scan + filter | Partition pruning with filter pushdown |
| Global sort before write | `sortWithinPartitions()` or skip sort |
| `.count()` for "is empty?" | `df.head(1) is not None` or `df.limit(1).count() > 0` |
| Re-reading same table | Cache and reuse |
| Large broadcast join | Sort-merge join (uses disk, not memory) |
| Overpartitioned writes | `.coalesce()` before write to reduce small files |

---

## Performance Regression

### Diagnostic Sequence

```python
# 1. Establish baseline
# What was the performance BEFORE the change?
# Check: Spark job history, previous run logs, monitoring

# 2. Compare execution plans
# Save explain() output from before and after
df_old.explain(True)
df_new.explain(True)

# 3. Identify what changed in the plan
# Look for: new shuffles, missing broadcast, different join strategy,
# partition count changes, added stages
```

### Common Regression Causes

| Change type | How it causes regression | Detection |
|---|---|---|
| Added a JOIN | Introduces shuffle or fanout | New Exchange stage in plan |
| Changed filter order | Less selective filter runs first | Row counts between stages |
| Removed .cache() | Same DF recomputed multiple times | Duplicate stages in plan |
| Added column | Wide rows use more memory per partition | Partition size increase |
| Changed write mode | Merge vs overwrite vs append have different costs | Write stage duration |

---

## Scale Wall

When code works at small scale but fails at large scale:

| Scale factor | Typical breakpoint | Cause |
|---|---|---|
| 10x rows | 1M → 10M | `.collect()` or driver-side processing |
| 100x rows | 10M → 1B | Partition skew, shuffle memory |
| Column explosion | 50 → 500 columns | Plan compilation time, memory per row |
| File count | 100 → 10,000 files | Driver-side listing, small file problem |

### Solutions by Scale Problem

| Problem | Solution |
|---|---|
| Hits driver memory | Keep everything in Spark — no collect, no toPandas |
| Hits executor memory | Repartition, reduce partition size, use disk spill |
| Hits shuffle limits | Use broadcast for small sides, salt for skew |
| Hits plan compilation | Break into stages with intermediate temp tables |
| Hits file listing | Use Delta table with OPTIMIZE instead of raw files |

---

## Investigation Workflow

1. **Baseline** — measure current performance (time, memory, cost)
2. **Profile** — identify the bottleneck (Spark UI, explain plan)
3. **Hypothesize** — match bottleneck to common culprit
4. **Test** — make ONE change and re-measure
5. **Verify** — did it actually improve? By how much?
6. **Assess learning** — capture only an evidence-backed reusable observation, or record none

**Rule:** Change one thing at a time. Multiple simultaneous changes make it impossible
to know what helped.

## Integration with odibi-anchor

```python
# 1. Plan the investigation
anchor("task", "investigate slow pipeline", goal="identify and fix performance bottleneck",
    mode="debugging")

# 2. Profile the data
anchor("profile_table", df, subject="catalog.schema.table")  # Size, partitions, freshness

# 3. After fixing, gate and assess evidence-backed learning
anchor("gate")
observation = anchor("learning", "capture", observation_type="reusable_practice", ...)
anchor("learning", "assess", outcome="observations_recorded",
   observation_ids=[observation["item"]["item_id"]])
```
