"""Focused startup qualification against a freshly installed candidate wheel."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def installed_candidate(tmp_path_factory):
    root = tmp_path_factory.mktemp("mcp-installed")
    wheels = root / "wheels"
    installed = root / "site-packages"
    repository = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheels), str(repository)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(installed),
         str(next(wheels.glob("*.whl")))],
        check=True, capture_output=True, text=True, timeout=120,
    )
    return installed


@pytest.mark.parametrize("configuration", ["default", "target", "conflicting-cwd", "environment"])
@pytest.mark.parametrize("binding", ["explicit", "target_match", "legacy_selector"])
def test_installed_binding_uses_target_config(installed_candidate, tmp_path, configuration, binding):
    target = tmp_path / "target"
    cwd = tmp_path / "unrelated-cwd"
    state = tmp_path / "state"
    if configuration == "default":
        state = tmp_path / ("local-app-data" if os.name == "nt" else "xdg") / "odibi-anchor"
    target.mkdir()
    cwd.mkdir()
    state.mkdir(parents=True)
    if configuration != "default":
        (target / ".anchor_config.json").write_text(json.dumps({
            "environment": {"profiles": {"ci": {
                "platform": "ci", "anchor_root": str(tmp_path / "overridden" if configuration == "environment" else state),
                "framework_root": str(target), "home": str(target),
            }}},
        }), encoding="utf-8")
    if configuration == "conflicting-cwd":
        (cwd / ".anchor_config.json").write_text(json.dumps({
            "environment": {"profiles": {"ci": {
                "platform": "ci", "anchor_root": str(tmp_path / "wrong-state"),
            }}},
        }), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("ANCHOR_", "_CW_", "CW_"))}
    environment.update({
        "PYTHONPATH": str(installed_candidate), "PYTHONDONTWRITEBYTECODE": "1",
        "ANCHOR_PROJECT_ID": "explicit", "ANCHOR_PROJECT_ROOT": str(target), "CI": "1",
        "HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "LOCALAPPDATA": str(tmp_path / "local-app-data"),
    })
    if configuration == "environment":
        environment["ANCHOR_HOME"] = str(state)
    script = '''
import json, os
from odibi_anchor._dispatcher._project import project_action
project_action(os.environ["STATE"], "create", name="explicit", target=os.environ["ANCHOR_PROJECT_ROOT"], output_format="dict")
'''
    registered = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=cwd,
        env={**environment, "STATE": str(state)}, capture_output=True, text=True, timeout=60,
    )
    assert registered.returncode == 0, registered.stderr
    if binding != "explicit":
        environment.pop("ANCHOR_PROJECT_ID")
    if binding == "legacy_selector":
        environment.pop("ANCHOR_PROJECT_ROOT")
        environment["ANCHOR_ALLOW_LEGACY_SELECTOR"] = "1"
        environment["ANCHOR_HOME"] = str(state)
    script = '''
import asyncio, json, os
from pathlib import Path
from fastmcp import Client
import odibi_anchor.mcp_server as server

assert Path(server.__file__).is_relative_to(Path(os.environ["PYTHONPATH"]))

async def status():
    async with Client(server.mcp) as client:
        result = await client.call_tool("anchor_execute", {"action": "status", "response_version": 2})
        return json.loads(result.content[0].text)

first = asyncio.run(status())
if not first["ok"]:
    print(json.dumps(first))
else:
    os.environ["ANCHOR_PROJECT_ROOT"] = "/mutated-after-start"
    os.environ["ANCHOR_PROJECT_ID"] = "mutated-after-start"
    os.chdir(Path.home().parent)
    second = asyncio.run(status())
    assert second["ok"], second
    for key in ("route_binding", "target_root", "artifact_root"):
        assert first["result"]["runtime"][key] == second["result"]["runtime"][key], key
    print(json.dumps(first))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=cwd,
        env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    output = json.loads(result.stdout)
    assert output["ok"] is True, output
    runtime = output["result"]["runtime"]
    assert runtime["route_binding"]["project_id"] == "explicit"
    assert runtime["route_binding"]["binding_source"] == binding
    assert Path(runtime["target_root"]) == target.resolve()
    assert Path(runtime["artifact_root"]).is_relative_to(state.resolve())
