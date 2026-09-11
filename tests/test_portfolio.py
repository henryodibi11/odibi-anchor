from __future__ import annotations

import hashlib
import os
from copy import deepcopy

import pytest

from odibi_anchor import portfolio as portfolio_module
from odibi_anchor.portfolio import (
    add_project,
    load_portfolio,
    load_portfolio_document,
    resolve_project,
    scaffold_portfolio,
    validate_portfolio,
    write_portfolio,
)


def _portfolio(tmp_path):
    return {
        "schema_version": 1,
        "authority": {"id": "odibi", "trust_domain": "work"},
        "hosts": {
            "amp-host": {"adapter": "amp", "local_state_root": str(tmp_path / "state")},
            "remote": {"adapter": "claude", "local_state_root": "/remote/state"},
        },
        "projects": {
            "alpha": {
                "repository": "https://example.invalid/alpha.git",
                "artifact_namespace": "alpha",
                "targets": {"amp-host": str(tmp_path / "alpha"), "remote": "/remote/missing"},
            }
        },
        "personas": {"reviewer": {"task_mode": "review", "memory_scope": "project"}},
    }


def test_deterministic_bytes_ignore_dictionary_insertion_order(tmp_path):
    first = _portfolio(tmp_path)
    second = {
        "personas": first["personas"],
        "projects": first["projects"],
        "hosts": dict(reversed(list(first["hosts"].items()))),
        "authority": {"trust_domain": "work", "id": "odibi"},
        "schema_version": 1,
    }
    one, two = tmp_path / "one.toml", tmp_path / "two.toml"
    assert write_portfolio(one, first)["sha256"] == write_portfolio(two, second)["sha256"]
    assert one.read_bytes() == two.read_bytes()


def test_scaffold_is_commented_and_truthfully_incomplete(tmp_path):
    path = tmp_path / "anchor.toml"
    result = scaffold_portfolio(path, host_id="amp", adapter="amp", target_root="/work/repo")
    assert path.read_text().startswith("# Odibi Anchor PortfolioV1")
    assert result["validation"]["status"] == "incomplete"
    assert {item["field"] for item in result["validation"]["required_missing"]} >= {
        "authority.id",
        "hosts.amp.local_state_root",
        "projects.id",
    }
    assert result["next_operation"]["operation"] == "supply_field"


def test_exact_resolution_and_explicit_mismatch(tmp_path):
    portfolio = _portfolio(tmp_path)
    target = str(tmp_path / "alpha")
    assert resolve_project(portfolio, host_id="amp-host", target_root=target)["binding_inputs"] == {
        "host_id": "amp-host",
        "project_id": "alpha",
        "target_root": target,
    }
    assert resolve_project(portfolio, host_id="amp-host", project_id="alpha")["binding_source"] == "explicit_project"
    with pytest.raises(ValueError, match="does not match"):
        resolve_project(portfolio, host_id="amp-host", project_id="alpha", target_root="/wrong")
    with pytest.raises(ValueError, match="ambient selectors"):
        resolve_project(portfolio, host_id="amp-host")


def test_duplicate_roots_fail_closed_as_ambiguous_authority(tmp_path):
    portfolio = _portfolio(tmp_path)
    portfolio["projects"]["beta"] = {
        "repository": "beta",
        "artifact_namespace": "beta",
        "targets": {"amp-host": str(tmp_path / "alpha")},
    }
    with pytest.raises(ValueError, match="duplicate target root"):
        validate_portfolio(portfolio)


@pytest.mark.parametrize(
    "text, message",
    [
        ("schema_version = 2\n", "unsupported"),
        ("schema_version = 1\nunknown = true\n", "unknown"),
        ("schema_version = 1\nschema_version = 1\n", "invalid PortfolioV1 TOML"),
        ("schema_version = 1\napi_key = 'nope'\n", "forbidden|unknown"),
        ("schema_version = 1\nactive_project = 'alpha'\n", "active-project|unknown"),
        (
            "schema_version = 1\n[personas.bad]\npermission = 'admin'\n",
            "routing/permission",
        ),
    ],
)
def test_unsupported_unknown_duplicate_secret_selector_and_persona_authority(tmp_path, text, message):
    path = tmp_path / "anchor.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        load_portfolio(path)


