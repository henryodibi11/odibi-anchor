from odibi_anchor.operational import capture_environment, compare_environments


def test_capture_has_names_but_no_environment_values(monkeypatch, tmp_path):
    monkeypatch.setenv("ANCHOR_TEST_SECRET", "must-not-appear")
    result = capture_environment(packages=["certainly-not-an-installed-distribution"], roots={"workspace": tmp_path})
    rendered = str(result.to_dict())
    assert "ANCHOR_TEST_SECRET" in rendered
    assert "must-not-appear" not in rendered
    assert result.facts["packages"]["certainly-not-an-installed-distribution"]["available"] is False


def test_comparison_classifies_changed_missing_and_unverifiable():
    diff = compare_environments(
        {"status": "collected", "facts": {"same": 1, "changed": "old", "unknown": None, "left_only": True}},
        {"status": "collected", "facts": {"same": 1, "changed": "new", "unknown": "known", "right_only": True}},
    )
    assert diff["changed"] == [{"path": "changed", "left": "old", "right": "new"}]
    assert diff["missing"] == [
        {"path": "left_only", "side": "right"},
        {"path": "right_only", "side": "left"},
    ]
    assert diff["unverifiable"] == ["unknown"]
    assert diff["equivalent"] is False


def test_comparison_refuses_failed_or_unclassified_inputs():
    diff = compare_environments(
        {"status": "failed", "facts": {"runtime": "same"}, "limitations": ["API failed"]},
        {"status": "collected", "facts": {"runtime": "same"}},
    )
    assert diff["equivalent"] is None
    assert diff["unverifiable"] == ["environment"]
    assert "left: API failed" in diff["limitations"]

    assert compare_environments({"facts": {}}, {"status": "collected", "facts": {}})["equivalent"] is None
