"""D-002: durable data-contract persistence + key/schema extraction."""
import os

from odibi_anchor._utils._contracts import (
    save_contract,
    load_contract,
    contract_keys,
    contract_schema_map,
    contract_path,
)


def _ctx():
    return {
        "schema": [{"name": "id", "dtype": "long"}, {"name": "v", "dtype": "double"}],
        "candidate_keys": {"provided_key": {"columns": ["id"]}},
        "freshness": {"selected_column": "v"},
        "metrics": {"row_count": 3, "column_count": 2},
        # transient fields that must NOT be persisted
        "samples": {"data_preview": [{"id": 1, "v": 1.5}]},
    }


def test_roundtrip(tmp_path):
    p = save_contract(str(tmp_path), "cat.sch.tbl", _ctx())
    assert os.path.isfile(p)
    c = load_contract(str(tmp_path), "cat.sch.tbl")
    assert c["subject"] == "cat.sch.tbl"
    assert contract_keys(c) == ["id"]
    assert contract_schema_map(c) == {"id": "long", "v": "double"}


def test_transient_fields_not_persisted(tmp_path):
    save_contract(str(tmp_path), "x", _ctx())
    c = load_contract(str(tmp_path), "x")
    assert "samples" not in c
    assert c["metrics"] == {"row_count": 3, "column_count": 2}


def test_load_missing_returns_none(tmp_path):
    assert load_contract(str(tmp_path), "nope") is None


def test_load_corrupt_returns_none(tmp_path):
    p = contract_path(str(tmp_path), "bad")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{ not valid json")
    assert load_contract(str(tmp_path), "bad") is None


def test_subject_slug_is_filesystem_safe(tmp_path):
    base = os.path.basename(contract_path(str(tmp_path), "weird/sub ject:name"))
    assert "/" not in base and ":" not in base and " " not in base


def test_inferred_key_fallback(tmp_path):
    ctx = _ctx()
    ctx["candidate_keys"] = {"inferred_single_column_keys": ["id"]}
    save_contract(str(tmp_path), "x", ctx)
    assert contract_keys(load_contract(str(tmp_path), "x")) == ["id"]


def test_no_keys_returns_empty():
    assert contract_keys({"candidate_keys": {}}) == []


def test_latest_wins(tmp_path):
    save_contract(str(tmp_path), "x", _ctx())
    ctx2 = _ctx()
    ctx2["candidate_keys"] = {"provided_key": {"columns": ["id", "snapshot_date"]}}
    save_contract(str(tmp_path), "x", ctx2)
    assert contract_keys(load_contract(str(tmp_path), "x")) == ["id", "snapshot_date"]