def test_explicit_path_symlink_and_size_contracts(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        load_portfolio("anchor.toml")
    with pytest.raises(ValueError, match="single-line"):
        load_portfolio("/tmp/a\nb")
    large = tmp_path / "large.toml"
    large.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        load_portfolio(large)
    target, link = tmp_path / "real.toml", tmp_path / "link.toml"
    target.write_text("schema_version = 1\n")
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_portfolio(link)


def test_stale_cas_and_validation_failure_preserve_existing_bytes(tmp_path):
    path = tmp_path / "anchor.toml"
    portfolio = _portfolio(tmp_path)
    digest = write_portfolio(path, portfolio)["sha256"]
    before = path.read_bytes()
    with pytest.raises(ValueError, match="base digest changed"):
        write_portfolio(path, portfolio, expected_sha256="0" * 64)
    assert path.read_bytes() == before
    broken = deepcopy(portfolio)
    broken["hosts"]["amp-host"]["local_state_root"] = "relative"
    with pytest.raises(ValueError, match="absolute"):
        write_portfolio(path, broken, expected_sha256=digest)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".anchor.toml.*.tmp"))


def test_add_project_uses_cas_and_rejects_duplicate_id_and_root(tmp_path):
    path = tmp_path / "anchor.toml"
    portfolio = _portfolio(tmp_path)
    initial = write_portfolio(path, portfolio)["sha256"]
    result = add_project(
        path,
        project_id="beta",
        host_id="amp-host",
        target_root=str(tmp_path / "beta"),
        repository="beta",
        artifact_namespace="beta",
        expected_sha256=initial,
    )
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    document = load_portfolio_document(path)
    assert document["sha256"] == result["sha256"]
    assert "beta" in document["portfolio"]["projects"]
    with pytest.raises(ValueError, match="already exists"):
        add_project(path, project_id="beta", host_id="amp-host", target_root="/other")
    with pytest.raises(ValueError, match="duplicate target root"):
        add_project(
            path,
            project_id="gamma",
            host_id="amp-host",
            target_root=str(tmp_path / "alpha"),
            repository="gamma",
            artifact_namespace="gamma",
        )


def test_project_metadata_is_optional_and_configured_is_not_claimed_verified(tmp_path):
    portfolio = _portfolio(tmp_path)
    del portfolio["projects"]["alpha"]["repository"]
    del portfolio["projects"]["alpha"]["artifact_namespace"]

    result = validate_portfolio(portfolio)

    assert result["status"] == "valid"
    assert result["prefilled_verified"] == []
    assert {item["field"] for item in result["not_applicable"]} >= {
        "projects.alpha.repository",
        "projects.alpha.artifact_namespace",
    }


def test_windows_paths_are_portable_and_case_insensitive(tmp_path):
    portfolio = _portfolio(tmp_path)
    portfolio["projects"]["alpha"]["targets"]["remote"] = r"C:\\Work\\Alpha"
    assert resolve_project(
        portfolio, host_id="remote", target_root=r"c:\\work\\alpha"
    )["project_id"] == "alpha"


def test_persona_defaults_are_advisory_and_do_not_change_binding(tmp_path):
    result = resolve_project(
        _portfolio(tmp_path), host_id="amp-host", project_id="alpha", persona_id="reviewer"
    )
    assert result["persona"] == {
        "persona_id": "reviewer", "advisory": True,
        "task_mode": "review", "memory_scope": "project",
    }
    assert result["binding_inputs"]["project_id"] == "alpha"


@pytest.mark.parametrize("project_id", [" beta ", "Beta", "beta_project", "beta.project"])
def test_project_ids_must_be_canonical_before_add(tmp_path, project_id):
    path = tmp_path / "anchor.toml"
    write_portfolio(path, _portfolio(tmp_path))
    with pytest.raises(ValueError, match="canonical lowercase hyphenated"):
        add_project(
            path, project_id=project_id, host_id="amp-host",
            target_root=str(tmp_path / "beta"),
        )


def test_windows_config_publication_skips_directory_descriptor_fsync(
    tmp_path, monkeypatch
):
    path = tmp_path / "anchor.toml"
    real_open = portfolio_module.os.open

    def guarded_open(candidate, flags, *args, **kwargs):
        if candidate == path.parent and flags == os.O_RDONLY:
            raise AssertionError("Windows must not open a directory descriptor")
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(portfolio_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(portfolio_module.os, "open", guarded_open)

    result = write_portfolio(path, _portfolio(tmp_path))
    assert result["status"] == "written"


def test_only_requested_host_paths_are_probed(tmp_path, monkeypatch):
    portfolio = _portfolio(tmp_path)
    visited = []
    original = os.stat

    def recording_stat(path, *args, **kwargs):
        visited.append(os.fspath(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", recording_stat)
    result = validate_portfolio(portfolio, host_id="amp-host")
    assert result["selected_host"]["targets"][0]["exists"] is False
    assert "/remote/missing" not in visited
