"""Tests for the MCP server layer (src/odibi_anchor/mcp_server.py).

The contract test in test_dispatcher.py checks that MCP tool names map to real
anchor() actions, but nothing here exercised the MCP-only glue: the _resolve /
_resolve_or_passthrough / _passthrough / _fmt helpers, and actually *calling* a
tool function end-to-end. These tests cover that surface.

FastMCP's @mcp.tool() returns the original function, so the tools are directly
callable. _boot() lazily bootstraps anchor() against ANCHOR_PROJECT_ROOT — the fixture
points it at an isolated tmp root so the repo's tracked .agent_memory.db and
session state are never touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import odibi_anchor.mcp_server as mcp_server
from odibi_anchor._utils._output_hints import estimate_tokens

_INVALID_RESPONSE_VERSIONS = (
    True, False, 1.0, 2.0, "1", "2", "1.0", "2.0", None, [], {}, "invalid",
)


@pytest.fixture(autouse=True)
def _reset_runtime_route_binding(monkeypatch):
    """Keep each test independent from MCP's intentional process-lifetime binding."""
    monkeypatch.setattr(mcp_server, "_ROUTE_BINDING", None)

# ─── Pure helpers ────────────────────────────────────────────────────────────


def test_default_server_exposes_only_governed_gateway_tools() -> None:
    script = """
import asyncio
import json
from odibi_anchor.mcp_server import mcp

tools = asyncio.run(mcp.list_tools())
print(json.dumps(sorted(tool.name for tool in tools)))
"""
    repo_root = Path(__file__).resolve().parents[1]
    python_path = os.pathsep.join(
        [str(repo_root / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    env = {
        **os.environ,
        "PYTHONPATH": python_path,
    }
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(completed.stdout) == ["anchor_execute", "anchor_help"]


def test_server_binding_is_explicit_target_checked_and_legacy_opt_in(
    tmp_path, monkeypatch,
):
    from odibi_anchor._dispatcher._project import project_action

    state = tmp_path / "state"
    alpha_target = tmp_path / "alpha-target"
    beta_target = tmp_path / "beta-target"
    state.mkdir()
    alpha_target.mkdir()
    beta_target.mkdir()
    project_action(state, "create", name="alpha", target=alpha_target, output_format="dict")
    project_action(state, "create", name="beta", target=beta_target, output_format="dict")
    monkeypatch.setenv("ANCHOR_RUNTIME_INSTANCE_ID", "mcp-test-runtime")
    monkeypatch.setenv("ANCHOR_PROJECT_ID", "alpha")

    with pytest.raises(ValueError, match="conflicts with target hint"):
        mcp_server._resolve_server_binding(str(state), str(beta_target))

    explicit = mcp_server._resolve_server_binding(str(state), str(alpha_target))
    assert explicit.project_id == "alpha"
    assert explicit.binding_source == "explicit"
    assert explicit.runtime_instance_id == "mcp-test-runtime"

    monkeypatch.delenv("ANCHOR_PROJECT_ID")
    matched = mcp_server._resolve_server_binding(str(state), str(alpha_target))
    assert matched.project_id == "alpha"
    assert matched.binding_source == "target_match"

    with pytest.raises(RuntimeError, match="legacy selector fallback is disabled"):
        mcp_server._resolve_server_binding(str(state), None)
    monkeypatch.setenv("ANCHOR_ALLOW_LEGACY_SELECTOR", "1")
    legacy = mcp_server._resolve_server_binding(str(state), None)
    assert legacy.project_id == "beta"
    assert legacy.binding_source == "legacy_selector"


def test_distinct_project_mcp_processes_share_cw_home(tmp_path):
    from odibi_anchor._dispatcher._project import project_action

    state = tmp_path / "state"
    alpha_target = tmp_path / "alpha-target"
    beta_target = tmp_path / "beta-target"
    state.mkdir()
    alpha_target.mkdir()
    beta_target.mkdir()
    project_action(state, "create", name="alpha", target=alpha_target, output_format="dict")
    project_action(state, "create", name="beta", target=beta_target, output_format="dict")

    script = textwrap.dedent(
        """
        import json
        import os
        import sys
        import time
        from pathlib import Path
        from odibi_anchor.mcp_server import anchor_execute

        ready, release, output = map(Path, sys.argv[1:])
        first = json.loads(anchor_execute("status"))
        ready.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 30
        while not release.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("release barrier timed out")
            time.sleep(0.02)
        os.environ["ANCHOR_PROJECT_ID"] = "mutated-after-start"
        os.environ["ANCHOR_PROJECT_ROOT"] = "/mutated-after-start"
        os.chdir(Path.home())
        second = json.loads(anchor_execute("status"))
        project = json.loads(anchor_execute("project", json.dumps({"arg0": "status"})))
        output.write_text(
            json.dumps({"first": first, "second": second, "project": project}),
            encoding="utf-8",
        )
        """
    )
    release = tmp_path / "release"
    processes = []
    fixtures = {}
    for project_id, target in (("alpha", alpha_target), ("beta", beta_target)):
        ready = tmp_path / f"{project_id}.ready"
        output = tmp_path / f"{project_id}.json"
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ANCHOR_HOME": str(state),
            "ANCHOR_PROJECT_ID": project_id,
            "ANCHOR_PROJECT_ROOT": str(target),
            "ANCHOR_RUNTIME_INSTANCE_ID": f"server-{project_id}",
        }
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(ready), str(release), str(output)],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        fixtures[project_id] = (target, ready, output)

    try:
        deadline = time.monotonic() + 30
        while not all(ready.exists() for _, ready, _ in fixtures.values()):
            exited = [process for process in processes if process.poll() is not None]
            if exited or time.monotonic() > deadline:
                details = [process.communicate(timeout=1) for process in processes]
                pytest.fail(f"servers did not reach live barrier: {details}")
            time.sleep(0.02)

        assert all(process.poll() is None for process in processes)
        (state / "workspace" / ".active_project").write_text(
            "../corrupt-selector\n",
            encoding="utf-8",
        )
        release.write_text("release", encoding="utf-8")
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        release.touch(exist_ok=True)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    for project_id, (target, _, output) in fixtures.items():
        result = json.loads(output.read_text(encoding="utf-8"))
        for phase in ("first", "second"):
            runtime = result[phase]["runtime"]
            binding = runtime["route_binding"]
            assert runtime["active_project"] == project_id
            assert Path(runtime["target_root"]) == target.resolve()
            assert binding["project_id"] == project_id
            assert binding["runtime_instance_id"] == f"server-{project_id}"
            assert binding["binding_source"] == "explicit"
            assert Path(binding["artifact_root"]) == (
                state / "workspace" / "projects" / project_id
            ).resolve()
        assert result["project"]["route_binding"]["project_id"] == project_id


class TestPassthrough:
    def test_skips_none_keeps_values(self):
        out = mcp_server._passthrough({}, a=1, b=None, c="x")
        assert out == {"a": 1, "c": "x"}

    def test_keeps_falsey_non_none(self):
        out = mcp_server._passthrough({}, zero=0, empty="", flag=False)
        assert out == {"zero": 0, "empty": "", "flag": False}

    def test_merges_into_existing(self):
        out = mcp_server._passthrough({"keep": 1}, add=2, skip=None)
        assert out == {"keep": 1, "add": 2}


class TestFmt:
    def test_dict_is_compact_json(self):
        s = mcp_server._fmt({"b": 1, "a": 2})
        # compact: no spaces after separators (token-efficient for the LLM)
        assert ", " not in s and ": " not in s
        assert json.loads(s) == {"b": 1, "a": 2}

    def test_non_serializable_falls_back_to_str(self):
        # default=str keeps it from raising on odd values
        s = mcp_server._fmt({"x": {1, 2, 3}})
        assert isinstance(s, str)

    def test_str_passthrough(self):
        assert mcp_server._fmt("already a string") == "already a string"


class TestTimingTelemetry:
    @staticmethod
    def _clock():
        current = -1_000_000

        def perf_counter_ns():
            nonlocal current
            current += 1_000_000
            return current

        return perf_counter_ns

    def test_exact_success_schema_and_base_response_metrics(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", lambda _action, **_kwargs: {"message": "héllo"})
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        text = mcp_server.anchor_execute("status", response_version=2, telemetry_version=1)
        response = json.loads(text)
        base_text = mcp_server._fmt({"ok": True, "result": {"message": "héllo"}})

        assert list(response) == ["ok", "result", "telemetry"]
        telemetry = response["telemetry"]
        assert list(telemetry) == [
            "schema_version", "clock", "scope", "server_total_ms", "stages_ms",
            "routing_reinitialization", "response", "excludes",
        ]
        assert telemetry["stages_ms"] == {
            "gateway_lock_wait": 1.0,
            "bootstrap": None,
            "normalization_resolution": 1.0,
            "dispatch": 1.0,
            "response_preparation": 1.0,
            "serialization": 1.0,
            "connection_cleanup": 1.0,
        }
        assert telemetry["server_total_ms"] == 13.0
        assert telemetry["routing_reinitialization"] == {
            "status": "not_needed", "elapsed_ms": None,
        }
        assert telemetry["response"] == {
            "basis": "v2_envelope_without_telemetry",
            "utf8_bytes": len(base_text.encode("utf-8")),
            "estimated_tokens": estimate_tokens(base_text),
        }
        assert telemetry["excludes"] == [
            "telemetry_encoding", "fastmcp_transport", "network", "client_harness",
        ]

    @pytest.mark.parametrize("value", _INVALID_RESPONSE_VERSIONS)
    def test_invalid_response_version_fails_before_side_effects(
        self, monkeypatch, tmp_path, value,
    ):
        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(mcp_server, "_ROOT", None)
        monkeypatch.setattr(mcp_server, "_boot", pytest.fail)
        monkeypatch.setattr(mcp_server, "_close_memory_connections", pytest.fail)
        monkeypatch.setenv("ANCHOR_PROJECT_ROOT", str(tmp_path / "project"))
        monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))

        with pytest.raises(ValueError, match="response_version"):
            mcp_server.anchor_execute("status", response_version=value)

        assert mcp_server._CW is None
        assert mcp_server._ROOT is None
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("value", _INVALID_RESPONSE_VERSIONS)
    def test_fastmcp_rejects_invalid_response_version_before_side_effects(
        self, monkeypatch, tmp_path, value,
    ):
        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(mcp_server, "_ROOT", None)
        monkeypatch.setattr(mcp_server, "_boot", pytest.fail)
        monkeypatch.setattr(mcp_server, "_close_memory_connections", pytest.fail)
        monkeypatch.setenv("ANCHOR_PROJECT_ROOT", str(tmp_path / "project"))
        monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))

        with pytest.raises(Exception, match="response_version"):
            asyncio.run(mcp_server.mcp.call_tool("anchor_execute", {
                "action": "status",
                "response_version": value,
            }))

        assert mcp_server._CW is None
        assert mcp_server._ROOT is None
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("value", [True, False, 0, 2, "1", 1.0])
    def test_invalid_options_fail_before_boot(self, monkeypatch, value):
        boot = pytest.fail
        monkeypatch.setattr(mcp_server, "_boot", boot)
        with pytest.raises(ValueError, match="telemetry_version"):
            mcp_server.anchor_execute("status", response_version=2, telemetry_version=value)

    @pytest.mark.parametrize("value", [True, False, 0, 2, "1", 1.0])
    def test_fastmcp_rejects_invalid_telemetry_without_dispatch(self, monkeypatch, value):
        monkeypatch.setattr(mcp_server, "_boot", pytest.fail)
        monkeypatch.setattr(mcp_server, "_close_memory_connections", pytest.fail)
        with pytest.raises(Exception, match="telemetry_version"):
            asyncio.run(mcp_server.mcp.call_tool("anchor_execute", {
                "action": "status",
                "response_version": 2,
                "telemetry_version": value,
            }))

    @pytest.mark.parametrize("value", ["verbose", True, 1, 1.0])
    def test_fastmcp_rejects_invalid_response_detail_without_dispatch(
        self, monkeypatch, value,
    ):
        monkeypatch.setattr(mcp_server, "_boot", pytest.fail)
        monkeypatch.setattr(mcp_server, "_close_memory_connections", pytest.fail)
        with pytest.raises(Exception, match="response_detail"):
            asyncio.run(mcp_server.mcp.call_tool("anchor_execute", {
                "action": "status",
                "response_detail": value,
            }))

    def test_fastmcp_schema_and_valid_strict_integer(self, monkeypatch):
        tool = asyncio.run(mcp_server.mcp.get_tool("anchor_execute"))
        assert tool is not None
        assert tool.parameters["properties"]["response_version"] == {
            "default": 1,
            "type": "integer",
            "description": "Response envelope version, 1 or 2.",
        }
        assert tool.parameters["properties"]["response_detail"] == {
            "default": "compact",
            "type": "string",
            "description": (
                "`compact` returns decision-critical task fields; `full`\n"
                "returns the complete task result. Non-task actions are unchanged."
            ),
        }
        assert tool.parameters["properties"]["telemetry_version"] == {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": None,
            "description": (
                "Explicit telemetry schema version. Version 1 requires\n"
                "response_version 2."
            ),
        }

        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", lambda *_args, **_kwargs: {"ok": True})
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)
        result = asyncio.run(mcp_server.mcp.call_tool("anchor_execute", {
            "action": "status",
            "response_version": 2,
            "response_detail": "full",
            "telemetry_version": 1,
        }))
        response = json.loads(result.content[0].text)
        assert response["telemetry"]["schema_version"] == 1

    @pytest.mark.parametrize("version", [1, 2])
    def test_fastmcp_preserves_valid_response_versions(self, monkeypatch, version):
        effects = []

        def dispatch(action, *_args, **_kwargs):
            effects.append(("dispatch", action))
            return {"response_version": version}

        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(mcp_server, "_boot", lambda: effects.append(("boot", None)) or dispatch)
        monkeypatch.setattr(
            mcp_server, "_close_memory_connections",
            lambda: effects.append(("cleanup", None)),
        )

        result = asyncio.run(mcp_server.mcp.call_tool("anchor_execute", {
            "action": "status",
            "response_version": version,
        }))
        content = result.content[0]
        assert content.type == "text"
        response = json.loads(content.text)

        if version == 1:
            assert response == {"response_version": 1}
        else:
            assert response == {"ok": True, "result": {"response_version": 2}}
        assert effects == [
            ("boot", None), ("dispatch", "status"), ("cleanup", None),
        ]

    def test_telemetry_requires_v2_before_boot(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_boot", pytest.fail)
        with pytest.raises(ValueError, match="requires response_version=2"):
            mcp_server.anchor_execute("status", telemetry_version=1)

    def test_caught_normalization_error_has_bounded_private_telemetry(self, monkeypatch):
        secret = "password=super-secret"
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", lambda _action: None)
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        response = json.loads(mcp_server.anchor_execute(
            "status", json.dumps([]) + secret, response_version=2, telemetry_version=1,
        ))

        assert response["ok"] is False
        assert response["telemetry"]["stages_ms"]["normalization_resolution"] == 1.0
        assert response["telemetry"]["stages_ms"]["dispatch"] is None
        assert response["telemetry"]["stages_ms"]["response_preparation"] is None
        assert secret not in json.dumps(response["telemetry"])

    def test_bootstrap_and_dispatch_errors_record_only_completed_stages(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(
            mcp_server,
            "_boot",
            lambda: (_ for _ in ()).throw(RuntimeError("token=bootstrap-secret")),
        )
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        bootstrap = json.loads(mcp_server.anchor_execute(
            "status", response_version=2, telemetry_version=1,
        ))
        assert list(bootstrap) == ["ok", "error", "telemetry"]
        assert list(bootstrap["telemetry"]) == [
            "schema_version", "clock", "scope", "server_total_ms", "stages_ms",
            "routing_reinitialization", "response", "excludes",
        ]
        assert bootstrap["error"]["message"] == "token=<redacted>"
        assert bootstrap["telemetry"]["stages_ms"] == {
            "gateway_lock_wait": 1.0,
            "bootstrap": 1.0,
            "normalization_resolution": None,
            "dispatch": None,
            "response_preparation": None,
            "serialization": 1.0,
            "connection_cleanup": 1.0,
        }

        monkeypatch.setattr(
            mcp_server,
            "_CW",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("dispatch failed")),
        )
        dispatched = json.loads(mcp_server.anchor_execute(
            "status", response_version=2, telemetry_version=1,
        ))
        assert dispatched["error"]["type"] == "RuntimeError"
        assert dispatched["telemetry"]["stages_ms"]["dispatch"] == 1.0
        assert dispatched["telemetry"]["stages_ms"]["response_preparation"] is None

    def test_lazy_bootstrap_success_is_timed(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(
            mcp_server,
            "_boot",
            lambda: lambda *_args, **_kwargs: {"booted": True},
        )
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        response = json.loads(mcp_server.anchor_execute(
            "status", response_version=2, telemetry_version=1,
        ))

        assert response["result"] == {"booted": True}
        assert response["telemetry"]["stages_ms"]["bootstrap"] == 1.0

    def test_preparation_error_is_timed_and_redacted(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", lambda *_args, **_kwargs: {"ok": True})
        monkeypatch.setattr(
            mcp_server,
            "_prepare_response",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("password=secret")),
        )
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        response = json.loads(mcp_server.anchor_execute(
            "status", response_version=2, telemetry_version=1,
        ))

        assert response["error"]["message"] == "password=<redacted>"
        assert response["telemetry"]["stages_ms"]["response_preparation"] == 1.0

    @pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
    def test_base_exceptions_propagate_after_cleanup(self, monkeypatch, failure):
        events = []
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(
            mcp_server,
            "_CW",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure()),
        )
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: events.append("cleanup"))

        with pytest.raises(failure):
            mcp_server.anchor_execute("status", response_version=2, telemetry_version=1)
        assert events == ["cleanup"]

    def test_serialization_and_cleanup_failures_propagate(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", lambda *_args, **_kwargs: {"ok": True})
        monkeypatch.setattr(
            mcp_server,
            "_fmt",
            lambda _value: (_ for _ in ()).throw(TypeError("serialization failed")),
        )
        cleaned = []
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: cleaned.append(True))
        with pytest.raises(TypeError, match="serialization failed"):
            mcp_server.anchor_execute("status", response_version=2, telemetry_version=1)
        assert cleaned == [True]

        monkeypatch.setattr(mcp_server, "_fmt", json.dumps)
        monkeypatch.setattr(
            mcp_server,
            "_close_memory_connections",
            lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
        )
        with pytest.raises(RuntimeError, match="cleanup failed"):
            mcp_server.anchor_execute("status", response_version=2, telemetry_version=1)

    def test_telemetry_overhead_is_bounded_independent_of_result_size(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        overheads = []
        for payload in ("x", "x" * 100_000):
            monkeypatch.setattr(mcp_server, "_CW", lambda *_args, value=payload, **_kwargs: value)
            base = mcp_server.anchor_execute("status", response_version=2)
            enriched = mcp_server.anchor_execute(
                "status", response_version=2, telemetry_version=1,
            )
            overheads.append(len(enriched.encode("utf-8")) - len(base.encode("utf-8")))

        assert abs(overheads[0] - overheads[1]) < 10

    def test_response_metrics_count_multibyte_utf8(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_CW", lambda *_args, **_kwargs: "é")
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)
        monkeypatch.setattr(
            mcp_server,
            "_fmt",
            lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

        response = json.loads(mcp_server.anchor_execute(
            "status", response_version=2, telemetry_version=1,
        ))
        base_text = '{"ok":true,"result":"é"}'

        assert len(base_text.encode("utf-8")) > len(base_text)
        assert response["telemetry"]["response"] == {
            "basis": "v2_envelope_without_telemetry",
            "utf8_bytes": len(base_text.encode("utf-8")),
            "estimated_tokens": estimate_tokens(base_text),
        }

    def test_telemetry_refresh_success_and_failure_preserve_routing(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        def stale(*_args, **_kwargs):
            return {"reinitialize_required": True}

        def fresh(*_args, **_kwargs):
            return {"routed": True}

        monkeypatch.setattr(mcp_server, "_CW", stale)
        monkeypatch.setattr(
            mcp_server,
            "_initialize_dispatcher",
            lambda *, reset: setattr(mcp_server, "_CW", fresh),
        )
        succeeded = json.loads(mcp_server.anchor_execute(
            "project", response_version=2, telemetry_version=1,
        ))
        assert succeeded["result"]["routing_refreshed"] is True
        assert succeeded["result"]["reinitialize_required"] is False
        assert succeeded["telemetry"]["routing_reinitialization"] == {
            "status": "succeeded", "elapsed_ms": 1.0,
        }

        attempts = 0

        def initialize(*, reset):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("credential=refresh-secret")
            assert reset is True
            mcp_server._CW = fresh
            return fresh

        monkeypatch.setattr(mcp_server, "_CW", stale)
        monkeypatch.setattr(mcp_server, "_ROOT", "stale-root")
        monkeypatch.setattr(mcp_server, "_initialize_dispatcher", initialize)
        failed = json.loads(mcp_server.anchor_execute(
            "project", response_version=2, telemetry_version=1,
        ))
        assert failed["ok"] is False
        assert failed["telemetry"]["routing_reinitialization"] == {
            "status": "failed", "elapsed_ms": 1.0,
        }
        assert "refresh-secret" not in json.dumps(failed["telemetry"])
        assert mcp_server._CW is None
        assert mcp_server._ROOT is None

        retried = json.loads(mcp_server.anchor_execute("status"))
        assert retried == {"routed": True}
        assert attempts == 2

    def test_refresh_success_and_failure_are_timed(self, monkeypatch):
        monkeypatch.setattr(mcp_server.time, "perf_counter_ns", self._clock())
        telemetry = mcp_server._McpTelemetry(0)
        monkeypatch.setattr(mcp_server, "_initialize_dispatcher", lambda **_kwargs: object())
        result = mcp_server._refresh_after_project_change(
            "project", {"reinitialize_required": True}, telemetry,
        )
        assert result["routing_refreshed"] is True
        assert telemetry.routing_status == "succeeded"
        assert telemetry.routing_elapsed_ms == 1.0

        telemetry = mcp_server._McpTelemetry(0)
        monkeypatch.setattr(
            mcp_server, "_initialize_dispatcher",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("credential-value")),
        )
        with pytest.raises(RuntimeError, match="next request will retry"):
            mcp_server._refresh_after_project_change(
                "project", {"reinitialize_required": True}, telemetry,
            )
        assert telemetry.routing_status == "failed"
        assert telemetry.routing_elapsed_ms == 1.0


# ─── DataFrame resolver ──────────────────────────────────────────────────────

class TestResolve:
    def test_reads_csv_to_dataframe(self, tmp_path):
        p = tmp_path / "t.csv"
        p.write_text("a,b\n1,2\n3,4\n")
        df = mcp_server._resolve(str(p))
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["a", "b"]
        assert len(df) == 2

    def test_caches_by_path(self, tmp_path):
        p = tmp_path / "c.csv"
        p.write_text("x\n1\n")
        first = mcp_server._resolve(str(p))
        second = mcp_server._resolve(str(p))
        assert first is second  # same cached object

    def test_unknown_extension_raises(self, tmp_path):
        p = tmp_path / "nope.txt"
        p.write_text("hello")
        with pytest.raises(ValueError, match="Cannot resolve"):
            mcp_server._resolve(str(p))

    def test_resolve_or_passthrough_file_to_df(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_text("k\n1\n")
        assert isinstance(mcp_server._resolve_or_passthrough(str(p)), pd.DataFrame)

    def test_resolve_or_passthrough_table_name_unchanged(self):
        # a Delta-style catalog name has no data-file extension -> passed through
        ref = "catalog.schema.my_table"
        assert mcp_server._resolve_or_passthrough(ref) == ref


# ─── End-to-end tool invocation ──────────────────────────────────────────────

@pytest.fixture
def booted(tmp_path, monkeypatch):
    """Boot the MCP anchor() against an isolated tmp root and reset the cache after."""
    from odibi_anchor._dispatcher._project import project_action

    state = tmp_path / "state"
    state.mkdir()
    project_action(
        state,
        "create",
        name="mcp-test",
        target=tmp_path,
        output_format="dict",
    )
    monkeypatch.setenv("ANCHOR_HOME", str(state))
    monkeypatch.delenv("ANCHOR_MEMORY_DB", raising=False)
    monkeypatch.setenv("ANCHOR_PROJECT_ID", "mcp-test")
    monkeypatch.setenv("ANCHOR_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_CW", None)
    monkeypatch.setattr(mcp_server, "_ROOT", None)
    mcp_server._table_cache.clear()
    yield
    mcp_server._CW = None
    mcp_server._ROOT = None
    mcp_server._ROUTE_BINDING = None
    mcp_server._table_cache.clear()


class TestToolInvocation:
    @staticmethod
    def _start_task():
        mcp_server.anchor_execute("status")
        mcp_server.anchor_execute("memory")
        mcp_server.anchor_execute("audit_history")
        mcp_server.anchor_execute("new_session", '{"name":"mcp-transport-test"}')
        mcp_server.anchor_execute(
            "task",
            json.dumps({
                "arg0": "MCP transport test",
                "goal": "Verify MCP transport compatibility",
                "mode": "implementation",
                "work_type": "operate",
                "execution_mode": "artifact_only",
                "known_facts": ["The MCP gateway delegates to anchor"],
                "constraints": ["Keep default response shape"],
                "acceptance_criteria": ["Learning request reaches dispatcher"],
            }),
        )

    def test_status_returns_contract_json_and_bootstrap_stdout_is_clean(self, booted, capsys):
        out = mcp_server.anchor_execute("status", None)
        parsed = json.loads(out)
        assert parsed["kind"] == "session_status"
        assert capsys.readouterr().out == ""

    def test_v1_v2_project_same_safe_repetition_and_exact_task_threshold(self, booted):
        safe_results = []
        for index, action in enumerate((
            "status", "memory", "audit_history", "orient", "manifest", "tools",
        ) * 2):
            version = 1 if index % 2 == 0 else 2
            payload = json.loads(mcp_server.anchor_execute(action, response_version=version))
            safe_results.append(payload if version == 1 else payload["result"])

        assert all("Planning not detected" not in str(result) for result in safe_results)

        task_results = []
        for attempt in range(1, 8):
            version = 1 if attempt % 2 else 2
            payload = json.loads(mcp_server.anchor_execute("review", response_version=version))
            task_results.append(payload if version == 1 else payload["result"])
        assert ["Planning not detected" in str(result) for result in task_results] == [
            False, False, True, True, True, True, True,
        ]

        blocked = json.loads(mcp_server.anchor_execute("review", response_version=2))
        assert blocked["ok"] is False
        assert blocked["error"]["type"] == "RuntimeError"
        assert "8 task-required" in blocked["error"]["message"]

    def test_profile_table_on_csv(self, booted, tmp_path):
        p = tmp_path / "orders.csv"
        p.write_text("order_id,amount\n1,10.0\n2,20.0\n2,20.0\n")
        out = mcp_server.anchor_execute("profile_table", json.dumps({"arg0": str(p), "subject": "orders"}))
        parsed = json.loads(out)
        assert parsed["kind"] in ("profile_table_context", "profile_table")
        assert parsed["metrics"]["row_count"] == 3

    def test_quality_gate_blocks_on_duplicate_keys(self, booted, tmp_path):
        p = tmp_path / "dup.csv"
        p.write_text("id,v\n1,a\n2,b\n2,c\n")
        out = mcp_server.anchor_execute("quality", json.dumps({"df": str(p), "keys": ["id"], "subject": "dup"}))
        parsed = json.loads(out)
        # duplicate id=2 should surface as a duplicate finding / not write-safe
        assert parsed["kind"] in ("quality_gate_context", "quality_gate")

    def test_learn_accepts_keyword_events(self, booted, tmp_path):
        self._start_task()
        events = [{"type": "discovery", "detail": "MCP preserves keyword session events"}]
        out = mcp_server.anchor_execute(
            "learn",
            json.dumps({"session_events": events, "db_path": str(tmp_path / "memory.db")}),
        )
        parsed = json.loads(out)
        assert parsed["metrics"]["events_processed"] == 1
        assert parsed["metrics"]["memories_added"] == 1

    def test_learn_accepts_positional_event_list(self, booted, tmp_path):
        self._start_task()
        events = [{"type": "discovery", "detail": "MCP positional events are normalized"}]
        out = mcp_server.anchor_execute(
            "learn",
            json.dumps({"arg0": events, "db_path": str(tmp_path / "memory.db")}),
        )
        parsed = json.loads(out)
        assert parsed["metrics"]["events_processed"] == 1

    def test_learn_without_events_is_read_only_noop(self, booted):
        self._start_task()
        parsed = json.loads(mcp_server.anchor_execute("learn", None))
        assert parsed["metrics"]["events_processed"] == 0
        assert parsed["metrics"]["memories_added"] == 0

    def test_gateway_rejects_non_object_args(self, booted):
        with pytest.raises(ValueError, match="JSON object"):
            mcp_server.anchor_execute("learn", json.dumps([]))

    def test_nested_action_keyword_is_distinct_from_dispatcher_action(self, monkeypatch):
        captured = {}

        def dispatcher(dispatcher_action, *args, **kwargs):
            captured.update({"action": dispatcher_action, "args": args, "kwargs": kwargs})
            return captured

        monkeypatch.setattr(mcp_server, "_boot", lambda: dispatcher)

        parsed = json.loads(mcp_server.anchor_execute(
            "convention",
            json.dumps({
                "action": "modify_function",
                "function_name": "_normalize_phases",
            }),
            response_version=2,
        ))

        assert parsed["ok"] is True
        assert captured == {
            "action": "convention",
            "args": (),
            "kwargs": {
                "action": "modify_function",
                "function_name": "_normalize_phases",
            },
        }

    def test_explicit_kwargs_preserve_positional_compatibility(self, monkeypatch):
        captured = {}

        def dispatcher(dispatcher_action, *args, **kwargs):
            captured.update({"action": dispatcher_action, "args": args, "kwargs": kwargs})
            return captured

        monkeypatch.setattr(mcp_server, "_boot", lambda: dispatcher)

        parsed = json.loads(mcp_server.anchor_execute(
            "convention",
            json.dumps({
                "arg0": "subject",
                "kwargs": {"action": "modify_function"},
            }),
            response_version=2,
        ))

        assert parsed["ok"] is True
        assert captured == {
            "action": "convention",
            "args": ("subject",),
            "kwargs": {"action": "modify_function"},
        }


class TestGatewayCompatibility:
    def test_test_runner_does_not_inherit_transport_stdin(self, monkeypatch, tmp_path):
        from odibi_anchor._dispatcher import _session_tools

        observed = {}
        proc = SimpleNamespace(
            args=["python", "-m", "pytest"], stdout="", stderr="", returncode=0
        )
        def run_pytest(*_args, **kwargs):
            observed.update(kwargs)
            return (
                {"passed": 1, "failed": 0, "errors": 0, "skipped": 0,
                 "duration_s": 0.01, "timed_out": False},
                proc,
            )
        monkeypatch.setattr(
            _session_tools,
            "run_pytest",
            run_pytest,
        )

        result = _session_tools._test_run(tmp_path, target="tests/example.py")

        assert result["metrics"]["exit_code"] == 0
        assert observed["timeout"] == 600
        assert observed["capture_output"] is True

    def test_test_runner_uses_structured_counts_not_progress_dots(self, monkeypatch, tmp_path):
        from odibi_anchor._dispatcher import _session_tools

        stdout = (
            "................. [100%]\n"
            "================ warnings summary ================\n"
            "..\\..\\site-packages\\_pytest\\config\\__init__.py:1464\n"
            "  C:\\Users\\test\\site-packages\\_pytest\\config\\__init__.py: warning\n"
        )
        proc = SimpleNamespace(
            args=["python", "-m", "pytest"], stdout=stdout, stderr="", returncode=0
        )
        monkeypatch.setattr(
            _session_tools,
            "run_pytest",
            lambda *_args, **_kwargs: (
                {"passed": 17, "failed": 0, "errors": 0, "skipped": 0,
                 "duration_s": 0.01, "timed_out": False},
                proc,
            ),
        )

        result = _session_tools._test_run(tmp_path, target="tests/example.py")

        assert result["metrics"]["passed"] == 17
        assert result["summary"].startswith("PASS: 17 passed")

    def test_project_change_refreshes_dispatcher_before_next_request(self, monkeypatch):
        calls = []

        def first_dispatch(action, *_args, **_kwargs):
            calls.append(("first", action))
            return {
                "active_project": "selected-project",
                "reinitialize_required": True,
                "suggested_next_actions": ["restart"],
            }

        def second_dispatch(action, *_args, **_kwargs):
            calls.append(("second", action))
            return {"active_project": "selected-project", "routed": True}

        monkeypatch.setattr(mcp_server, "_CW", first_dispatch)

        def initialize(*, reset):
            assert reset is False
            mcp_server._CW = second_dispatch
            return second_dispatch

        monkeypatch.setattr(mcp_server, "_initialize_dispatcher", initialize)

        project = json.loads(mcp_server.anchor_execute("project", '{"arg0":"use"}'))
        status = json.loads(mcp_server.anchor_execute("status"))

        assert project["reinitialize_required"] is False
        assert project["routing_refreshed"] is True
        assert project["task_state_reset"] is True
        assert project["orientation_required"] is True
        assert project["fresh_task_required"] is True
        assert "operating_protocol" not in project
        assert "anchor('orient')" in project["suggested_next_actions"][0]
        assert "restart" not in " ".join(project["suggested_next_actions"]).lower()
        assert status == {"active_project": "selected-project", "routed": True}
        assert calls == [("first", "project"), ("second", "status")]

    def test_project_refresh_uses_side_effect_free_protocol_snapshot(self, monkeypatch):
        calls = []

        def first_dispatch(action, *_args, **_kwargs):
            calls.append(("first", action))
            return {"reinitialize_required": True}

        def second_dispatch(action, *_args, **_kwargs):
            calls.append(("second", action))
            return {}

        second_dispatch._operating_protocol_snapshot = lambda: {
            "version": "1.0", "required_now": [{
                "id": "orient", "satisfy_with": {"route": "orient"},
            }],
        }
        second_dispatch._agent_context_snapshot = lambda: {
            "kind": "agent_context",
            "next_operation": {"copy_ready": 'anchor("orient")'},
        }
        monkeypatch.setattr(mcp_server, "_CW", first_dispatch)
        monkeypatch.setattr(
            mcp_server, "_initialize_dispatcher",
            lambda *, reset: setattr(mcp_server, "_CW", second_dispatch) or second_dispatch,
        )

        project = json.loads(mcp_server.anchor_execute("project", '{"arg0":"use"}'))

        assert project["orientation_required"] is True
        assert project["operating_protocol"]["required_now"][0]["id"] == "orient"
        assert project["agent_context"]["next_operation"]["copy_ready"] == 'anchor("orient")'
        assert calls == [("first", "project")]

    def test_failed_project_refresh_invalidates_stale_dispatcher_and_retries(self, monkeypatch):
        calls = []

        def stale_dispatch(action, *_args, **_kwargs):
            calls.append(("stale", action))
            return {"reinitialize_required": True}

        def fresh_dispatch(action, *_args, **_kwargs):
            calls.append(("fresh", action))
            return {"routed": True}

        attempts = 0

        def initialize(*, reset):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("refresh failed")
            assert reset is True
            mcp_server._CW = fresh_dispatch
            return fresh_dispatch

        monkeypatch.setattr(mcp_server, "_CW", stale_dispatch)
        monkeypatch.setattr(mcp_server, "_ROOT", "stale-root")
        monkeypatch.setattr(mcp_server, "_initialize_dispatcher", initialize)

        failed = json.loads(mcp_server.anchor_execute("project", response_version=2))
        retried = json.loads(mcp_server.anchor_execute("status"))

        assert failed["ok"] is False
        assert "persisted" in failed["error"]["message"]
        assert retried == {"routed": True}
        assert calls == [("stale", "project"), ("fresh", "status")]
        assert attempts == 2

    def test_read_only_project_result_does_not_refresh(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server,
            "_boot",
            lambda: lambda *_a, **_k: {"active_project": "current"},
        )
        refreshed = False

        def initialize(*, reset):
            nonlocal refreshed
            refreshed = True

        monkeypatch.setattr(mcp_server, "_initialize_dispatcher", initialize)

        assert json.loads(mcp_server.anchor_execute("project", '{"arg0":"status"}')) == {
            "active_project": "current",
        }
        assert refreshed is False

    def test_initialize_closes_handles_and_clears_table_cache(self, monkeypatch):
        calls = []
        binding = object()

        def dispatcher(*_args, **_kwargs):
            return None

        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(mcp_server, "_ROOT", None)
        monkeypatch.setattr(
            "odibi_anchor.bootstrap.init",
            lambda **_kwargs: (calls.append("init") or (dispatcher, "new-root", {})),
        )
        monkeypatch.setattr(
            mcp_server,
            "_close_memory_connections",
            lambda: calls.append("close"),
        )
        monkeypatch.setattr(
            mcp_server,
            "_resolve_server_binding",
            lambda *_args: binding,
        )
        mcp_server._table_cache["old.table"] = object()

        assert mcp_server._initialize_dispatcher(reset=False) is dispatcher
        assert calls == ["close", "init", "close"]
        assert mcp_server._ROOT == "new-root"
        assert mcp_server._ROUTE_BINDING is binding
        assert mcp_server._table_cache == {}

    def test_source_initialize_does_not_load_explicit_target_config(
        self,
        tmp_path,
        monkeypatch,
    ):
        target = (tmp_path / "target").resolve()
        source_home = (tmp_path / "source-home").resolve()
        target.mkdir()
        source_home.mkdir()
        runtime_paths = SimpleNamespace(source_checkout=True, anchor_home=source_home)
        dispatcher = object()
        binding = object()
        boot_calls = []
        binding_calls = []

        monkeypatch.setenv("ANCHOR_PROJECT_ROOT", str(target))
        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(mcp_server, "_ROOT", None)
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *args, **_kwargs: (
                boot_calls.append(args) or {"runtime_paths": runtime_paths}
            ),
        )
        monkeypatch.setattr(
            mcp_server,
            "_resolve_server_binding",
            lambda home, target_hint: binding_calls.append((home, target_hint)) or binding,
        )
        monkeypatch.setattr(
            "odibi_anchor.bootstrap.init",
            lambda **kwargs: (dispatcher, str(target), {})
        )
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        assert mcp_server._initialize_dispatcher(reset=False) is dispatcher
        assert boot_calls == [()]
        assert binding_calls == [(str(source_home), str(target))]
        assert mcp_server._ROUTE_BINDING is binding
        assert str(target) == mcp_server._ROOT

    def test_installed_initialize_uses_portable_bootstrap_target(self, tmp_path, monkeypatch):
        target = (tmp_path / "target").resolve()
        state = (tmp_path / "state").resolve()
        target.mkdir()
        runtime_paths = SimpleNamespace(
            source_checkout=False,
            resource_root=tmp_path / "resources",
            instructions_file=tmp_path / "resources" / ".assistant_instructions.md",
            anchor_home=state,
            skills_dir=tmp_path / "resources" / ".assistant" / "skills",
            tools_dir=tmp_path / "resources" / "tools",
        )
        effective_environment = {
            "runtime_paths": runtime_paths,
            "memory_db": str(state / "agent.db"),
            "skills_dir": str(runtime_paths.skills_dir),
            "tools_dir": str(runtime_paths.tools_dir),
        }
        from odibi_anchor._dispatcher._boot import _installed_boot_authority

        resolved = SimpleNamespace(
            runtime_paths=runtime_paths,
            canonical_target=target,
            memory_db=state / "agent.db",
            authority=_installed_boot_authority(effective_environment),
        )
        artifact_root = state / "workspace" / "projects" / "portable"
        binding = SimpleNamespace(artifact_root=str(artifact_root))

        def dispatcher(*_args, **_kwargs):
            return {
                "runtime": {
                    "artifact_root": str(artifact_root),
                    "target_root": str(target),
                },
            }

        calls = []
        observed_environment = {}
        original_environment = {
            name: os.environ.get(name)
            for name in ("ANCHOR_HOME", "ANCHOR_MEMORY_DB", "_CW_BOOT_CONFIG_ROOT")
        }

        def initialize(**kwargs):
            calls.append(kwargs)
            observed_environment.update(
                {
                    name: os.environ.get(name)
                    for name in (
                        "ANCHOR_PROJECT_ROOT",
                        "ANCHOR_HOME",
                        "ANCHOR_MEMORY_DB",
                        "_CW_BOOT_CONFIG_ROOT",
                    )
                }
            )
            return dispatcher, str(target), {}

        monkeypatch.setenv("ANCHOR_PROJECT_ROOT", str(target))
        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(mcp_server, "_ROOT", None)
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *_args, **_kwargs: {"runtime_paths": runtime_paths},
        )
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_installed_project_bootstrap",
            lambda value, environment: (
                calls.append((value, environment["ANCHOR_PROJECT_ROOT"])) or resolved
            ),
        )
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot._ENV",
            effective_environment,
        )
        monkeypatch.setattr(
            "odibi_anchor.bootstrap.init",
            initialize,
        )
        monkeypatch.setattr(
            mcp_server,
            "_resolve_server_binding",
            lambda home, target_hint: (
                calls.append((home, target_hint)) or binding
            ),
        )
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        assert mcp_server._initialize_dispatcher(reset=False) is dispatcher
        assert calls == [
            (str(target), str(target)),
            (str(state), str(target)),
            {"route_binding": binding, "output_format": "dict"},
        ]
        assert observed_environment == {
            "ANCHOR_PROJECT_ROOT": str(target),
            "ANCHOR_HOME": str(state),
            "ANCHOR_MEMORY_DB": str(resolved.memory_db),
            "_CW_BOOT_CONFIG_ROOT": str(target),
        }
        assert os.environ.get("ANCHOR_HOME") == original_environment["ANCHOR_HOME"]
        assert os.environ.get("ANCHOR_MEMORY_DB") == original_environment["ANCHOR_MEMORY_DB"]
        assert (
            os.environ.get("_CW_BOOT_CONFIG_ROOT")
            == original_environment["_CW_BOOT_CONFIG_ROOT"]
        )
        assert str(target) == mcp_server._ROOT

    def test_installed_initialize_rejects_effective_root_mismatch(self, tmp_path, monkeypatch):
        target = (tmp_path / "target").resolve()
        target.mkdir()
        runtime_paths = SimpleNamespace(
            source_checkout=False,
            resource_root=tmp_path / "resources",
            instructions_file=tmp_path / "resources" / ".assistant_instructions.md",
            anchor_home=tmp_path / "state",
            skills_dir=tmp_path / "resources" / ".assistant" / "skills",
            tools_dir=tmp_path / "resources" / "tools",
        )
        effective_environment = {
            "runtime_paths": runtime_paths,
            "memory_db": str(tmp_path / "state" / "agent.db"),
            "skills_dir": str(runtime_paths.skills_dir),
            "tools_dir": str(runtime_paths.tools_dir),
        }
        from odibi_anchor._dispatcher._boot import _installed_boot_authority

        resolved = SimpleNamespace(
            runtime_paths=runtime_paths,
            canonical_target=target,
            memory_db=tmp_path / "state" / "agent.db",
            authority=_installed_boot_authority(effective_environment),
        )
        binding = SimpleNamespace(
            artifact_root=str(tmp_path / "state" / "workspace" / "projects" / "portable")
        )

        def dispatcher(*_args, **_kwargs):
            return {
                "runtime": {
                    "artifact_root": str(runtime_paths.anchor_home),
                    "target_root": str(target),
                },
            }

        monkeypatch.setenv("ANCHOR_PROJECT_ROOT", str(target))
        monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "original-home"))
        monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "original-memory.db"))
        monkeypatch.setenv("_CW_BOOT_CONFIG_ROOT", str(tmp_path / "original-config"))
        monkeypatch.setattr(mcp_server, "_CW", None)
        monkeypatch.setattr(mcp_server, "_ROOT", None)
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *_args, **_kwargs: {"runtime_paths": runtime_paths},
        )
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_installed_project_bootstrap",
            lambda *_args, **_kwargs: resolved,
        )
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot._ENV",
            effective_environment,
        )
        monkeypatch.setattr(
            "odibi_anchor.bootstrap.init",
            lambda **_kwargs: (dispatcher, str(tmp_path / "wrong"), {}),
        )
        monkeypatch.setattr(
            mcp_server,
            "_resolve_server_binding",
            lambda *_args: binding,
        )
        monkeypatch.setattr(mcp_server, "_close_memory_connections", lambda: None)

        with pytest.raises(RuntimeError, match="effective runtime does not match"):
            mcp_server._initialize_dispatcher(reset=False)
        assert mcp_server._CW is None
        assert mcp_server._ROOT is None
        assert os.environ["ANCHOR_HOME"] == str(tmp_path / "original-home")
        assert os.environ["ANCHOR_MEMORY_DB"] == str(tmp_path / "original-memory.db")
        assert os.environ["_CW_BOOT_CONFIG_ROOT"] == str(tmp_path / "original-config")

    def test_task_response_is_compact_by_default_and_full_on_request(self, monkeypatch):
        full = {
            key: {"value": key}
            for key in mcp_server._COMPACT_TASK_KEYS
        }
        full.update({
            "plan": ["large plan"],
            "discovery": {"recommended_context_generators": ["many"]},
            "hints": {"prompting_hints": ["many"]},
            "handoff": {"prompt_brief": "large duplicate"},
        })
        monkeypatch.setattr(mcp_server, "_boot", lambda: lambda *_a, **_k: full)

        compact_text = mcp_server.anchor_execute("task")
        compact = json.loads(compact_text)
        full_text = mcp_server.anchor_execute("task", response_detail="full")

        assert set(mcp_server._COMPACT_TASK_KEYS) <= set(compact)
        assert compact["artifact_contract"] == full["artifact_contract"]
        assert compact["capture_guidance"] == full["capture_guidance"]
        assert compact["operating_protocol"] == full["operating_protocol"]
        assert compact["agent_context"] == full["agent_context"]
        assert compact["transport"]["response_detail"] == "compact"
        assert set(compact["transport"]["omitted_sections"]) == {
            "discovery", "handoff", "hints", "plan",
        }
        assert json.loads(full_text) == full
        assert len(compact_text) < len(full_text)

    def test_invalid_response_detail_is_rejected_before_dispatch(self, monkeypatch):
        dispatched = False

        def dispatch(*_args, **_kwargs):
            nonlocal dispatched
            dispatched = True

        monkeypatch.setattr(mcp_server, "_boot", lambda: dispatch)
        with pytest.raises(ValueError, match="response_detail"):
            mcp_server.anchor_execute("task", response_detail="verbose")
        assert dispatched is False

    def test_gateway_serializes_requests_before_closing_connections(self, monkeypatch, tmp_path):
        from odibi_anchor.codebase._memory_db import get_db

        db = str(tmp_path / "memory.db")
        opened = threading.Event()
        release = threading.Event()

        def dispatch(action, *_args, **_kwargs):
            if action == "status":
                conn = get_db(db)
                opened.set()
                assert release.wait(timeout=2)
                conn.execute("SELECT 1").fetchone()
            return {"action": action}

        monkeypatch.setattr(mcp_server, "_boot", lambda: dispatch)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(mcp_server.anchor_execute, "status")
            assert opened.wait(timeout=2)
            second = executor.submit(mcp_server.anchor_execute, "memory")
            assert not second.done()
            release.set()
            assert json.loads(first.result(timeout=2)) == {"action": "status"}
            assert json.loads(second.result(timeout=2)) == {"action": "memory"}

    def test_gateway_releases_memory_db_handles_after_every_request(self, monkeypatch, tmp_path):
        from odibi_anchor.codebase._memory_db import get_db

        db = str(tmp_path / "memory.db")

        def dispatch(*_args, **_kwargs):
            get_db(db).execute("SELECT 1").fetchone()
            return {"ok": True}

        monkeypatch.setattr(mcp_server, "_boot", lambda: dispatch)
        assert json.loads(mcp_server.anchor_execute("status")) == {"ok": True}

        replacement = tmp_path / "replacement.db"
        replacement.write_bytes((tmp_path / "memory.db").read_bytes())
        replacement.replace(tmp_path / "memory.db")

    def test_gateway_releases_memory_db_handles_after_errors(self, monkeypatch, tmp_path):
        from odibi_anchor.codebase._memory_db import get_db

        db = str(tmp_path / "memory.db")

        def dispatch(*_args, **_kwargs):
            get_db(db).execute("SELECT 1").fetchone()
            raise RuntimeError("failed after opening memory")

        monkeypatch.setattr(mcp_server, "_boot", lambda: dispatch)
        with pytest.raises(RuntimeError, match="failed after opening memory"):
            mcp_server.anchor_execute("status")

        replacement = tmp_path / "replacement.db"
        replacement.write_bytes((tmp_path / "memory.db").read_bytes())
        replacement.replace(tmp_path / "memory.db")

    def test_default_golden_bytes_and_top_level_shape(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_boot", lambda: lambda *_a, **_k: {"b": 1, "a": [2]})
        output = mcp_server.anchor_execute("status")
        assert output.encode("utf-8") == b'{"b":1,"a":[2]}'
        assert mcp_server.anchor_execute("status", response_version=1).encode("utf-8") == output.encode("utf-8")
        assert isinstance(json.loads(output), dict)
        assert "ok" not in json.loads(output)

    def test_default_exception_is_unwrapped_and_v2_error_is_redacted(self, monkeypatch):
        def fail(*_args, **_kwargs):
            raise ValueError("token=top-secret")

        monkeypatch.setattr(mcp_server, "_boot", lambda: fail)
        with pytest.raises(ValueError, match="top-secret"):
            mcp_server.anchor_execute("status")
        v2 = mcp_server.anchor_execute("status", response_version=2)
        assert v2.encode("utf-8") == (
            b'{"ok":false,"error":{"type":"ValueError","message":"token=<redacted>"}}'
        )
        assert json.loads(v2) == {
            "ok": False,
            "error": {"type": "ValueError", "message": "token=<redacted>"},
        }

    def test_v2_success_is_explicit_envelope(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_boot", lambda: lambda *_a, **_k: ["raw", "shape"])
        v2 = mcp_server.anchor_execute("status", response_version=2)
        assert v2.encode("utf-8") == b'{"ok":true,"result":["raw","shape"]}'
        assert json.loads(v2) == {
            "ok": True,
            "result": ["raw", "shape"],
        }

    def test_v2_envelopes_bootstrap_and_argument_errors(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server, "_boot",
            lambda: (_ for _ in ()).throw(RuntimeError("token=bootstrap-secret")),
        )
        assert json.loads(mcp_server.anchor_execute("status", response_version=2)) == {
            "ok": False,
            "error": {"type": "RuntimeError", "message": "token=<redacted>"},
        }
        monkeypatch.setattr(mcp_server, "_boot", lambda: lambda *_a, **_k: None)
        invalid = json.loads(mcp_server.anchor_execute("", response_version=2))
        assert invalid["ok"] is False
        assert invalid["error"]["type"] == "RequestError"

    def test_target_resolver_failure_preserves_original_value(self, monkeypatch):
        captured = {}

        def dispatch(_action, **kwargs):
            captured.update(kwargs)
            return {"target": kwargs["target"]}

        monkeypatch.setattr(mcp_server, "_boot", lambda: dispatch)
        monkeypatch.setattr(
            mcp_server, "_resolve_or_passthrough",
            lambda _value: (_ for _ in ()).throw(ValueError("unavailable")),
        )
        output = mcp_server.anchor_execute("status", '{"target":"missing.csv"}')
        assert json.loads(output) == {"target": "missing.csv"}
        assert captured["target"] == "missing.csv"

    def test_strict_normalization_rejects_gaps_and_duplicate_json_keys(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_boot", lambda: lambda *_a, **_k: None)
        with pytest.raises(ValueError, match="contiguous"):
            mcp_server.anchor_execute("status", '{"arg1":1}')
        with pytest.raises(ValueError, match="duplicate JSON object key"):
            mcp_server.anchor_execute("status", '{"arg0":1,"arg0":2}')

    def test_help_catalog_is_sourced_from_current_tables(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_boot", lambda: lambda *_a, **_k: None)
        parsed = json.loads(mcp_server.anchor_help())
        assert parsed["total_actions"] == sum(len(actions) for actions in mcp_server.ACTION_GROUPS.values())
        assert set(parsed["categories"]) == set(mcp_server.ACTION_GROUPS)
