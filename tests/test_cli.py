"""Focused tests for deterministic CLI transport behavior."""
import builtins
import io
import json
import math
import os
import subprocess
import sys

import pytest

from odibi_anchor import cli


def test_request_maps_json_to_core_dict_and_classifies_errors():
    calls = []

    def dispatch(action, *args, **kwargs):
        calls.append((action, args, kwargs))
        return {"text": "héllo"}

    response, code = cli._request(
        {"action": "help", "kwargs": {"output_format": "json"}}, dispatch, shared=False
    )
    assert code == 0
    assert calls == [("help", (), {"output_format": "dict"})]
    assert response["metadata"]["process_state_shared"] is False
    _, code = cli._request({"action": "missing"}, lambda *_a, **_k: (_ for _ in ()).throw(
        ValueError("Unknown anchor() action: 'missing'.")
    ), shared=True)
    assert code == cli.EXIT_ACTION
    _, code = cli._request({"action": "x"}, lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("BLOCKED: policy denied")
    ), shared=True)
    assert code == cli.EXIT_POLICY
    _, code = cli._request(
        {"action": "x"},
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("policy BLOCKED: but not runtime")),
        shared=True,
    )
    assert code == cli.EXIT_ACTION


def test_batch_boots_once_preserves_dispatcher_and_emits_json_lines(monkeypatch, capsys):
    boots = []
    state = []

    def dispatch(action, *args, **kwargs):
        state.append(action)
        return list(state)

    monkeypatch.setattr(cli, "_boot", lambda root: boots.append(root) or dispatch)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('{"action":"a"}\n{"action":"b"}\n'))
    assert cli.main(["batch"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert boots == [None]
    assert [line["result"] for line in lines] == [["a"], ["a", "b"]]
    assert all(line["metadata"]["process_state_shared"] for line in lines)


def test_exec_warning_json_stdout_input_and_bootstrap_codes(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_boot", lambda root: (lambda action, *args, **kwargs: "ok"))
    assert cli.main(["exec", "help"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "do not share session state" in captured.err
    assert cli.main(["exec", "help", "[]"]) == cli.EXIT_INPUT
    monkeypatch.setattr(cli, "_boot", lambda root: (_ for _ in ()).throw(RuntimeError("token=abc")))
    assert cli.main(["help"]) == cli.EXIT_BOOTSTRAP
    assert "abc" not in capsys.readouterr().out


def test_parser_help_documents_discovery_and_state(capsys):
    try:
        cli._parser().parse_args(["exec", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "do not share session state" in captured.err


def test_doctor_and_guidance_commands_do_not_boot_dispatcher(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_boot", lambda _root: pytest.fail("dispatcher booted"))
    monkeypatch.setattr("odibi_anchor.startup.doctor", lambda: {"kind": "startup_doctor"})
    assert cli.main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["kind"] == "startup_doctor"

    monkeypatch.setattr(
        "odibi_anchor.startup.install_guidance",
        lambda target: {"kind": "guidance_install", "target_root": target},
    )
    assert cli.main(["install-guidance", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["kind"] == "guidance_install"


def test_setup_host_and_portfolio_commands_do_not_boot_dispatcher(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_boot", lambda _root: pytest.fail("dispatcher booted"))
    monkeypatch.setattr(
        "odibi_anchor.host_setup.setup_host",
        lambda target, *, adapter: {"kind": "host_guidance_setup", "target": target, "adapter": adapter},
    )
    assert cli.main(["setup-host", "databricks", "--target", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["adapter"] == "databricks"

    assert cli.main(["portfolio", "schema"]) == 0
    schema = json.loads(capsys.readouterr().out)["result"]["schema"]
    assert schema["schema_version"] == 1


def test_portfolio_cli_scaffold_validate_resolve(tmp_path, capsys):
    config = tmp_path / "anchor.toml"
    target = tmp_path / "project"
    state = tmp_path / "state"
    target.mkdir()
    state.mkdir()
    args = [
        "portfolio", "scaffold", "--config", str(config), "--host", "local",
        "--adapter", "amp", "--target-root", str(target), "--project", "alpha",
        "--authority", "work", "--local-state-root", str(state),
        "--instruction-root", str(tmp_path),
    ]
    assert cli.main(args) == 0
    capsys.readouterr()

    assert cli.main(["portfolio", "validate", "--config", str(config), "--host", "local"]) == 0
    validation = json.loads(capsys.readouterr().out)["result"]["validation"]
    assert validation["status"] == "valid"
    assert cli.main([
        "portfolio", "resolve", "--config", str(config), "--host", "local", "--project", "alpha"
    ]) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["binding_inputs"]["project_id"] == "alpha"
    assert len(result["config_sha256"]) == 64


def test_state_cli_snapshots_lists_and_restores_without_boot(tmp_path, monkeypatch, capsys):
    import sqlite3

    monkeypatch.setattr(cli, "_boot", lambda _root: pytest.fail("dispatcher booted"))
    database = tmp_path / "live.db"
    durable = tmp_path / "durable"
    restored = tmp_path / "restored.db"
    artifacts = tmp_path / "projects"
    restored_artifacts = tmp_path / "restored-projects"
    durable.mkdir()
    artifacts.mkdir()
    (artifacts / "record.md").write_text("# Kept\n")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE facts(value TEXT)")
        connection.execute("INSERT INTO facts VALUES('kept')")

    common = ["--durable-root", str(durable), "--authority", "work"]
    assert cli.main([
        "state", "snapshot", *common, "--database", str(database),
        "--artifacts", str(artifacts),
    ]) == 0
    capsys.readouterr()
    assert cli.main(["state", "list", *common]) == 0
    assert len(json.loads(capsys.readouterr().out)["result"]["snapshots"]) == 1
    assert cli.main([
        "state", "restore", *common, "--database", str(restored),
        "--artifacts", str(restored_artifacts),
    ]) == 0
    capsys.readouterr()
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM facts").fetchone() == ("kept",)
    assert (restored_artifacts / "record.md").read_text() == "# Kept\n"


def test_state_list_cli_selects_databricks_transport(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_boot", lambda _root: pytest.fail("dispatcher booted"))
    monkeypatch.setattr(
        "odibi_anchor.durability.list_snapshots",
        lambda **kwargs: calls.append(kwargs) or {"snapshots": []},
    )

    assert cli.main([
        "state", "list", "--durable-root", "/Volumes/catalog/schema/anchor",
        "--authority", "work", "--databricks",
    ]) == 0

    assert calls == [{
        "durable_root": "/Volumes/catalog/schema/anchor",
        "authority_id": "work",
        "databricks": True,
    }]
    assert json.loads(capsys.readouterr().out)["result"] == {"snapshots": []}


def test_invalid_input_is_rejected_before_bootstrap(monkeypatch, capsys):
    boots = []
    monkeypatch.setattr(cli, "_boot", lambda root: boots.append(root))
    assert cli.main(["exec", "x", '{"args":null}']) == cli.EXIT_INPUT
    assert boots == []
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "input"


def test_cli_forces_dict_override_and_contains_noisy_dispatch_stdout(monkeypatch, capsys):
    calls = []

    def dispatch(action, *args, **kwargs):
        print("DISPATCH NOISE")
        calls.append(kwargs)
        return "h\u00e9llo"

    monkeypatch.setattr(cli, "_boot", lambda root: dispatch)
    assert cli.main(["--format", "json", "exec", "x", '{"kwargs":{"output_format":"text"}}']) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["result"] == "h\u00e9llo"
    assert calls == [{"output_format": "dict"}]
    assert "DISPATCH NOISE" in captured.err


def test_bootstrap_error_has_stable_category_and_redacted_separate_type(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_boot", lambda root: (_ for _ in ()).throw(cli.BootstrapError("token=shh")))
    assert cli.main(["help"]) == cli.EXIT_BOOTSTRAP
    error = json.loads(capsys.readouterr().out)["error"]
    assert error == {"category": "bootstrap", "message": "token=<redacted>", "type": "BootstrapError"}


def test_shell_boots_once_and_processes_requests_incrementally(monkeypatch, capsys):
    boots = []
    state = []

    def dispatch(action, *args, **kwargs):
        state.append(action)
        return list(state)

    monkeypatch.setattr(cli, "_boot", lambda root: boots.append(root) or dispatch)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('{"action":"first"}\nnot-json\n{"action":"second"}\n'))

    assert cli.main(["shell"]) == cli.EXIT_INPUT
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert boots == [None]
    assert records[0]["result"] == ["first"]
    assert records[1]["error"]["type"] == "input"
    assert records[2]["result"] == ["first", "second"]
    assert all(record.get("metadata", {}).get("process_state_shared", True) for record in records)


@pytest.mark.parametrize(
    ("input_text", "failures", "expected_code"),
    [
        ('{"action":"ok"}\n{"action":"policy"}\n', {}, cli.EXIT_POLICY),
        ('not-json\n{"action":"policy"}\n', {}, cli.EXIT_POLICY),
        (
            '{"action":"policy"}\n{"action":"generic"}\n',
            {"generic": RuntimeError("unexpected runtime failure")},
            cli.EXIT_ACTION,
        ),
        (
            '{"action":"policy"}\n{"action":"generic"}\n',
            {"generic": ValueError("unexpected value failure")},
            cli.EXIT_ACTION,
        ),
        (
            '{"action":"oversized-policy"}\n',
            {"oversized-policy": RuntimeError("BLOCKED: " + "x" * cli._MAX_JSON_BYTES)},
            cli.EXIT_ACTION,
        ),
    ],
)
def test_stateful_shell_aggregates_policy_and_worse_failures(
    monkeypatch, capsys, input_text, failures, expected_code
):
    def dispatch(action, *args, **kwargs):
        if action == "policy":
            raise RuntimeError("BLOCKED: source-change task requires a clean initial Git worktree")
        if action in failures:
            raise failures[action]
        return action

    monkeypatch.setattr(cli, "_boot", lambda _root: dispatch)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(input_text))

    assert cli.main(["shell"]) == expected_code
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]["ok"] is False
    if expected_code == cli.EXIT_POLICY:
        assert records[-1]["error"] == {
            "message": "BLOCKED: source-change task requires a clean initial Git worktree",
            "type": "RuntimeError",
        }
    elif "oversized-policy" in input_text:
        assert records[-1]["error"]["type"] == "SerializationError"


def test_stateful_shell_projects_safe_repetition_and_exact_task_threshold(
    monkeypatch, capsys, tmp_path
):
    from odibi_anchor.bootstrap import init

    anchor, _root, _manifest = init(root=str(tmp_path))
    capsys.readouterr()
    monkeypatch.setattr(cli, "_boot", lambda _root: anchor)
    requests = [
        *({"action": action} for action in (
            "status", "memory", "audit_history", "orient", "manifest", "tools",
        ) * 2),
        *({"action": "review"} for _ in range(8)),
    ]
    monkeypatch.setattr(
        cli.sys, "stdin",
        io.StringIO("".join(json.dumps(request) + "\n" for request in requests)),
    )

    assert cli.main(["shell"]) == cli.EXIT_POLICY
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    safe_records, task_records = records[:12], records[12:]
    assert all(record["ok"] is True for record in safe_records)
    assert all("Planning not detected" not in str(record) for record in safe_records)
    assert all(record["ok"] is True for record in task_records[:7])
    assert ["Planning not detected" in str(record) for record in task_records[:7]] == [
        False, False, True, True, True, True, True,
    ]
    assert task_records[7]["ok"] is False
    assert "8 task-required" in task_records[7]["error"]["message"]
    assert all(record["metadata"]["process_state_shared"] is True for record in records)


def test_exec_rejects_duplicate_action_before_bootstrap(monkeypatch, capsys):
    boots = []
    monkeypatch.setattr(cli, "_boot", lambda root: boots.append(root))

    assert cli.main(["exec", "status", '{"action":"help"}']) == cli.EXIT_INPUT
    assert boots == []
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "input"


def test_argument_errors_are_json_and_do_not_boot(monkeypatch, capsys):
    boots = []
    monkeypatch.setattr(cli, "_boot", lambda root: boots.append(root))

    assert cli.main(["unknown-command"]) == cli.EXIT_INPUT
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["type"] == "input"
    assert boots == []


def test_module_help_keeps_stdout_machine_clean():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, ["src", os.environ.get("PYTHONPATH")]))}
    result = subprocess.run(
        [sys.executable, "-m", "odibi_anchor.cli", "--help"],
        cwd=os.path.dirname(os.path.dirname(__file__)), env=env,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "odibi-anchor" in result.stderr


def test_shell_continues_after_pathological_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_boot", lambda root: (lambda action, *args, **kwargs: action))
    deep = '{"action":"x","value":' + "[" * 2_000 + "0" + "]" * 2_000 + "}"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(deep + '\n{"action":"status"}\n'))

    assert cli.main(["shell"]) == cli.EXIT_INPUT
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["error"]["type"] == "input"
    assert records[1]["result"] == "status"


def test_untransportable_results_become_strict_json_action_errors(monkeypatch, capsys):
    results = [math.nan, {1: "one", "two": 2}]
    recursive = []
    recursive.append(recursive)
    results.append(recursive)
    class BrokenString:
        def __str__(self):
            raise RuntimeError("must not escape")

    results.append(BrokenString())
    monkeypatch.setattr(cli, "_boot", lambda root: (lambda action, *args, **kwargs: results.pop(0)))

    for _ in range(4):
        assert cli.main(["exec", "x"]) == cli.EXIT_ACTION
        record = json.loads(capsys.readouterr().out)
        assert record["ok"] is False
        assert record["error"]["type"] == "SerializationError"
        assert record["error"]["message"] == "action result is not strict JSON-serializable"


def test_verify_delivery_routes_before_bootstrap_and_maps_policy_exit(monkeypatch, tmp_path, capsys):
    request = {
        "schema_version": 1,
        "target": {"worktree": str(tmp_path), "artifact_root": str(tmp_path)},
        "intended_pr_paths": [], "requirements": [], "attestations": [], "artifacts": [],
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(cli, "_boot", lambda _root: (_ for _ in ()).throw(AssertionError("must not boot")))

    assert cli.main(["verify-delivery", "--request", str(path)]) == cli.EXIT_POLICY
    assert json.loads(capsys.readouterr().out)["scope"] == "local_declared_policy"


def test_verify_delivery_rejects_duplicate_json_without_bootstrap(monkeypatch, tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    monkeypatch.setattr(cli, "_boot", lambda _root: (_ for _ in ()).throw(AssertionError("must not boot")))

    assert cli.main(["verify-delivery", "--request", str(path)]) == cli.EXIT_INPUT
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "input"


def test_verify_delivery_never_imports_bootstrap(monkeypatch, tmp_path, capsys):
    request = {"schema_version": 1, "target": {"worktree": str(tmp_path), "artifact_root": str(tmp_path)},
               "intended_pr_paths": [], "requirements": [], "attestations": [], "artifacts": []}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    imported = builtins.__import__

    def poison(name, *args, **kwargs):
        if name == "odibi_anchor.bootstrap":
            raise AssertionError("bootstrap import is forbidden")
        return imported(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poison)
    assert cli.main(["verify-delivery", "--request", str(path)]) == cli.EXIT_POLICY
    assert json.loads(capsys.readouterr().out)["scope"] == "local_declared_policy"


def test_verify_delivery_pathological_json_is_input_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("[" * 2_000 + "0" + "]" * 2_000))
    assert cli.main(["verify-delivery", "--request", "-"]) == cli.EXIT_INPUT
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "input"


def test_verify_delivery_bounds_input_before_full_read(monkeypatch, capsys):
    class BoundedInput:
        buffer = None

        def read(self, size):
            assert size == cli._MAX_JSON_BYTES + 1
            return b"x" * size

    stream = BoundedInput()
    stream.buffer = stream
    monkeypatch.setattr(cli.sys, "stdin", stream)
    assert cli.main(["verify-delivery", "--request", "-"]) == cli.EXIT_INPUT
    assert "exceeds" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_verify_delivery_maps_success_and_output_failure(monkeypatch, tmp_path, capsys):
    from odibi_anchor import delivery

    path = tmp_path / "request.json"
    path.write_text("{}", encoding="utf-8")

    class Result:
        def to_dict(self):
            return {"eligible_under_declared_policy": True}

    monkeypatch.setattr(delivery, "verify_delivery", lambda _request: Result())
    assert cli.main(["verify-delivery", "--request", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["eligible_under_declared_policy"] is True

    monkeypatch.setattr(delivery, "verify_delivery",
                        lambda _request: (_ for _ in ()).throw(delivery.OutputLimitError("too large")))
    assert cli.main(["verify-delivery", "--request", str(path)]) == cli.EXIT_ACTION
    output = capsys.readouterr().out
    assert len(output.encode("utf-8")) <= cli._MAX_JSON_BYTES
    assert json.loads(output)["error"]["type"] == "OutputLimitError"


def test_ordinary_help_does_not_import_delivery(monkeypatch, capsys):
    imported = builtins.__import__

    def poison(name, *args, **kwargs):
        if name == "odibi_anchor.delivery":
            raise AssertionError("ordinary help must not import delivery")
        return imported(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poison)
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == ""


def test_transport_overflow_is_always_an_action_error(capsys):
    assert cli._emit("x" * cli._MAX_JSON_BYTES, cli.EXIT_POLICY) == cli.EXIT_ACTION
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "SerializationError"
