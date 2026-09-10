"""Regression coverage for canonical release-policy checker scripts."""

import time
import tomllib
from email.message import EmailMessage
from importlib import import_module
from pathlib import Path
from runpy import run_path

import pytest


def _import_checker():
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_no_datakit_imports.py"
    return run_path(str(script))["check_file"]


def test_output_verifier_runs_current_contracts() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_outputs.py"
    verify = run_path(str(script))["main"]

    assert verify() == 0


def test_output_verifier_rejects_profile_error_contract(monkeypatch) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_outputs.py"
    verify = run_path(str(script))["main"]
    tool_wrappers = import_module("odibi_anchor._dispatcher._tool_wrappers")
    monkeypatch.setattr(
        tool_wrappers,
        "_profile_table_context",
        lambda *_args, **_kwargs: {
            "kind": "profile_table_context",
            "subject": "verification",
            "summary": "Profiling failed",
            "metrics": {},
        },
    )

    assert verify() == 1


def test_forbidden_import_checker_ignores_non_code_text(tmp_path: Path) -> None:
    candidate = tmp_path / "fixture.py"
    candidate.write_text(
        'payload = {"import": "from odibi.transformers import test_func"}\n'
        '# import datakit\n',
        encoding="utf-8",
    )

    assert _import_checker()(str(candidate)) == []


def test_forbidden_import_checker_finds_import_nodes_at_any_depth(tmp_path: Path) -> None:
    candidate = tmp_path / "imports.py"
    candidate.write_text(
        "from odibi.transformers import transform\n"
        "def load():\n"
        "    import datakit.runtime\n",
        encoding="utf-8",
    )

    violations = _import_checker()(str(candidate))

    assert [line for line, _text in violations] == [1, 3]
    assert all("import" in text for _line, text in violations)


def test_forbidden_import_checker_fails_closed_on_invalid_python(tmp_path: Path) -> None:
    candidate = tmp_path / "invalid.py"
    candidate.write_text("def broken(:\n", encoding="utf-8")

    assert _import_checker()(str(candidate))[0][1].startswith("SYNTAX ERROR:")


def _distribution_script() -> dict:
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_distribution.py"
    return run_path(str(script))


def test_distribution_advisory_unavailability_does_not_fail_local_checks() -> None:
    finish = _distribution_script()["_finish"]
    report, exit_code = finish(
        "2026-08-20T00:00:00Z",
        time.monotonic(),
        [{"id": "build", "required": True, "status": "pass"}],
        [
            {"id": "dependency-audit", "advisory": True, "status": "unavailable"},
            {"id": "sbom-generation", "advisory": True, "status": "unavailable"},
        ],
    )

    assert exit_code == 0
    assert report["status"] == "pass"
    assert [item["status"] for item in report["observations"]] == ["unavailable", "unavailable"]
    assert "advisory observations only" in report["claims"][1]


def test_distribution_metadata_rejects_unexpected_runtime_dependency() -> None:
    namespace = _distribution_script()
    verify_metadata = namespace["_verify_metadata"]
    error = namespace["DistributionError"]
    metadata = EmailMessage()
    metadata["Name"] = "odibi-anchor"
    metadata["Version"] = "0.9.0"
    metadata["Requires-Python"] = ">=3.11"
    metadata["Requires-Dist"] = "requests>=2"
    project = {
        "name": "odibi-anchor",
        "version": "0.9.0",
        "requires-python": ">=3.11",
        "dependencies": [],
        "optional-dependencies": {},
    }

    with pytest.raises(error, match="unconditional runtime requirement"):
        verify_metadata(metadata, project)


def test_distribution_secret_scan_checks_nested_values() -> None:
    contains_secret = _distribution_script()["_contains_secret_value"]

    assert contains_secret({"metadata": {"token": "not-for-an-sbom"}}) is True
    assert contains_secret({"metadata": {"component": "odibi-anchor"}}) is False


def test_quality_workflow_is_read_only_and_uses_maintained_entry_points() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "permissions:\n  contents: read" in workflow
    assert 'python-version: ["3.11", "3.12"]' in workflow
    assert "python scripts/full_verification.py" in workflow
    assert "python scripts/quality_ratchet.py check" in workflow
    assert 'python -m pip install ".[dev]"' in workflow
    assert 'python -m pip install ".[mcp]"' in workflow
    assert "python -m pip install --group quality" in workflow
    assert "setuptools>=68,<81" in pyproject["dependency-groups"]["quality"]
    assert "contents: write" not in workflow
    assert "secrets." not in workflow
