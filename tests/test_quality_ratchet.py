"""Focused quality-ratchet contract and failure-path tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "quality_ratchet.py"
PYPROJECT = SCRIPT.parents[1] / "pyproject.toml"
SPEC = importlib.util.spec_from_file_location("quality_ratchet_test_module", SCRIPT)
assert SPEC and SPEC.loader
quality = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quality
SPEC.loader.exec_module(quality)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True,
    ).stdout.strip()


def _repository(root: Path) -> str:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "tools").mkdir()
    (root / "scripts").mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "quality@example.invalid")
    _git(root, "config", "user.name", "Quality Ratchet")
    for name, content in {
        "src/a.py": "one\ntwo\nthree\n",
        "src/rename.py": "same\ncontent\n",
        "src/delete.py": "delete\n",
    }.items():
        (root / name).write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _finding(root: Path, path: str = "src/a.py", line: int = 2, message: str = "problem"):
    return quality._finding(
        "ruff", root, path=path, rule="F001", message=message, line=line, column=1,
    )


def _baseline(root: Path, finding: dict, *, tool: str = "ruff", version: str = "1.2.3") -> dict:
    payload = quality._baseline_payload(tool, version, "a" * 40, [finding])
    destination = root / quality.BASELINE_PATHS[tool]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(quality._canonical_bytes(payload))
    return payload


def _quality_requirements() -> dict[str, Requirement]:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    requirements = (Requirement(value) for value in pyproject["dependency-groups"]["quality"])
    return {requirement.name: requirement for requirement in requirements}


def test_baseline_bound_analyzers_are_pinned_to_the_recorded_versions() -> None:
    requirements = _quality_requirements()

    for tool, baseline_path in quality.BASELINE_PATHS.items():
        baseline = json.loads((SCRIPT.parents[1] / baseline_path).read_text(encoding="utf-8"))
        assert str(requirements[tool].specifier) == f"=={baseline['tool_version']}"


def test_pyright_type_dependencies_are_exactly_pinned() -> None:
    requirements = _quality_requirements()

    for package in ("numpy", "pandas"):
        specifiers = list(requirements[package].specifier)
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="


def test_baseline_is_canonical_sorted_and_line_independent(tmp_path: Path) -> None:
    first = _finding(tmp_path, "src/z.py", 2)
    shifted = _finding(tmp_path, "src/z.py", 200)
    other_file = _finding(tmp_path, "src/a.py", 2)

    payload = quality._baseline_payload("ruff", "1.2.3", "a" * 40, [first, shifted, other_file])

    assert first["fingerprint"] == shifted["fingerprint"]
    assert first["fingerprint"] != other_file["fingerprint"]
    assert len(payload["findings"]) == 2
    assert [item["path"] for item in payload["findings"]] == ["src/a.py", "src/z.py"]
    assert quality._canonical_bytes(payload).endswith(b"\n")
    assert json.loads(quality._canonical_bytes(payload)) == payload


def test_changed_lines_override_baseline_but_unrelated_line_shifts_do_not(tmp_path: Path) -> None:
    original = _finding(tmp_path, line=2)
    baseline = quality._baseline_payload("ruff", "1.2.3", "a" * 40, [original])
    shifted = _finding(tmp_path, line=20)

    existing = quality._categorize(
        "ruff", [shifted], baseline, quality.ChangedScope({}, frozenset(), {}),
    )
    introduced = quality._categorize(
        "ruff", [shifted], baseline, quality.ChangedScope({"src/a.py": ((20, 20),)}, frozenset(), {}),
    )

    assert existing["existing"] == [shifted]
    assert not existing["introduced"]
    assert introduced["introduced"] == [shifted]


def test_added_file_and_rename_attribution(tmp_path: Path) -> None:
    old = _finding(tmp_path, "src/old.py", 1)
    baseline = quality._baseline_payload("ruff", "1.2.3", "a" * 40, [old])
    renamed = _finding(tmp_path, "src/new.py", 1)
    added = _finding(tmp_path, "src/added.py", 1)
    changed = quality.ChangedScope({}, frozenset({"src/added.py"}), {"src/new.py": "src/old.py"})

    categorized = quality._categorize("ruff", [renamed, added], baseline, changed)

    assert categorized["existing"] == [renamed]
    assert categorized["introduced"] == [added]


def test_git_scope_combines_committed_staged_unstaged_untracked_rename_and_delete(tmp_path: Path) -> None:
    base = _repository(tmp_path)
    (tmp_path / "src/a.py").write_text("one\nchanged\nthree\n", encoding="utf-8")
    _git(tmp_path, "add", "src/a.py")
    _git(tmp_path, "commit", "-q", "-m", "committed")
    (tmp_path / "src/a.py").write_text("one\nchanged\nunstaged\n", encoding="utf-8")
    (tmp_path / "src/staged.py").write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", "src/staged.py")
    _git(tmp_path, "mv", "src/rename.py", "src/renamed.py")
    _git(tmp_path, "rm", "-q", "src/delete.py")
    (tmp_path / "src/untracked.py").write_text("untracked\n", encoding="utf-8")

    changed = quality._changed_scope(tmp_path, base)

    assert changed.ranges["src/a.py"] == ((2, 3),)
    assert {"src/staged.py", "src/untracked.py"}.issubset(changed.created)
    assert changed.renamed_from == {"src/renamed.py": "src/rename.py"}
    assert "src/delete.py" not in changed.ranges


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(python_target="3.12"), "target or scope mismatch"),
        (lambda payload: payload["findings"].append(payload["findings"][0]), "duplicate"),
        (lambda payload: payload["findings"][0].update(path="../escape.py"), "unsafe path"),
    ],
)
def test_baseline_rejects_mismatch_duplicates_and_path_escape(tmp_path: Path, mutation, message: str) -> None:
    finding = _finding(tmp_path)
    payload = _baseline(tmp_path, finding)
    mutation(payload)
    (tmp_path / quality.BASELINE_PATHS["ruff"]).write_bytes(quality._canonical_bytes(payload))

    with pytest.raises(quality.RatchetError, match=message):
        quality._load_baseline(tmp_path, "ruff", "1.2.3")


def test_baseline_rejects_malformed_and_noncanonical_json(tmp_path: Path) -> None:
    destination = tmp_path / quality.BASELINE_PATHS["ruff"]
    destination.parent.mkdir()
    destination.write_text("{bad json", encoding="utf-8")
    with pytest.raises(quality.RatchetError, match="unreadable or malformed"):
        quality._load_baseline(tmp_path, "ruff", "1.2.3")
    payload = quality._baseline_payload("ruff", "1.2.3", "a" * 40, [])
    destination.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(quality.RatchetError, match="not canonical"):
        quality._load_baseline(tmp_path, "ruff", "1.2.3")


def test_missing_git_base_is_explicitly_unavailable_in_both_modes(tmp_path: Path) -> None:
    _repository(tmp_path)
    for mode in ("advisory", "ratchet"):
        result, exit_code = quality.check(root=tmp_path, base_ref="missing", mode=mode)
        assert exit_code == 2
        assert [item["status"] for item in result["checks"]] == ["unavailable", "unavailable"]
        assert all("Git evidence unavailable" in item["diagnostics"][0] for item in result["checks"])


def test_missing_timeout_nonzero_and_malformed_tool_results_are_unavailable(tmp_path: Path) -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("missing")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ruff", 1)

    with pytest.raises(quality.RatchetError, match="unavailable"):
        quality._run(["ruff"], root=tmp_path, timeout=1, runner=missing)
    with pytest.raises(quality.RatchetError, match="timed out"):
        quality._run(["ruff"], root=tmp_path, timeout=1, runner=timeout)

    for result, message in (
        (subprocess.CompletedProcess([], 2, "[]", "config"), "exited 2"),
        (subprocess.CompletedProcess([], 1, "not json", ""), "malformed JSON"),
        (subprocess.CompletedProcess([], 1, "[]", "Config contains unrecognized setting"), "invalid configuration"),
    ):
        with pytest.raises(quality.RatchetError, match=message):
            quality._tool_findings(
                tmp_path,
                "ruff",
                runner=lambda *_args, result=result, **_kwargs: result,
            )


def test_pyright_requires_the_canonical_mcp_analysis_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        quality.importlib.util,
        "find_spec",
        lambda name: None if name == "fastmcp" else object(),
    )

    with pytest.raises(quality.RatchetError, match=r"install \.\[dev\] and \.\[mcp\].*fastmcp"):
        quality._tool_findings(tmp_path, "pyright")


def test_advisory_reports_introduced_debt_while_ratchet_blocks(monkeypatch, tmp_path: Path) -> None:
    finding = _finding(tmp_path)
    baseline = quality._baseline_payload("ruff", "1.2.3", "a" * 40, [])
    monkeypatch.setattr(quality, "_resolve_base", lambda *_args, **_kwargs: ("a" * 40, "b" * 40))
    monkeypatch.setattr(
        quality,
        "_changed_scope",
        lambda *_args, **_kwargs: quality.ChangedScope({"src/a.py": ((2, 2),)}, frozenset(), {}),
    )
    monkeypatch.setattr(quality, "_scope_digest", lambda _root: "sha256:" + "c" * 64)
    monkeypatch.setattr(quality, "_tool_version", lambda *_args, **_kwargs: "1.2.3")
    monkeypatch.setattr(quality, "_load_baseline", lambda *_args, **_kwargs: (baseline, "sha256:" + "d" * 64))
    monkeypatch.setattr(quality, "_tool_findings", lambda *_args, **_kwargs: ([finding], 1))

    advisory, advisory_code = quality.check(root=tmp_path, mode="advisory")
    ratchet, ratchet_code = quality.check(root=tmp_path, mode="ratchet")

    assert advisory_code == 0
    assert ratchet_code == 1
    assert [item["status"] for item in advisory["checks"]] == ["fail", "fail"]
    assert ratchet["checks"][0]["findings"]["introduced"] == [finding]


def test_check_never_rewrites_baseline(monkeypatch, tmp_path: Path) -> None:
    finding = _finding(tmp_path)
    _baseline(tmp_path, finding)
    destination = tmp_path / quality.BASELINE_PATHS["ruff"]
    before = destination.read_bytes()
    monkeypatch.setattr(quality, "_resolve_base", lambda *_args, **_kwargs: ("a" * 40, "b" * 40))
    monkeypatch.setattr(quality, "_changed_scope", lambda *_args, **_kwargs: quality.ChangedScope({}, frozenset(), {}))
    monkeypatch.setattr(quality, "_scope_digest", lambda _root: "sha256:" + "c" * 64)
    monkeypatch.setattr(quality, "_tool_version", lambda *_args, **_kwargs: "1.2.3")
    monkeypatch.setattr(quality, "_tool_findings", lambda *_args, **_kwargs: ([], 0))
    pyright = quality._baseline_payload("pyright", "1.2.3", "a" * 40, [])
    pyright_path = tmp_path / quality.BASELINE_PATHS["pyright"]
    pyright_path.write_bytes(quality._canonical_bytes(pyright))

    quality.check(root=tmp_path, mode="advisory")

    assert destination.read_bytes() == before


def test_update_baseline_rejects_dirty_worktree(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(quality.RatchetError, match="clean Git worktree"):
        quality.update_baseline("ruff", root=tmp_path)
