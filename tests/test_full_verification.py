"""Focused contracts for the canonical full-verification orchestrator."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "full_verification.py"
SPEC = importlib.util.spec_from_file_location("full_verification_test_module", SCRIPT)
assert SPEC and SPEC.loader
full = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = full
SPEC.loader.exec_module(full)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["python"], returncode, stdout, stderr)


def _quality_payload(status: str = "pass") -> str:
    return json.dumps({
        "schema_version": 1,
        "mode": "advisory",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "checks": [
            {
                "id": f"static-{tool}",
                "tool": tool,
                "status": status,
                "tool_version": "1.2.3",
                "scope_digest": "sha256:" + "c" * 64,
                "baseline_digest": "sha256:" + "d" * 64,
                "findings": {"existing": [{}], "introduced": [], "unknown": []},
                "diagnostics": [],
            }
            for tool in ("ruff", "pyright")
        ],
    })


def test_legacy_run_script_uses_argv_without_shell(monkeypatch) -> None:
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return _completed()

    monkeypatch.setattr(full.subprocess, "run", fake_run)

    assert full.run_script("verify_outputs.py") == 0
    assert seen["command"] == [sys.executable, str(full.SCRIPTS_DIR / "verify_outputs.py")]
    assert "shell" not in seen["kwargs"]


def test_main_retains_order_and_atomically_writes_report(monkeypatch, tmp_path: Path) -> None:
    seen = []

    def fake_execute(check_id, label, script, args, *, timeout):
        seen.append((check_id, script, list(args), timeout))
        return {"id": check_id, "label": label, "status": "pass", "exit_code": 0}

    monkeypatch.setattr(full, "_execute", fake_execute)
    report_path = tmp_path / "nested" / "report.json"

    assert full.main([
        "--mode", "advisory", "--base-ref", "origin/main", "--pytest-workers", "2",
        "--json-out", str(report_path),
    ]) == 0

    assert [item[0] for item in seen] == [
        "forbidden-imports", "quality-ratchet", "tests", "output-contracts", "distribution",
    ]
    assert seen[1][2] == [
        "check", "--mode", "advisory", "--format", "json", "--base-ref", "origin/main",
    ]
    assert seen[2][2] == ["-n", "2", "tests/"]
    assert seen[4][2] == ["--format", "json"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert not list(report_path.parent.glob(f".{report_path.name}.*"))


@pytest.mark.parametrize("bad_status", ["fail", "unavailable"])
def test_one_failed_or_unavailable_check_makes_suite_nonzero(monkeypatch, bad_status: str) -> None:
    def fake_execute(check_id, label, _script, _args, *, timeout):
        del timeout
        status = bad_status if check_id == "tests" else "pass"
        return {"id": check_id, "label": label, "status": status, "exit_code": 1 if status == "fail" else None}

    monkeypatch.setattr(full, "_execute", fake_execute)

    assert full.main([]) == 1


def test_negative_pytest_worker_count_is_rejected() -> None:
    with pytest.raises(SystemExit, match="zero or greater"):
        full.main(["--pytest-workers", "-1"])


def test_execute_timeout_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "slow.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(full, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(full, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        full.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("python", 1)),
    )

    result = full._execute("slow", "Slow", "slow.py", [], timeout=1)

    assert result["status"] == "unavailable"
    assert result["exit_code"] is None
    assert result["diagnostic"] == "command timed out after 1s"


def test_advisory_debt_remains_visible_without_failing(monkeypatch, tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "quality_ratchet.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(full, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(full, "REPO_ROOT", tmp_path)
    payload = json.loads(_quality_payload("fail"))
    for item in payload["checks"]:
        item["findings"]["existing"] = []
        item["findings"]["introduced"] = [{"path": "src/new.py"}]
    monkeypatch.setattr(full.subprocess, "run", lambda *_args, **_kwargs: _completed(0, json.dumps(payload), ""))

    result = full._execute(
        "quality-ratchet", "Quality", "quality_ratchet.py", ["check", "--mode", "advisory"], timeout=10,
    )

    assert result["status"] == "pass"
    assert [item["finding_counts"]["introduced"] for item in result["details"]["checks"]] == [1, 1]


@pytest.mark.parametrize(
    ("check_id", "stdout"),
    [
        ("quality-ratchet", "not json"),
        ("tests", "{}"),
        ("distribution", json.dumps({"schema_version": 1, "status": "mystery"})),
    ],
)
def test_malformed_decisive_result_is_unavailable(monkeypatch, tmp_path: Path, check_id: str, stdout: str) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "check.py"
    script.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(full, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(full, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(full.subprocess, "run", lambda *_args, **_kwargs: _completed(0, stdout, ""))

    result = full._execute(check_id, "Check", "check.py", [], timeout=10)

    assert result["status"] == "unavailable"
    assert "parse_error" in result["details"]
