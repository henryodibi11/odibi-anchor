"""S-3: spec review checks gained teeth — testable AC + real verification coverage."""
from datetime import datetime, timezone

from odibi_anchor._dispatcher._spec import spec_action


def _proj(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    specs = project / "specs"
    specs.mkdir()
    return project, specs


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spec(specs, name, ac_lines, verification="Run tests."):
    specs.joinpath(f"{name}_SPEC.md").write_text(
        f"---\nstatus: ready\nmode: analysis\ncreated: {_now()}\n"
        f"files_touched: []\ndependencies: []\npermissions_needed: []\n---\n\n"
        f"# {name}\n\n## Problem\n\nP.\n\n## Design\n\ncw(\"safe\")\n\n"
        f"## Acceptance Criteria\n\n{ac_lines}\n\n## Verification\n\n{verification}\n",
        encoding="utf-8",
    )


def _check(project, specs, name):
    r = spec_action(str(project), "review", name, specs_dir=str(specs), output_format="dict")
    return {c["name"]: c["passed"] for c in r["checks"]}


def test_vague_ac_fails_standards_alignment(tmp_path):
    project, specs = _proj(tmp_path)
    _spec(specs, "VAGUE", "- Works")
    assert _check(project, specs, "VAGUE")["standards_alignment"] is False


def test_testable_ac_passes_standards_alignment(tmp_path):
    project, specs = _proj(tmp_path)
    _spec(specs, "EARS", "- WHEN x happens, the system SHALL return 200")
    assert _check(project, specs, "EARS")["standards_alignment"] is True


def test_numeric_ac_passes_standards_alignment(tmp_path):
    project, specs = _proj(tmp_path)
    _spec(specs, "NUM", "- p95 latency under 200ms for 10000 rows")
    assert _check(project, specs, "NUM")["standards_alignment"] is True


def test_no_verification_fails_test_coverage(tmp_path):
    project, specs = _proj(tmp_path)
    specs.joinpath("NOVER_SPEC.md").write_text(
        f"---\nstatus: ready\nmode: analysis\ncreated: {_now()}\n"
        f"files_touched: []\ndependencies: []\npermissions_needed: []\n---\n\n"
        f"# NoVer\n\n## Problem\n\nP.\n\n## Design\n\ncw(\"safe\")\n\n"
        f"## Acceptance Criteria\n\n- system SHALL return 200\n",
        encoding="utf-8",
    )
    assert _check(project, specs, "NOVER")["test_coverage"] is False


def test_verification_section_passes_test_coverage(tmp_path):
    project, specs = _proj(tmp_path)
    _spec(specs, "VER", "- system SHALL return 200", verification="Run the suite.")
    assert _check(project, specs, "VER")["test_coverage"] is True
