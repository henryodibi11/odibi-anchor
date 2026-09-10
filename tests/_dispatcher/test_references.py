"""Engineering reference registry, matching, loading, and routing tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._references import (
    _heading_sections,
    _snapshot_sections,
    load_reference_registry,
    load_snapshot_section,
    match_reference_registry,
    reference_action,
    resolve_reference_guidance,
    search_snapshot_sections,
    task_reference_guidance,
)
from odibi_anchor.planning._task_profile import normalize_task_profile
from odibi_anchor.planning._task_render import render_task_execution_report

ROOT = Path(__file__).resolve().parents[2]
REFERENCES = ROOT / ".assistant" / "references"


def _copy_library(tmp_path: Path) -> Path:
    target = tmp_path / "references"
    shutil.copytree(REFERENCES, target)
    return target


def _symlink_or_skip(path: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise


def _registry(path: Path) -> dict:
    return json.loads((path / "registry.json").read_text(encoding="utf-8"))


def _write_registry(path: Path, value: dict) -> None:
    (path / "registry.json").write_text(json.dumps(value), encoding="utf-8")


def test_distributed_registry_validates_and_covers_initial_library():
    registry = load_reference_registry(REFERENCES)
    assert registry["schema_version"] == 1
    assert [item["id"] for item in registry["entries"]] == [
        "development.house", "development.python", "development.data-platform",
        "visualization.stack", "visualization.api-crosswalk", "visualization.interactions",
        "visualization.vega", "visualization.vega-lite", "visualization.altair",
        "visualization.deneb-powerbi", "visualization.powerbi-portability",
        "visualization.vl-convert", "python.pydantic-v2", "python.pydantic-contracts",
        "odibi-anchor.assurance-kernel", "assurance.standards-overlays",
    ]
    assert all(item["sources"] and item["refresh_triggers"] for item in registry["entries"])
    altair = next(item for item in registry["entries"] if item["id"] == "visualization.altair")
    assert altair["versions"][1]["baseline"] == "6.4.3"
    assurance = next(
        item for item in registry["entries"]
        if item["id"] == "odibi-anchor.assurance-kernel"
    )
    assert assurance["path"] == "development/assurance-kernel.md"
    content = (REFERENCES / assurance["path"]).read_text(encoding="utf-8").lower()
    assert "shadow findings do not block or authorize work" in content


def test_development_registry_profiles_have_stable_metadata_and_resources():
    registry = load_reference_registry(REFERENCES)
    profiles = {
        item["id"]: item
        for item in registry["entries"]
        if item["id"].startswith("development.")
    }
    assert tuple(profiles) == (
        "development.house", "development.python", "development.data-platform",
    )
    assert {item["profile_version"] for item in profiles.values()} == {
        "anchor-house-engineering/v1",
    }
    assert all(item["applicability"] for item in profiles.values())
    assert all((REFERENCES / item["path"]).is_file() for item in profiles.values())


def _profile(**overrides):
    values = {
        "legacy_mode": "analysis",
        "execution_mode": "read_only",
        "domains": ["general"],
        "traits": [],
    }
    values.update(overrides)
    return normalize_task_profile(**values)


@pytest.mark.parametrize(("profile", "changed", "signals", "expected", "databricks"), [
    (_profile(execution_mode="source_change"), (), (), ["development.house"], False),
    (_profile(execution_mode="source_change"), ("README.md",), (), ["development.house"], False),
    (_profile(), ("src/service.py",), (), ["development.house", "development.python"], False),
    (_profile(), ("src/types.pyi",), (), ["development.house", "development.python"], False),
    (_profile(execution_mode="source_change"), (), ("python",), ["development.house", "development.python"], False),
    (_profile(execution_mode="data_change", domains=["data"]), (), (), ["development.data-platform"], False),
    (_profile(), ("queries/load.sql",), (), ["development.house", "development.data-platform"], False),
    (_profile(), ("databricks.yml",), ("databricks",), ["development.house", "development.data-platform"], True),
    (_profile(), ("notebooks/spark_ingestion.ipynb",), (), ["development.house", "development.data-platform"], True),
    (_profile(), ("src/job.py", "queries/load.sql"), ("databricks",), ["development.house", "development.python", "development.data-platform"], True),
    (_profile(), ("docs/table-design.md",), ("python", "databricks"), [], False),
    (_profile(), ("README.md",), (), [], False),
    (_profile(domains=["data"]), (), (), [], False),
    (_profile(), (), (), [], False),
])
def test_deterministic_development_reference_matrix(
    profile, changed, signals, expected, databricks,
):
    first = resolve_reference_guidance(REFERENCES, profile, changed, signals)
    second = resolve_reference_guidance(REFERENCES, profile, changed, signals)
    assert first == second
    assert [item["id"] for item in first] == expected
    assert all(item["version"] == "anchor-house-engineering/v1" for item in first)
    assert all(item["reason"] and item["applicability"] for item in first)
    assert all(item["applicability"]["databricks"] is databricks for item in first)
    assert all(
        not ({"skill", "effect", "approval", "evidence", "status"} & set(item))
        for item in first
    )


def test_task_guidance_orders_profiles_before_technology_and_ignores_prose_only_code_words():
    profile = _profile(execution_mode="source_change")
    guidance = task_reference_guidance(
        REFERENCES,
        "Build an Altair chart where a table is displayed",
        task_profile=profile,
        changed_files=["src/chart.py"],
    )
    assert [item["id"] for item in guidance[:2]] == [
        "development.house", "development.python",
    ]
    assert "development.data-platform" not in {item["id"] for item in guidance}
    assert any(item["id"] == "visualization.altair" for item in guidance[2:])


def test_missing_development_resource_and_profile_metadata_fail_closed(tmp_path):
    library = _copy_library(tmp_path)
    (library / "development/code-standards-data-platform.md").unlink()
    with pytest.raises(ValueError, match="missing"):
        resolve_reference_guidance(library, _profile(execution_mode="source_change"))

    library = _copy_library(tmp_path / "metadata")
    registry = _registry(library)
    registry["entries"][0].pop("applicability")
    _write_registry(library, registry)
    with pytest.raises(ValueError, match="unsupported or missing"):
        load_reference_registry(library)


def test_offline_snapshot_manifest_covers_bytes_and_retains_licenses():
    snapshot_root = REFERENCES / "snapshots"
    manifest = json.loads((snapshot_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["deneb_public_docs"]["status"] == "not-copied"
    for snapshot in manifest["snapshots"]:
        assert snapshot["revision"] and snapshot["archive_sha256"]
        paths = {item["path"] for item in snapshot["files"]}
        assert f"{snapshot['name']}/LICENSE" in paths
        for item in snapshot["files"]:
            data = (snapshot_root / item["path"]).read_bytes()
            assert len(data) == item["bytes"]
            assert hashlib.sha256(data).hexdigest() == item["sha256"]


@pytest.mark.parametrize(("reference_id", "required"), [
    ("visualization.api-crosswalk", {"Layer ownership decision table", "Vega runtime API map", "Export decision matrix"}),
    ("visualization.interactions", {"Interaction design checklist", "Recipe 9: Direct Vega signal integration", "Power BI/Deneb boundary"}),
    ("visualization.powerbi-portability", {"Source-confirmed host contract", "Dataset substitution", "Power BI qualification matrix"}),
    ("python.pydantic-contracts", {"API decision map", "Discriminated mark union", "Contract test matrix"}),
])
def test_task_oriented_cookbooks_are_substantive_and_cover_required_decisions(
    reference_id, required,
):
    reference = reference_action(
        REFERENCES, "load", reference_id, output_format="dict",
    )["reference"]
    assert reference["lines"] >= 150
    assert all(f"## {heading}" in reference["content"] for heading in required)
    assert "## Sources and refresh" in reference["content"]


def test_list_and_match_never_return_content_and_matching_is_deterministic():
    listed = reference_action(REFERENCES, output_format="dict")
    first = reference_action(
        REFERENCES, "match", "ALTAIR selection renderer", limit=3, output_format="dict",
    )
    second = reference_action(
        REFERENCES, "match", "altair selection renderer", limit=3, output_format="dict",
    )
    assert all("content" not in item for item in listed["references"])
    assert first["references"] == second["references"]
    assert first["references"][0]["id"] == "visualization.altair"
    assert all(item["score"] > 0 and "content" not in item for item in first["references"])
    assert first["references"] == sorted(
        first["references"], key=lambda item: (-item["score"], item["id"]),
    )


def test_explicit_load_returns_complete_content_and_digest():
    result = reference_action(
        REFERENCES, "load", "python.pydantic-v2", output_format="dict",
    )["reference"]
    expected = (REFERENCES / "technologies/python/pydantic.md").read_text(encoding="utf-8")
    assert result["content"] == expected
    assert result["bytes"] == len(expected.encode())
    assert len(result["sha256"]) == 64
    with pytest.raises(ValueError, match="include_content=True"):
        reference_action(
            REFERENCES, "load", "python.pydantic-v2", include_content=False,
            output_format="dict",
        )


def test_snapshot_search_is_bounded_deterministic_scoped_and_content_free():
    first = reference_action(
        REFERENCES, "search", "selection parameter interval", limit=5,
        reference_id="visualization.vega-lite", output_format="dict",
    )
    second = reference_action(
        REFERENCES, "search", "SELECTION PARAMETER INTERVAL", limit=5,
        reference_id="visualization.vega-lite", output_format="dict",
    )
    assert first["sections"] == second["sections"]
    assert len(first["sections"]) == 5
    assert all("content" not in item for item in first["sections"])
    assert all("visualization.vega-lite" in item["reference_ids"] for item in first["sections"])
    assert first["sections"][0]["heading"] == "Interval Selection Properties"
    assert len(first["manifest_sha256"]) == 64


def test_snapshot_load_section_returns_exact_bounded_content_and_provenance():
    matches, manifest_sha256 = search_snapshot_sections(
        REFERENCES, "vegalite_to_pdf allowed_base_urls", limit=3,
        reference_id="visualization.vl-convert",
    )
    selected = next(item for item in matches if "vl_convert.pyi" in item["path"])
    section, loaded_manifest_sha256 = load_snapshot_section(REFERENCES, selected["section_id"])
    assert loaded_manifest_sha256 == manifest_sha256
    assert section["section_sha256"] == selected["section_sha256"]
    assert section["bytes"] <= 32_768
    assert section["lines"] <= 200
    assert "vegalite_to_pdf" in section["content"]
    assert section["revision"] == "655a7579017b9f52cf67b9631cc5def17cf92fae"


@pytest.mark.parametrize(("query", "reference_id", "expected"), [
    ("View runAsync signal listener finalize", "visualization.vega", "api/view.md"),
    ("selection resolve union intersect", "visualization.vega-lite", "parameter/select.md"),
    ("selection_interval add_params", "visualization.altair", "interactions/parameters.rst"),
    ("ExportContent selectionMaxDataPoints", "visualization.deneb-powerbi", "capabilities.json"),
    ("model_validate model_dump TypeAdapter", "python.pydantic-v2", "docs/"),
])
def test_representative_offline_questions_retrieve_authoritative_sections(
    query, reference_id, expected,
):
    matches, _ = search_snapshot_sections(
        REFERENCES, query, limit=10, reference_id=reference_id,
    )
    assert matches
    assert any(expected in item["path"] for item in matches)


def test_all_distributed_snapshot_sections_are_bounded_and_unique():
    sections, manifest_sha256 = _snapshot_sections(REFERENCES)
    ids = [item["section_id"] for item in sections]
    assert sections
    assert len(ids) == len(set(ids))
    assert len(manifest_sha256) == 64
    assert max(len(item["content"].encode()) for item in sections) <= 32_768
    assert max(len(item["content"].splitlines()) for item in sections) <= 200


def test_heading_parser_ignores_fenced_markdown_and_recognizes_rst():
    markdown = "# Root\ntext\n```\n## Not a heading\n```\n## Child\nbody\n"
    markdown_sections = _heading_sections(markdown, ".md")
    assert [item[0] for item in markdown_sections] == ["Root", "Child"]
    assert markdown_sections[1][1] == ("Root", "Child")

    rst = "Title\n=====\nintro\nSection\n-------\nbody\n"
    rst_sections = _heading_sections(rst, ".rst")
    assert [item[0] for item in rst_sections] == ["Title", "Section"]
    assert rst_sections[1][1] == ("Title", "Section")


def test_snapshot_search_rejects_bad_scope_id_and_section_id():
    with pytest.raises(ValueError, match="unknown reference id"):
        search_snapshot_sections(REFERENCES, "chart", reference_id="missing.reference")
    with pytest.raises(ValueError, match="invalid snapshot section id"):
        load_snapshot_section(REFERENCES, "../../etc/passwd")
    with pytest.raises(ValueError, match="1 through 20"):
        search_snapshot_sections(REFERENCES, "chart", limit=True)


def test_snapshot_digest_mismatch_and_parent_symlink_fail_closed(tmp_path):
    library = _copy_library(tmp_path)
    target = library / "snapshots/vega-6.3.1/docs/docs/api/view.md"
    target.write_text(target.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match its manifest"):
        search_snapshot_sections(library, "runAsync", reference_id="visualization.vega")

    library = _copy_library(tmp_path / "symlink")
    docs = library / "snapshots/vega-6.3.1/docs/docs"
    outside = tmp_path / "outside"
    docs.rename(outside)
    _symlink_or_skip(docs, outside, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot use symlinks"):
        search_snapshot_sections(library, "runAsync", reference_id="visualization.vega")


@pytest.mark.parametrize("command", ["unknown", "LOAD"])
def test_unknown_command_or_id_fails_closed(command):
    args = (command,) if command == "unknown" else (command, "missing.reference")
    with pytest.raises(ValueError):
        reference_action(REFERENCES, *args, output_format="dict")


def test_match_bounds_and_empty_technology_query():
    assert match_reference_registry(REFERENCES, "payroll kubernetes", limit=5) == ()
    with pytest.raises(ValueError, match="1 through 20"):
        match_reference_registry(REFERENCES, "altair", limit=0)
    with pytest.raises(ValueError, match="1 through 20"):
        match_reference_registry(REFERENCES, "altair", limit=True)


@pytest.mark.parametrize("mutation,match", [
    (lambda r: r.update(extra=True), "unsupported or missing"),
    (lambda r: r["entries"][1].update(id=r["entries"][0]["id"]), "duplicate reference id"),
    (lambda r: r["entries"][1].update(path=r["entries"][0]["path"]), "duplicate reference path"),
    (lambda r: r["entries"][0].update(path="../outside.md"), "unsafe reference path"),
    (lambda r: r["entries"][0]["sources"][0].update(url="http://example.test"), "must use HTTPS"),
])
def test_malformed_registry_fails_closed(tmp_path, mutation, match):
    library = _copy_library(tmp_path)
    registry = _registry(library)
    mutation(registry)
    _write_registry(library, registry)
    with pytest.raises(ValueError, match=match):
        load_reference_registry(library)


def test_missing_file_and_symlink_fail_closed(tmp_path):
    library = _copy_library(tmp_path)
    entry = _registry(library)["entries"][0]
    path = library / entry["path"]
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        load_reference_registry(library)

    library = _copy_library(tmp_path / "second")
    entry = _registry(library)["entries"][0]
    path = library / entry["path"]
    content = path.read_text(encoding="utf-8")
    path.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text(content, encoding="utf-8")
    _symlink_or_skip(path, outside)
    with pytest.raises(ValueError, match="symlink"):
        load_reference_registry(library)


def test_task_guidance_is_compact_and_progressive():
    guidance = task_reference_guidance(
        REFERENCES, "Build an Altair selection and Pydantic contract",
    )
    assert {item["id"] for item in guidance} >= {
        "visualization.altair", "python.pydantic-v2",
    }
    assert all("content" not in item for item in guidance)
    altair = next(item for item in guidance if item["id"] == "visualization.altair")
    assert altair["load"] == 'anchor("references", "load", "visualization.altair")'
    assert altair["search"] == (
        'anchor("references", "search", "<specific query>", '
        'reference_id="visualization.altair")'
    )
    assert task_reference_guidance(REFERENCES, "Update payroll retry policy") == []


def test_task_markdown_renders_reference_guidance():
    context = {
        "kind": "task_execution_context", "subject": "Altair chart",
        "summary": "ready", "status": "ready", "mode": "implementation",
        "readiness": {"score": 100}, "canonical_guidance": [], "artifact_contract": {},
        "context_plan": {}, "guidance_obligations": [], "plan": [], "discovery": {},
        "verification": {}, "risks": [], "findings": [], "suggested_next_actions": [],
        "reference_guidance": task_reference_guidance(REFERENCES, "Altair"),
    }
    rendered = render_task_execution_report(context)
    assert "## Engineering References" in rendered
    assert 'anchor("references", "load", "visualization.altair")' in rendered
    assert 'anchor("references", "search", "<specific query>"' in rendered
