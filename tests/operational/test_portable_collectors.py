from odibi_anchor.operational import delta_changes, run_diff, spark_diagnose, uc_context


def test_serverless_plan_requires_metrics_for_skew_and_spill():
    result = spark_diagnose(
        "AdaptiveSparkPlan\n+- Exchange hashpartitioning(id)\n+- BroadcastHashJoin id\n+- BatchEvalPython",
        query_profile={"metrics": [{"spill_bytes": 4096, "max_partition_bytes": 900, "median_partition_bytes": 100}]},
    )
    kinds = {finding["kind"] for finding in result.findings}
    assert {"exchange", "join_strategy", "python_udf", "spill", "skew"} <= kinds
    assert all("evidence" in finding for finding in result.findings)
    no_metrics = spark_diagnose("Exchange hashpartitioning(id) /* skew */")
    assert not ({"skew", "spill"} & {finding["kind"] for finding in no_metrics.findings})


def test_uc_normalizes_serverless_metadata_and_denied_status():
    result = uc_context({
        "object": {"full_name": "main.sales.orders", "type": "TABLE"},
        "detail": {"owner": "data", "location": "https://user:pass@store/path?sig=secret"},
        "direct_grants": [{"principal": "analysts", "privileges": ["SELECT"]}],
        "tags": {"domain": "sales"}, "dependencies": ["main.sales.v_orders"],
        "table_lineage": [], "column_lineage": [], "row_filters": ["region_filter"],
        "column_masks": [{"column": "email", "function": "mask_email"}],
    })
    assert result.facts["detail"]["location"] == "https://store/path"
    assert any("effective privileges" in item for item in result.limitations)
    denied = uc_context({"status": "denied", "error": {"type": "PermissionError", "message": "denied"}})
    assert denied.status == "denied" and not denied.findings


def test_delta_cdf_variants_hash_keys_and_preserve_metrics():
    result = delta_changes(
        {"version": 12, "cdf_enabled": True, "retention": "30 days"},
        history=[{"version": 12, "operation": "MERGE", "operation_metrics": {"numOutputRows": "4"}}],
        cdf_summary={"readable_from": 8, "readable_to": 12, "change_counts": {"insert": 3, "delete": 1},
                     "changed_keys": [{"id": 42}], "affected_columns": ["amount"],
                     "target_version_available": True, "files_available": True},
    )
    assert result.facts["cdf"]["change_counts"] == {"insert": 3, "update_preimage": 0, "update_postimage": 0, "delete": 1}
    assert result.facts["cdf"]["hashed_changed_keys"][0] != "42"
    assert result.facts["history"][0]["operation_metrics"]["numOutputRows"] == "4"
    expired = delta_changes({"version": 2}, cdf_summary={"expired": True})
    assert expired.facts["cdf"]["enabled"] is None
    assert not any("disabled" in item for item in expired.limitations)
    assert any("state is unknown" in item for item in expired.limitations)
    assert any("retention is unknown" in item for item in expired.limitations)
    assert any("expired" in item for item in expired.limitations)

    string_false = delta_changes({"cdf_enabled": "false"}, history=[{"version": 4}])
    assert string_false.facts["cdf"]["enabled"] is False
    assert any("disabled" in item for item in string_false.limitations)


def test_run_diff_is_conservative_about_outcomes_and_missing_fields():
    result = run_diff(
        {"status": "SUCCESS", "code_revision": "a", "parameters": {"day": "1"}},
        {"status": "FAILED", "code_revision": "b", "parameters": {"day": "1"}},
    )
    by_field = {item["field"]: item for item in result.facts["comparisons"]}
    assert by_field["status"]["label"] == "probably unrelated"
    assert by_field["code_revision"]["label"] == "potentially relevant"
    assert by_field["runtime"]["label"] == "unable to verify"
    explicit = run_diff({"code_revision": "a"}, {"code_revision": "b", "causal_evidence": {"code_revision": "stack points to removed API"}})
    assert explicit.facts["comparisons"][0]["label"] == "likely causal"
