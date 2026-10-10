"""Portfolio-authorized PROJECT.md repair and gate route-field protection (#29)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from odibi_anchor import cli
from odibi_anchor._dispatcher._descriptor_protection import check_descriptor_route_protection
from odibi_anchor._dispatcher._effects import build_static_action_contracts, resolve_invocation
from odibi_anchor._dispatcher._project import project_action, resolve_route_binding
from odibi_anchor.planning._task_policy import ManagedArtifactEntry
from odibi_anchor.portfolio import write_portfolio
from odibi_anchor.startup import prepare_portfolio_runtime, repair_portfolio_descriptor

PLAIN = "# Alpha\n\nTeam notes replaced the whole file.\n\n---\n\nMore notes: kept.\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _portfolio(tmp_path: Path, targets: dict[str, Path]) -> Path:
    config = tmp_path / "anchor.toml"
    write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "work", "trust_domain": "work"},
        "hosts": {"local": {"adapter": "amp", "local_state_root": str(tmp_path / "state")}},
        "projects": {name: {"targets": {"local": str(path)}} for name, path in targets.items()},
        "personas": {},
    })
    return config


@pytest.fixture
def deployment(tmp_path: Path) -> SimpleNamespace:
    """One portfolio-registered referenced project ``alpha`` with an intact descriptor."""
    target = tmp_path / "target"
    target.mkdir()
    config = _portfolio(tmp_path, {"alpha": target})
    prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")
    home = (tmp_path / "state").resolve()
    artifact = home / "workspace" / "projects" / "alpha"
    descriptor = artifact / "PROJECT.md"
    return SimpleNamespace(
        config=config, target=target.resolve(), home=home, artifact=artifact,
        descriptor=descriptor, intact=descriptor.read_bytes(),
    )


def _repair(deployment: SimpleNamespace, expected: str, approve: Any = False, **kwargs: Any) -> dict:
    return repair_portfolio_descriptor(
        config_path=deployment.config, host_id="local", project_id=kwargs.pop("project_id", "alpha"),
        expected_sha256=expected, approve=approve, **kwargs,
    )


def _damage(deployment: SimpleNamespace, text: str) -> str:
    deployment.descriptor.write_bytes(text.encode("utf-8"))
    return _sha(text.encode("utf-8"))


def test_damage_error_names_the_repair_when_a_portfolio_is_available(deployment) -> None:
    sha = _damage(deployment, PLAIN)

    with pytest.raises(ValueError) as caught:
        prepare_portfolio_runtime(config_path=deployment.config, host_id="local", project_id="alpha")

    exc: Any = caught.value
    assert exc.error_code == "managed_descriptor_damaged"
    assert exc.context["integrity_status"] == "missing_frontmatter"
    assert exc.context["supported_repair_available"] is True
    assert exc.context["owner_decision_required"] is True
    first = exc.next_operations[0]
    assert first["requires_owner"] is True
    assert first["arguments"] == {
        "config_path": str(deployment.config), "host_id": "local", "project_id": "alpha",
        "expected_sha256": sha, "approve": False,
    }
    assert first["copy_ready"] == (
        f"anchor portfolio repair-descriptor --config {deployment.config} --host local "
        f"--project alpha --expected-sha256 {sha}"
    )
    assert exc.next_operations[1]["copy_ready"].startswith("anchor('project', 'repair-descriptor', 'alpha'")
    assert deployment.descriptor.read_bytes() == PLAIN.encode()


def test_plain_markdown_repair_plans_then_writes_backup_receipt_and_boots(deployment) -> None:
    sha = _damage(deployment, PLAIN)
    expected_text = (
        f"---\nid: alpha\nproject_type: referenced\ntarget_root: {deployment.target}\n---\n" + PLAIN
    )

    plan = _repair(deployment, sha)

    assert plan["status"] == "plan" and plan["approved"] is False
    assert plan["mode"] == "frontmatter_prepended"
    assert plan["rendered_text"] == expected_text
    assert plan["rendered_sha256"] == _sha(expected_text.encode())
    assert plan["next_operations"][0]["arguments"]["approve"] is True
    assert deployment.descriptor.read_bytes() == PLAIN.encode()
    assert not (deployment.artifact / "archive" / "descriptor-backups").exists()

    repaired = _repair(deployment, sha, approve=True)

    assert repaired["status"] == "repaired" and repaired["verified"] is True
    assert deployment.descriptor.read_bytes() == expected_text.encode()
    backup = Path(repaired["backup_path"])
    assert backup.parent == deployment.artifact / "archive" / "descriptor-backups"
    assert re.fullmatch(rf"PROJECT\.\d{{8}}T\d{{12}}Z\.{sha[:12]}\.md", backup.name)
    assert backup.read_bytes() == PLAIN.encode()
    raw = Path(repaired["receipt_path"]).read_bytes()
    receipt = json.loads(raw)
    assert raw == (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert receipt["pre_sha256"] == sha
    assert receipt["post_sha256"] == _sha(expected_text.encode())
    assert receipt["route_fields"] == {
        "id": "alpha", "project_type": "referenced", "target_root": str(deployment.target),
    }
    ready = prepare_portfolio_runtime(config_path=deployment.config, host_id="local", project_id="alpha")
    assert ready["status"] == "ready" and ready["target_root"] == str(deployment.target)


@pytest.mark.parametrize("damage", [
    lambda text: "".join(line for line in text.splitlines(True) if not line.startswith("target_root:")),
    lambda text: text.replace("project_type: referenced\n", "project_type: referenced\nproject_type: managed\n"),
    lambda text: text.replace("\n---\n", "\n- stray\n---\n", 1),
    lambda text: text.replace("project_type: referenced\n", "project_type: shared\n"),
], ids=["missing_target_root", "duplicate_project_type", "nested_under_target", "invalid_project_type"])
def test_route_field_reconstruction_restores_the_generated_descriptor_exactly(deployment, damage) -> None:
    sha = _damage(deployment, damage(deployment.intact.decode()))

    repaired = _repair(deployment, sha, approve=True)

    assert repaired["mode"] == "route_fields_reconstructed"
    # Only route lines changed, so the result is byte-identical to the generated descriptor.
    assert deployment.descriptor.read_bytes() == deployment.intact


def test_malformed_non_route_frontmatter_falls_back_to_keeping_everything_as_body(deployment) -> None:
    damaged = deployment.intact.decode().replace("status: active\n", "status active\n")
    sha = _damage(deployment, damaged)

    plan = _repair(deployment, sha)

    assert plan["mode"] == "frontmatter_prepended"
    assert plan["rendered_text"].endswith(damaged)


def test_managed_project_whose_target_is_its_artifact_root(tmp_path: Path) -> None:
    home = (tmp_path / "state").resolve()
    home.mkdir()
    project_action(home, "create", name="alpha", output_format="dict")
    artifact = home / "workspace" / "projects" / "alpha"
    config = _portfolio(tmp_path, {"alpha": artifact})
    descriptor = artifact / "PROJECT.md"
    body = descriptor.read_text(encoding="utf-8").split("---\n", 2)[2]
    descriptor.write_text(body, encoding="utf-8")

    repaired = repair_portfolio_descriptor(
        config_path=config, host_id="local", project_id="alpha",
        expected_sha256=_sha(body.encode()), approve=True,
    )

    assert repaired["route_fields"]["project_type"] == "managed"
    binding = resolve_route_binding(home, project="alpha", target_hint=artifact, runtime_instance_id="t")
    assert binding is not None and binding.target_root == binding.artifact_root == str(artifact)
    assert descriptor.read_text(encoding="utf-8").endswith(body)


def _owner(deployment: SimpleNamespace, **overrides: Any) -> None:
    sentinel = deployment.artifact / "continuity" / "v1" / "OWNER.json"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    owner = {"schema_version": 1, "project_id": "alpha", "anchor_home": str(deployment.home),
             "artifact_root": str(deployment.artifact), "target_root": str(deployment.target)}
    sentinel.write_text(json.dumps({**owner, **overrides}), encoding="utf-8")


def test_matching_continuity_owner_allows_repair(deployment) -> None:
    _owner(deployment)
    sha = _damage(deployment, PLAIN)
    assert _repair(deployment, sha, approve=True)["status"] == "repaired"


@pytest.mark.parametrize(("setup", "classification"), [
    (lambda d: _damage(d, d.intact.decode().replace("id: alpha\n", "id: beta\n").replace(
        "project_type: referenced\n", "")), "identity_mismatch"),
    (lambda d: _damage(d, d.intact.decode().replace(str(d.target), "/elsewhere/alpha").replace(
        "project_type: referenced\n", "")), "portfolio_mismatch"),
    (lambda d: (_owner(d, target_root="/elsewhere/alpha"), _damage(d, PLAIN))[1], "portfolio_mismatch"),
    (lambda d: (_owner(d, artifact_root="/elsewhere/projects/alpha"), _damage(d, PLAIN))[1],
     "ownership_mismatch"),
    (lambda d: (_owner(d, project_id="beta"), _damage(d, PLAIN))[1], "ownership_mismatch"),
    (lambda d: ((d.artifact / "continuity" / "v1").mkdir(parents=True),
                (d.artifact / "continuity" / "v1" / "OWNER.json").write_text("{", encoding="utf-8"),
                _damage(d, PLAIN))[2], "ownership_mismatch"),
    (lambda d: _sha(d.intact), "not_damaged"),
], ids=["claimed_id", "claimed_target", "owner_target", "owner_artifact", "owner_project", "owner_unreadable",
        "intact"])
def test_each_refusal_classification_writes_nothing(deployment, setup, classification) -> None:
    sha = setup(deployment)
    before = deployment.descriptor.read_bytes()

    with pytest.raises(ValueError) as caught:
        _repair(deployment, sha, approve=True)

    exc: Any = caught.value
    assert exc.error_code == "descriptor_repair_refused"
    assert exc.context["classification"] == classification
    assert deployment.descriptor.read_bytes() == before
    assert not (deployment.artifact / "archive" / "descriptor-backups").exists()


def test_portfolio_and_identity_refusals_before_reading_the_descriptor(deployment, tmp_path) -> None:
    sha = _damage(deployment, PLAIN)
    with pytest.raises(ValueError) as unconfigured:
        _repair(deployment, sha, project_id="beta")
    assert unconfigured.value.context["classification"] == "portfolio_mismatch"  # type: ignore[attr-defined]

    other = tmp_path / "gamma-target"
    other.mkdir()
    config = _portfolio(tmp_path, {"alpha": deployment.target, "gamma": other})
    with pytest.raises(ValueError) as missing:
        repair_portfolio_descriptor(config_path=config, host_id="local", project_id="gamma",
                                    expected_sha256=sha)
    assert missing.value.context["classification"] == "identity_mismatch"  # type: ignore[attr-defined]

    deployment.descriptor.unlink()
    with pytest.raises(ValueError) as absent:
        _repair(deployment, sha)
    assert absent.value.context["classification"] == "not_damaged"  # type: ignore[attr-defined]


def test_stale_hash_and_non_boolean_approval_are_refused(deployment) -> None:
    sha = _damage(deployment, PLAIN)
    assert _repair(deployment, sha)["status"] == "plan"
    changed = _damage(deployment, PLAIN + "\nA later edit.\n")

    with pytest.raises(ValueError) as stale:
        _repair(deployment, sha, approve=True)

    assert stale.value.context["classification"] == "stale_hash"  # type: ignore[attr-defined]
    assert stale.value.context["current_sha256"] == changed  # type: ignore[attr-defined]
    assert _sha(deployment.descriptor.read_bytes()) == changed
    with pytest.raises(ValueError) as approval:
        _repair(deployment, changed, approve="yes")
    assert approval.value.context["classification"] == "approval_required"  # type: ignore[attr-defined]
    assert _sha(deployment.descriptor.read_bytes()) == changed


def test_dispatcher_subcommand_requires_matching_runtime_home(deployment, tmp_path) -> None:
    sha = _damage(deployment, PLAIN)

    plan = project_action(
        deployment.home, "repair-descriptor", "alpha", config_path=str(deployment.config),
        host_id="local", expected_sha256=sha, output_format="dict",
    )
    assert plan["descriptor_repair"]["status"] == "plan"
    assert plan["projects"][0]["integrity_status"] == "missing_frontmatter"

    other_home = tmp_path / "other-home"
    other_home.mkdir()
    with pytest.raises(ValueError) as caught:
        project_action(other_home, "repair-descriptor", "alpha", config_path=str(deployment.config),
                       host_id="local", expected_sha256=sha, approve=True, output_format="dict")
    assert caught.value.context["classification"] == "portfolio_mismatch"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="config_path"):
        project_action(deployment.home, "repair-descriptor", "alpha", expected_sha256=sha)

    repaired = project_action(
        deployment.home, "repair-descriptor", "alpha", config_path=str(deployment.config),
        host_id="local", expected_sha256=sha, approve=True, output_format="dict",
    )
    assert repaired["descriptor_repair"]["status"] == "repaired"
    assert repaired["projects"][0]["integrity_status"] == "intact"


def test_dispatcher_effects_classify_dry_run_and_approved_repair() -> None:
    contract = build_static_action_contracts()["project"]

    def effect(**kwargs: Any) -> tuple[Any, ...]:
        outcome = resolve_invocation(contract, ("repair-descriptor",), kwargs)
        if outcome.error is not None:
            raise outcome.error
        return outcome.effects[0], outcome.pre_task_access

    assert effect() == ("read", "safe_orientation")
    assert effect(approve=True) == ("artifact_write", "task_required")
    with pytest.raises(ValueError, match="approve must be boolean"):
        effect(approve="yes")


def test_cli_repair_descriptor_dry_run_and_approve(deployment, capsys) -> None:
    sha = _damage(deployment, PLAIN)
    arguments = ["portfolio", "repair-descriptor", "--config", str(deployment.config), "--host", "local",
                 "--project", "alpha", "--expected-sha256", sha]

    assert cli.main(arguments) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["ok"] is True and planned["result"]["status"] == "plan"
    assert deployment.descriptor.read_bytes() == PLAIN.encode()

    assert cli.main([*arguments, "--approve"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "repaired"

    assert cli.main([*arguments, "--approve"]) == cli.EXIT_ACTION
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False


# ── gate route-field protection ─────────────────────────────────────────────


def _session(deployment: SimpleNamespace, *, ledger: bool = True, bound: bool = True) -> SimpleNamespace:
    entries = [ManagedArtifactEntry(str(deployment.descriptor), "managed-artifact", "2026-10-10T00:00:00+00:00")]
    return SimpleNamespace(
        active_project="alpha", artifact_root=str(deployment.artifact), target_root=str(deployment.target),
        route_fingerprint="sha256:bound" if bound else None, managed_artifact_ledger=entries if ledger else [],
    )


def test_gate_allows_body_only_descriptor_edits(deployment) -> None:
    deployment.descriptor.write_bytes(deployment.intact + b"\n## Notes\n\nA body edit.\n")
    check_descriptor_route_protection(_session(deployment), changed_paths=set())


@pytest.mark.parametrize(("edit", "changed"), [
    (lambda d, text: text.replace(f"target_root: {d.target}", "target_root: /elsewhere/alpha"), ["target_root"]),
    (lambda d, text: text.replace("project_type: referenced", "project_type: managed"), ["project_type"]),
    (lambda d, text: text.split("---\n", 2)[2], ["frontmatter"]),
    (lambda d, text: text.replace("id: alpha", "id: beta"), ["frontmatter"]),
], ids=["target_root", "project_type", "frontmatter_removed", "id"])
def test_gate_blocks_route_changes_and_damage(deployment, edit, changed) -> None:
    deployment.descriptor.write_text(edit(deployment, deployment.intact.decode()), encoding="utf-8")

    with pytest.raises(RuntimeError) as caught:
        check_descriptor_route_protection(_session(deployment), changed_paths=set())

    exc: Any = caught.value
    assert exc.error_code == "managed_descriptor_route_change"
    assert exc.context["changed_fields"] == changed
    assert set(exc.context["supported_operations"]) == {"project move-target", "project repair-descriptor"}
    assert str(exc).startswith("BLOCKED:")


def test_gate_protection_applies_only_when_descriptor_is_in_the_changed_set(deployment) -> None:
    deployment.descriptor.write_text(deployment.intact.decode().split("---\n", 2)[2], encoding="utf-8")

    check_descriptor_route_protection(_session(deployment, ledger=False), changed_paths={"src/app.py"})

    with pytest.raises(RuntimeError):
        check_descriptor_route_protection(
            _session(deployment, ledger=False), changed_paths={str(deployment.descriptor)},
        )


def test_unbound_runtime_target_override_does_not_block_body_edits(deployment, tmp_path) -> None:
    session = _session(deployment, bound=False)
    session.target_root = str(tmp_path / "override")
    deployment.descriptor.write_bytes(deployment.intact + b"\nBody.\n")
    check_descriptor_route_protection(session, changed_paths=set())


@pytest.mark.parametrize(("project_type", "blocked"), [(None, False), ("managed", True)])
def test_gate_accepts_descriptors_with_defaulted_id_and_project_type(
    deployment, monkeypatch, project_type, blocked,
) -> None:
    # Since v0.3.25 the parser defaults id and project_type; simulate that intact shape.
    from odibi_anchor._dispatcher import _descriptor_protection
    from odibi_anchor._dispatcher._descriptor import DescriptorIntegrity

    fields = {"target_root": str(deployment.target)}
    if project_type is not None:
        fields["project_type"] = project_type
    monkeypatch.setattr(_descriptor_protection, "read_descriptor", lambda _root: DescriptorIntegrity(
        str(deployment.descriptor), "intact", "0" * 64, fields=fields,
    ))

    if not blocked:
        check_descriptor_route_protection(_session(deployment), changed_paths=set())
        return
    with pytest.raises(RuntimeError) as caught:
        check_descriptor_route_protection(_session(deployment), changed_paths=set())
    assert caught.value.context["changed_fields"] == ["project_type"]  # type: ignore[attr-defined]
