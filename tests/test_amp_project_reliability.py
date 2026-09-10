from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_orb_scripts_are_tracked_executable() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--stage", ".agents/setup", ".agents/resume"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    modes = {line.split()[3]: line.split()[0] for line in result.stdout.splitlines()}
    assert modes == {".agents/resume": "100755", ".agents/setup": "100755"}


def test_amp_instruction_and_gateway_contracts() -> None:
    guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    config = json.loads((ROOT / ".amp/settings.json").read_text(encoding="utf-8"))
    server = config["amp.mcpServers"]["odibi-anchor"]

    assert "ordinary substantive prompts" in guidance
    assert "anchor_execute" in guidance and "touched" in guidance
    assert server["includeTools"] == ["anchor_execute", "anchor_help"]
    assert server["env"]["ANCHOR_MCP_GATEWAY_ONLY"] == "1"
    assert server["env"]["ANCHOR_PROJECT_ROOT"] == "${AMP_WORKING_DIRECTORY}"


def test_external_amp_guide_has_portable_safety_contract() -> None:
    guide = (ROOT / "docs/guides/amp-project-integration.md").read_text(encoding="utf-8")
    for contract in (
        "odibi-anchor[mcp]==0.1.0",
        "<full-commit-sha>",
        "never install a mutable branch",
        "ANCHOR_PROJECT_ROOT",
        "ANCHOR_HOME",
        "outside that checkout",
        "ANCHOR_MCP_GATEWAY_ONLY",
        "AGENTS.md",
    ):
        assert contract in guide


def test_resume_is_executable_and_delegates_only_after_failed_health_check(tmp_path: Path) -> None:
    agents = tmp_path / ".agents"
    python = tmp_path / ".venv" / "bin" / "python"
    agents.mkdir()
    python.parent.mkdir(parents=True)
    (agents / "resume").write_bytes((ROOT / ".agents/resume").read_bytes())
    (agents / "resume").chmod((agents / "resume").stat().st_mode | stat.S_IXUSR)
    (agents / "setup").write_text("#!/bin/sh\necho setup >> \"$REPO_ROOT/calls\"\n", encoding="utf-8")
    (agents / "setup").chmod(0o755)
    python.write_text("#!/bin/sh\nexit \"${HEALTH_EXIT:-0}\"\n", encoding="utf-8")
    python.chmod(0o755)
    env = {**os.environ, "REPO_ROOT": str(tmp_path)}

    subprocess.run([str(agents / "resume")], cwd=tmp_path, env=env, check=True)
    assert not (tmp_path / "calls").exists()

    subprocess.run(
        [str(agents / "resume")],
        cwd=tmp_path,
        env={**env, "HEALTH_EXIT": "1"},
        check=True,
    )
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "setup\n"


def test_setup_short_circuits_before_install_and_lifecycle_when_healthy(tmp_path: Path) -> None:
    agents = tmp_path / ".agents"
    python = tmp_path / ".venv" / "bin" / "python"
    agents.mkdir()
    python.parent.mkdir(parents=True)
    (agents / "setup").write_bytes((ROOT / ".agents/setup").read_bytes())
    (agents / "setup").chmod(0o755)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)

    result = subprocess.run(
        [str(agents / "setup")], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert "already healthy" in result.stdout
    assert not (tmp_path / ".anchor_session_state.json").exists()
