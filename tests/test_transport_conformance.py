"""Transport conformance: one request must reach the dispatcher identically everywhere.

Each case is sent through the in-process dispatcher call, the CLI (`anchor exec`) and
the MCP gateway (`anchor_execute`). A recording dispatcher replaces the real one so the
test isolates what each transport delivers. The recorder echoes the action and
arguments it received, tagging every non-JSON value with its type, so a transport that
rewrites a path into a DataFrame (issue #28) produces a visibly different result.

Extend `CASES` with new requests and `TRANSPORTS` with new adapters. The CLI always
adds `output_format="dict"` (its JSON transport encoding); the recorder omits that one
keyword so results remain comparable, and MCP is asked for `response_detail="full"`
so its compact task projection does not hide the echo. MCP-only table resolution of
explicit table parameters is intentionally transport-specific and is covered in
test_mcp_server.py.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

import odibi_anchor.mcp_server as mcp_server
from odibi_anchor import cli

_TRANSPORT_ENCODING_KWARGS = frozenset({"output_format"})


def _describe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_describe(item) for item in value]
    if isinstance(value, dict):
        return {key: _describe(item) for key, item in value.items()}
    return {"non_json_value": type(value).__name__}


def _recording_dispatcher(action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "action": action,
        "args": _describe(list(args)),
        "kwargs": _describe({
            key: value for key, value in kwargs.items()
            if key not in _TRANSPORT_ENCODING_KWARGS
        }),
    }


def _in_process(action: str, args: list[Any], kwargs: dict[str, Any], _monkeypatch, _capsys):
    return _recording_dispatcher(action, *args, **kwargs)


def _cli_exec(action: str, args: list[Any], kwargs: dict[str, Any], monkeypatch, capsys):
    monkeypatch.setattr(cli, "_boot", lambda _root: _recording_dispatcher)
    code = cli.main(["exec", action, json.dumps({"args": args, "kwargs": kwargs})])
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    return output["result"]


def _mcp_gateway(action: str, args: list[Any], kwargs: dict[str, Any], monkeypatch, _capsys):
    monkeypatch.setattr(mcp_server, "_boot", lambda: _recording_dispatcher)
    monkeypatch.setattr(mcp_server, "_table_cache", {})
    output = json.loads(mcp_server.anchor_execute(
        action, json.dumps({"args": args, "kwargs": kwargs}),
        response_version=2, response_detail="full",
    ))
    assert output["ok"] is True, output
    return output["result"]


Transport = Callable[..., dict[str, Any]]
TRANSPORTS: dict[str, Transport] = {
    "in_process": _in_process,
    "cli_exec": _cli_exec,
    "mcp_gateway": _mcp_gateway,
}

# (case id, action, positional args, keyword args). "{root}" expands to a temporary
# directory holding real files, so readers that react to existing paths are exercised.
CASES: list[tuple[str, str, list[Any], dict[str, Any]]] = [
    ("touched_existing_json", "touched", ["{root}/results/manifest.json"], {}),
    ("touched_missing_json", "touched", ["benchmarks/results/2026-10/manifest.json"], {}),
    ("touched_existing_csv", "touched", ["{root}/results/rows.csv"], {"created": True}),
    ("touched_parquet", "touched", ["{root}/results/rows.parquet"], {}),
    ("touched_dotted_source", "touched", ["src/odibi_anchor/mcp_server.py"], {}),
    ("task_dotted_description", "task", ["Fix the parser. Then add tests."], {"goal": "Ship it."}),
    ("impact_target_json", "impact", [], {"target": "{root}/results/manifest.json"}),
    ("save_snap_path", "save_snap", [{"decisions": []}, "{root}/results/snap.json"], {}),
]


def _expand(value: Any, root: str) -> Any:
    if isinstance(value, str):
        return value.replace("{root}", root)
    if isinstance(value, list):
        return [_expand(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, root) for key, item in value.items()}
    return value


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Create real files and an active fake SparkSession that would rewrite dotted names."""
    pyspark = type(sys)("pyspark")
    pyspark_sql = type(sys)("pyspark.sql")
    session = SimpleNamespace(table=lambda name: SimpleNamespace(spark_table=name))
    pyspark_sql.SparkSession = SimpleNamespace(getActiveSession=lambda: session)
    pyspark.sql = pyspark_sql
    monkeypatch.setitem(sys.modules, "pyspark", pyspark)
    monkeypatch.setitem(sys.modules, "pyspark.sql", pyspark_sql)
    results = tmp_path / "results"
    results.mkdir()
    (results / "manifest.json").write_text('[{"path": "a.py", "sha": "x"}]')
    (results / "rows.csv").write_text("id,v\n1,a\n")
    (results / "rows.parquet").write_bytes(b"PAR1")
    (results / "snap.json").write_text('{"decisions": []}')
    return str(tmp_path)


@pytest.mark.parametrize(
    ("action", "args", "kwargs"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_path_like_arguments_conform_across_transports(
    action, args, kwargs, data_root, monkeypatch, capsys,
):
    args, kwargs = _expand(args, data_root), _expand(kwargs, data_root)
    expected = {"action": action, "args": args, "kwargs": kwargs}

    results = {
        name: transport(action, args, kwargs, monkeypatch, capsys)
        for name, transport in TRANSPORTS.items()
    }

    assert results == dict.fromkeys(TRANSPORTS, expected)
