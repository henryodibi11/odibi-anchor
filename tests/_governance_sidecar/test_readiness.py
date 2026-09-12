"""Focused canonicalization and readiness primitive tests."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.metadata
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from odibi_anchor._governance_sidecar import _measurement
from odibi_anchor._governance_sidecar._canonical import (
    CanonicalError,
    domain_digest,
    domain_preimage,
    dumps,
    safe_integer,
)
from odibi_anchor._governance_sidecar._crypto import (
    create_process_identity,
    decode_canonical_base64,
    verify_public_key,
)
from odibi_anchor._governance_sidecar._kernel import Kernel
from odibi_anchor._governance_sidecar._measurement import LocalMeasurementInputs, _path, measure_local
from odibi_anchor._governance_sidecar._readiness import (
    ReadinessError,
    build_readiness_conformance_candidate,
    verify_bound_ack_conformance,
)
from odibi_anchor._governance_sidecar._readiness_protocol import (
    ReadinessProtocolError,
    normalize_repository_id,
    validate_bootstrap_payload,
)


def test_jcs_vectors_and_nfc() -> None:
    assert dumps({"z": "é", "a": [True, None, 2]}) == b'{"a":[true,null,2],"z":"\xc3\xa9"}'
    assert dumps({"e\N{COMBINING ACUTE ACCENT}": "ok"}) == b'{"\xc3\xa9":"ok"}'


@pytest.mark.parametrize("value", [1.5, float("nan"), 9_007_199_254_740_992, "control\n", chr(0xD800)])
def test_security_objects_reject_invalid_values(value: object) -> None:
    with pytest.raises(CanonicalError):
        dumps({"value": value})


def test_nfc_key_collision_and_integer_boolean() -> None:
    with pytest.raises(CanonicalError, match="collision"):
        dumps({"é": 1, "e\N{COMBINING ACUTE ACCENT}": 2})
    with pytest.raises(CanonicalError):
        safe_integer(True)


def test_canonical_base64_and_public_key_digest() -> None:
    raw = b"x" * 32
    encoded = base64.b64encode(raw).decode("ascii")
    assert decode_canonical_base64(encoded, length=32) == raw
    assert verify_public_key(encoded, hashlib.sha256(raw).hexdigest()) == raw
    for invalid in (encoded.rstrip("="), encoded + "=", "!!!!"):
        with pytest.raises(ValueError):
            decode_canonical_base64(invalid, length=32)
    with pytest.raises(ValueError, match="digest"):
        verify_public_key(encoded, "0" * 64)


def _signed_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    repository_id = "https://github.com/example/project"
    subprocess.run(["git", "remote", "add", "origin", repository_id], cwd=tmp_path, check=True)
    plugin = tmp_path / "plugin.py"
    plugin.write_bytes(b"plugin bytes\n")
    monkeypatch.setattr(_measurement.odibi_anchor, "__version__", "0.9.0")

    identity = create_process_identity()
    now = int(identity.handshake["issued_at_ms"]) + 1
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    encoded_public = base64.b64encode(public).decode()
    def sha(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()
    compatibility = {
        "protocol_version": 1, "amp_version": "amp-1", "plugin_api_digest": "1" * 64,
        "python_min": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_max": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    release = {
        "anchor_version": "0.9.0", "source_commit": "a" * 40, "wheel_digest": "2" * 64,
        "plugin_digest": sha(plugin.read_bytes()), "compatibility": compatibility,
    }
    adapter = {
        "receipt_version": 1, "adapter_id": "adapter", "adapter_version": "1", "amp_version": "amp-1",
        "source": "builtin", "provider_id": "provider", "effective_name": "tool",
        "provider_artifact_digest": "3" * 64, "resolved_configuration_digest": "4" * 64,
        "input_schema_digest": "5" * 64, "classified_effects": ["read"], "path_derivation": "exact_leaf",
        "qualification_suite_digest": "6" * 64, "qualified_at_ms": now, "limitations": [],
    }
    tool = {
        "effective_name": "tool", "source": "builtin", "provider_id": "provider",
        "provider_artifact_digest": "3" * 64, "resolved_configuration_digest": "4" * 64,
        "input_schema_digest": "5" * 64, "qualified_adapter_receipt": adapter,
        "qualified_adapter_digest": domain_digest("anchor-amp/qualified-adapter/v1", adapter),
        "pre_call_observed": True, "terminal_result_observed": True,
    }
    manifest = {"manifest_version": 1, "amp_version": "amp-1", "plugin_api_digest": "1" * 64, "sources": []}
    manifest_digest = domain_digest("anchor-amp/declaration-source-manifest/v1", manifest)
    evidence = {
        "evidence_version": 1, "declaration_source_manifest": manifest,
        "declaration_source_manifest_digest": manifest_digest, "inspections": [],
        "declared_call_modifiers": [], "observed_at_ms": now, "limitations": [],
    }
    fence = {
        "fence_version": 1, "host_instance_id": "host", "process_instance_id": "process",
        "primitive": "pidfd_cgroup", "qualification_suite_digest": "7" * 64,
    }
    receipt = {
        "receipt_version": 1, "attestor_id": "attestor", "sidecar_instance_id": identity.handshake["sidecar_instance_id"],
        "challenge": identity.handshake["challenge"],
        "closure_public_key_base64": identity.handshake["closure_public_key_base64"],
        "closure_public_key_digest": identity.handshake["closure_public_key_digest"],
        "checkout_instance_id": "checkout", "ledger_generation_id": "ledger", "amp_workspace_id": "workspace",
        "amp_project_id": "project", "canonical_root": str(tmp_path.resolve()), "repository_id": repository_id,
        "checkout_reuse": {"status": "fresh", "prior_closure_digest": None},
        "setup_plugin_digest": release["plugin_digest"], "setup_wheel_digest": release["wheel_digest"],
        "amp_version": "amp-1", "enabled_tools": [tool],
        "enabled_tools_digest": domain_digest("anchor-amp/enabled-tools/v1", [tool]),
        "confirmation": {"status": "unavailable", "reason": "host_receipt_api_absent"},
        "sidecar_fence": fence, "sidecar_fence_digest": domain_digest("anchor-amp/sidecar-fence/v1", fence),
        "competing_modifiers": {
            "status": "none_declared", "evidence": evidence,
            "evidence_digest": domain_digest("anchor-amp/competing-modifiers-evidence/v1", evidence),
        },
        "gateway_handshake_digest": domain_digest("anchor-amp/sidecar-handshake/v1", identity.handshake),
        "observed_at_ms": now, "expires_at_ms": now + 30_000, "provenance": [], "limitations": [],
    }
    receipt["signature_base64"] = base64.b64encode(
        private.sign(domain_preimage("anchor-amp/readiness-signature/v1", receipt))
    ).decode()
    pin = {
        "attestor_id": "attestor", "algorithm": "ed25519", "public_key_base64": encoded_public,
        "public_key_digest": sha(public), "declaration_source_manifest_digest": manifest_digest,
        "confirmation_surface_receipt_digest": None, "sidecar_fence_qualification_digest": "7" * 64,
    }
    payload = {
        "project_root": str(tmp_path.resolve()),
        "routing": {"kind": "git", "repository_id": repository_id, "remote_name": "origin"},
        "release_pin": release, "attestor_pin": pin,
        "deployment_claim": {"plugin_path": str(plugin), "readiness_receipt": receipt,
                             "readiness_receipt_digest": domain_digest("anchor-amp/readiness/v1", receipt)},
    }
    return identity, private, pin, release, payload, LocalMeasurementInputs(tmp_path, plugin, payload["routing"]), now


def _resign_receipt(payload: dict[str, object], private) -> None:
    claim = payload["deployment_claim"]
    receipt = claim["readiness_receipt"]
    unsigned = {key: value for key, value in receipt.items() if key != "signature_base64"}
    receipt["signature_base64"] = base64.b64encode(
        private.sign(domain_preimage("anchor-amp/readiness-signature/v1", unsigned))
    ).decode()
    claim["readiness_receipt_digest"] = domain_digest("anchor-amp/readiness/v1", receipt)


def test_complete_signed_readiness_binding_ack_and_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity, attestor, pin, release, payload, inputs, now = _signed_fixture(tmp_path, monkeypatch)
    candidate = build_readiness_conformance_candidate(
        identity=identity, payload=payload, trusted_release_pin=release, trusted_attestor_pin=pin,
        now_ms=now, local_inputs=inputs,
    )
    public = decode_canonical_base64(identity.handshake["closure_public_key_base64"], length=32)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    binding_unsigned = {key: value for key, value in candidate.deployment_binding.items() if key != "sidecar_signature_base64"}
    Ed25519PublicKey.from_public_bytes(public).verify(
        base64.b64decode(candidate.deployment_binding["sidecar_signature_base64"]),
        domain_preimage("anchor-amp/deployment-binding-signature/v1", binding_unsigned),
    )
    ack = {
        "ack_version": 1, "status": "BOUND", "checkout_instance_id": "checkout", "ledger_generation_id": "ledger",
        "request_kind": "deployment_binding", "request_digest": candidate.deployment_binding_request_digest,
        "deployment_attestation_digest": candidate.deployment_attestation_digest, "closure_digest": None,
        "committed_at_ms": now,
    }
    ack["acknowledgement_digest"] = domain_digest("anchor-amp/ledger-ack/v1", ack)
    ack["attestor_signature_base64"] = base64.b64encode(
        attestor.sign(domain_preimage("anchor-amp/ledger-ack-signature/v1", ack))
    ).decode()
    verify_bound_ack_conformance(candidate, ack, now_ms=now)
    assert candidate.live_fence_status == "unverified"
    with pytest.raises(ReadinessError, match="challenge already consumed"):
        build_readiness_conformance_candidate(
            identity=identity, payload=payload, trusted_release_pin=release, trusted_attestor_pin=pin,
            now_ms=now, local_inputs=inputs,
        )
    with pytest.raises(TypeError):
        candidate.deployment_attestation["release_pin"]["anchor_version"] = "changed"


def test_valid_bootstrap_shape_remains_non_authorizing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    *_, payload, _, _ = _signed_fixture(tmp_path, monkeypatch)
    kernel = Kernel()
    result = kernel.handle({
        "protocol_version": 1,
        "request_id": "bootstrap-shaped",
        "operation": "bootstrap",
        "thread_id": "thread",
        "payload": payload,
    })
    assert result["error"]["code"] == "readiness_unavailable"
    assert kernel._state.lifecycle.value == "UNBOOTSTRAPPED"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda receipt: receipt.__setitem__("attestor_id", "other-attestor"), "issuer"),
        (lambda receipt: receipt.__setitem__("challenge", base64.b64encode(b"z" * 32).decode()), "challenge"),
        (lambda receipt: receipt.__setitem__("observed_at_ms", 0), "timestamp"),
        (
            lambda receipt: receipt["enabled_tools"][0]["qualified_adapter_receipt"]["limitations"].append("changed"),
            "adapter digest",
        ),
    ],
)
def test_authenticated_receipt_substitutions_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    message: str,
) -> None:
    identity, private, pin, release, payload, inputs, now = _signed_fixture(tmp_path, monkeypatch)
    mutation(payload["deployment_claim"]["readiness_receipt"])
    _resign_receipt(payload, private)
    with pytest.raises(ReadinessError, match=message):
        build_readiness_conformance_candidate(
            identity=identity,
            payload=payload,
            trusted_release_pin=release,
            trusted_attestor_pin=pin,
            now_ms=now,
            local_inputs=inputs,
        )


@pytest.mark.parametrize("field", ["pre_call_observed", "terminal_result_observed"])
def test_incomplete_tool_observation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    identity, private, pin, release, payload, inputs, now = _signed_fixture(tmp_path, monkeypatch)
    receipt = payload["deployment_claim"]["readiness_receipt"]
    receipt["enabled_tools"][0][field] = False
    receipt["enabled_tools_digest"] = domain_digest("anchor-amp/enabled-tools/v1", receipt["enabled_tools"])
    _resign_receipt(payload, private)
    with pytest.raises(ReadinessError, match="observations"):
        build_readiness_conformance_candidate(
            identity=identity, payload=payload, trusted_release_pin=release,
            trusted_attestor_pin=pin, now_ms=now, local_inputs=inputs,
        )


def test_adapter_amp_substitution_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity, private, pin, release, payload, inputs, now = _signed_fixture(tmp_path, monkeypatch)
    receipt = payload["deployment_claim"]["readiness_receipt"]
    tool = receipt["enabled_tools"][0]
    tool["qualified_adapter_receipt"]["amp_version"] = "other-amp"
    tool["qualified_adapter_digest"] = domain_digest(
        "anchor-amp/qualified-adapter/v1", tool["qualified_adapter_receipt"]
    )
    receipt["enabled_tools_digest"] = domain_digest("anchor-amp/enabled-tools/v1", receipt["enabled_tools"])
    _resign_receipt(payload, private)
    with pytest.raises(ReadinessError, match="adapter Amp"):
        build_readiness_conformance_candidate(
            identity=identity, payload=payload, trusted_release_pin=release,
            trusted_attestor_pin=pin, now_ms=now, local_inputs=inputs,
        )


def test_invalid_signature_does_not_consume_but_authentic_local_failure_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, _, pin, release, payload, inputs, now = _signed_fixture(tmp_path, monkeypatch)
    authentic = copy.deepcopy(payload)
    payload["deployment_claim"]["readiness_receipt"]["signature_base64"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(ValueError, match="signature"):
        build_readiness_conformance_candidate(
            identity=identity, payload=payload, trusted_release_pin=release,
            trusted_attestor_pin=pin, now_ms=now, local_inputs=inputs,
        )
    inputs.plugin_path.write_bytes(b"changed")
    with pytest.raises(ReadinessError, match="plugin"):
        build_readiness_conformance_candidate(
            identity=identity, payload=authentic, trusted_release_pin=release,
            trusted_attestor_pin=pin, now_ms=now, local_inputs=inputs,
        )
    inputs.plugin_path.write_bytes(b"plugin bytes\n")
    with pytest.raises(ReadinessError, match="consumed"):
        build_readiness_conformance_candidate(
            identity=identity, payload=authentic, trusted_release_pin=release,
            trusted_attestor_pin=pin, now_ms=now, local_inputs=inputs,
        )


def test_bound_ack_rejects_alternate_key_and_boolean_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    identity, _, pin, release, payload, inputs, now = _signed_fixture(tmp_path, monkeypatch)
    candidate = build_readiness_conformance_candidate(
        identity=identity, payload=payload, trusted_release_pin=release,
        trusted_attestor_pin=pin, now_ms=now, local_inputs=inputs,
    )
    ack = {
        "ack_version": 1, "status": "BOUND", "checkout_instance_id": "checkout",
        "ledger_generation_id": "ledger", "request_kind": "deployment_binding",
        "request_digest": candidate.deployment_binding_request_digest,
        "deployment_attestation_digest": candidate.deployment_attestation_digest,
        "closure_digest": None, "committed_at_ms": now,
    }
    ack["acknowledgement_digest"] = domain_digest("anchor-amp/ledger-ack/v1", ack)
    alternate = Ed25519PrivateKey.generate()
    ack["attestor_signature_base64"] = base64.b64encode(
        alternate.sign(domain_preimage("anchor-amp/ledger-ack-signature/v1", ack))
    ).decode()
    with pytest.raises(ValueError, match="signature"):
        verify_bound_ack_conformance(candidate, ack, now_ms=now)
    ack["committed_at_ms"] = True
    with pytest.raises(ReadinessError, match="invalid BOUND"):
        verify_bound_ack_conformance(candidate, ack, now_ms=now)


@pytest.mark.parametrize("value", [
    "http://github.com/a/b", "https://user@github.com/a/b", "https://github.com/a/../b",
    "https://github.com/a/b?token=x", "https://github.com/a%2Fb/c", "https://github.com/a/",
    "https://github.com/a/%", "https://github.com/a/b z",
    "https://github.com/a/b|c", "https://github.com/a/b[", "https://github.com:/a/b",
    "https://github.com:444/a/b", "https://github.com/a/naïve",
])
def test_repository_id_rejects_ambiguous_or_credentialed_values(value: str) -> None:
    with pytest.raises(ReadinessProtocolError):
        normalize_repository_id(value)


def test_repository_id_normalization_and_git_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert normalize_repository_id("HTTPS://GitHub.COM/Owner/Repo.git/") == "https://github.com/owner/repo"
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    first = measure_local(inputs).repository_evidence
    assert first["outcome"] == "available" and "plugin.py" in first["fingerprint"]["fingerprinted_paths"]
    inputs.plugin_path.write_bytes(b"different")
    second = measure_local(inputs).repository_evidence
    assert first["fingerprint"]["worktree_fingerprint"] != second["fingerprint"]["worktree_fingerprint"]


@pytest.mark.parametrize("remote_url", [
    " https://github.com/example/project",
    "https://github.com/example/project ",
])
def test_git_remote_url_whitespace_is_not_trimmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_url: str,
) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=tmp_path, check=True)

    with pytest.raises(ReadinessProtocolError):
        measure_local(inputs)


def test_committed_staged_delete_ignored_path_remains_fingerprinted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    (tmp_path / ".git" / "info" / "exclude").write_text("tracked.txt\n", encoding="utf-8")
    subprocess.run(["git", "rm", "--cached", "-q", "tracked.txt"], cwd=tmp_path, check=True)

    first = measure_local(inputs).repository_evidence["fingerprint"]
    assert "tracked.txt" in first["fingerprinted_paths"]
    (tmp_path / "tracked.txt").write_text("changed ignored bytes\n", encoding="utf-8")
    second = measure_local(inputs).repository_evidence["fingerprint"]
    assert first["worktree_fingerprint"] != second["worktree_fingerprint"]


def test_cw_version_measurement_ignores_higher_precedence_unowned_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = tmp_path / "fake-metadata"
    fake_metadata = fake_root / "odibi_anchor-9.9.9.dist-info"
    fake_metadata.mkdir(parents=True)
    (fake_metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: odibi-anchor\nVersion: 9.9.9\n",
        encoding="utf-8",
    )
    plugin = tmp_path / "plugin.py"
    plugin.write_text("plugin\n", encoding="utf-8")
    monkeypatch.syspath_prepend(fake_root)

    assert importlib.metadata.version("odibi-anchor") == "9.9.9"
    measured = measure_local(LocalMeasurementInputs(tmp_path, plugin, {"kind": "non_git_read_only"}))
    assert measured.anchor_version == "0.3.4"


def test_unknown_cw_version_provenance_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = tmp_path / "plugin.py"
    plugin.write_text("plugin\n", encoding="utf-8")
    monkeypatch.setattr(_measurement.odibi_anchor, "__version__", "0+unknown")

    with pytest.raises(ValueError, match="version provenance"):
        measure_local(LocalMeasurementInputs(tmp_path, plugin, {"kind": "non_git_read_only"}))


def test_git_fingerprint_matches_independent_exact_preimages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    evidence = measure_local(inputs).repository_evidence["fingerprint"]
    object_id = subprocess.run(
        ["git", "rev-parse", "HEAD:tracked.txt"], cwd=tmp_path, text=True,
        capture_output=True, check=True,
    ).stdout.strip()
    index_jcs = (
        f'[{json.dumps({"mode": "100644", "object_id": object_id, "path": "tracked.txt", "stage": 0}, separators=(",", ":"), sort_keys=True)}]'
    ).encode()
    expected_index = hashlib.sha256(b"anchor-amp/index-entries/v1\0" + index_jcs).hexdigest()

    def frame(tag: str, value: bytes) -> bytes:
        raw_tag = tag.encode()
        return struct.pack(">I", len(raw_tag)) + raw_tag + struct.pack(">Q", len(value)) + value

    paths = ["plugin.py", "tracked.txt"]
    content = bytearray()
    for relative in paths:
        target = tmp_path / relative
        info = target.lstat()
        content += frame("path", relative.encode())
        content += frame("type", struct.pack(">Q", stat.S_IFMT(info.st_mode)))
        content += frame("mode", struct.pack(">I", info.st_mode & 0o7777))
        content += frame("regular", target.read_bytes())
    path_digest = hashlib.sha256("\0".join(paths).encode()).digest()
    content_digest = hashlib.sha256(content).digest()
    expected_worktree = hashlib.sha256(
        b"anchor-amp/worktree/v1\0" + path_digest + content_digest
    ).hexdigest()
    assert evidence["index_fingerprint"] == expected_index
    assert evidence["worktree_fingerprint"] == expected_worktree


def test_repository_paths_reject_non_nfc_and_external_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="NFC"):
        _path("e\N{COMBINING ACUTE ACCENT}.txt".encode())
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    outside = tmp_path.parent / "outside-readiness.txt"
    outside.write_text("outside")
    (tmp_path / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        measure_local(inputs)


def test_missing_path_below_external_parent_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    tracked_dir = tmp_path / "tracked-dir"
    tracked_dir.mkdir()
    tracked_leaf = tracked_dir / "leaf.txt"
    tracked_leaf.write_text("leaf")
    subprocess.run(["git", "add", "tracked-dir/leaf.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "add nested"],
        cwd=tmp_path, check=True,
    )
    tracked_leaf.unlink()
    tracked_dir.rmdir()
    outside = tmp_path.parent / "outside-directory"
    outside.mkdir(exist_ok=True)
    tracked_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        measure_local(inputs)


def _require_user_mount_namespace(probe_path: Path) -> None:
    """Skip namespace qualification when the runner cannot create its sandbox."""
    if sys.platform != "linux":
        pytest.skip("Linux user/mount namespace qualification is unavailable")
    unshare = Path("/usr/bin/unshare")
    if not unshare.is_file():
        pytest.skip("Linux user/mount namespace qualification requires unshare")
    probe = subprocess.run(
        [
            str(unshare), "--user", "--map-root-user", "--mount",
            "/bin/mount", "--bind", str(probe_path), str(probe_path),
        ],
        text=True, capture_output=True, check=False, timeout=10,
    )
    if probe.returncode != 0:
        diagnostic = (probe.stderr or probe.stdout).strip().splitlines()
        detail = diagnostic[-1][:200] if diagnostic else f"exit code {probe.returncode}"
        pytest.skip(f"Linux user/mount namespace qualification is unavailable: {detail}")


def test_user_mount_namespace_probe_skips_when_runner_denies_uid_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, "", "unshare: write failed /proc/self/uid_map: Operation not permitted\n",
        ),
    )

    with pytest.raises(pytest.skip.Exception, match="user/mount namespace qualification is unavailable"):
        _require_user_mount_namespace(tmp_path)


def test_repository_evidence_runs_with_git_administration_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_user_mount_namespace(tmp_path)
    _, _, _, _, payload, _, _ = _signed_fixture(tmp_path, monkeypatch)
    linked = tmp_path.parent / f"{tmp_path.name}-linked"
    alternate_objects = tmp_path.parent / f"{tmp_path.name}-alternate-objects"
    subprocess.run(["git", "worktree", "add", "--detach", "-q", str(linked), "HEAD"], cwd=tmp_path, check=True)
    plugin = linked / "plugin.py"
    plugin.write_bytes(b"plugin bytes\n")
    alternate_objects.mkdir()
    preexisting = subprocess.run(
        ["git", "hash-object", "-w", str(plugin)], cwd=linked,
        env={**os.environ, "GIT_OBJECT_DIRECTORY": str(alternate_objects)},
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    preexisting_object = alternate_objects / preexisting[:2] / preexisting[2:]
    assert preexisting_object.is_file()

    def git_path(*args: str) -> Path:
        value = Path(subprocess.run(
            ["git", "-C", str(linked), *args], check=True, text=True, capture_output=True,
        ).stdout.strip())
        return value.resolve() if value.is_absolute() else (linked / value).resolve()

    primary_objects = git_path("rev-parse", "--git-path", "objects")
    alternates_file = primary_objects / "info" / "alternates"
    alternates_file.parent.mkdir(exist_ok=True)
    alternates_file.write_text(f"{alternate_objects}\n")
    administration = {
        (linked / ".git").resolve(), git_path("rev-parse", "--git-dir"),
        git_path("rev-parse", "--git-common-dir"), git_path("rev-parse", "--git-path", "index"),
        primary_objects, alternate_objects.resolve(),
    }

    def administrative_hashes() -> dict[str, str]:
        files: set[Path] = set()
        for location in administration:
            if location.is_file():
                files.add(location)
            elif location.is_dir():
                files.update(path for path in location.rglob("*") if path.is_file())
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}

    hashes_before = administrative_hashes()
    script = """
import json, os, subprocess, sys
from pathlib import Path
from odibi_anchor._governance_sidecar._measurement import LocalMeasurementInputs, measure_local
root = Path(sys.argv[1])
plugin = Path(sys.argv[2])
routing = json.loads(sys.argv[3])
git_dir = root / '.git'
def git_path(*args):
    value = Path(subprocess.run(['git', '-C', str(root), *args], check=True, text=True,
                                capture_output=True).stdout.strip())
    return value.resolve() if value.is_absolute() else (root / value).resolve()
protected = {git_dir.resolve(), git_path('rev-parse', '--git-dir'),
             git_path('rev-parse', '--git-common-dir'), git_path('rev-parse', '--git-path', 'index'),
             git_path('rev-parse', '--git-path', 'objects')}
alternates = git_path('rev-parse', '--git-path', 'objects/info/alternates')
if alternates.is_file():
    object_dir = git_path('rev-parse', '--git-path', 'objects')
    protected.update((Path(line).resolve() if Path(line).is_absolute() else (object_dir / line).resolve())
                     for line in alternates.read_text().splitlines() if line)
for path in sorted(protected, key=lambda item: len(item.parts), reverse=True):
    if path.exists():
        subprocess.run(['mount', '--bind', str(path), str(path)], check=True)
        subprocess.run(['mount', '-o', 'remount,bind,ro', str(path)], check=True)
for directory in (git_path('rev-parse', '--git-dir'), git_path('rev-parse', '--git-common-dir'),
                  git_path('rev-parse', '--git-path', 'objects'), *[path for path in protected if path.is_dir()]):
    try:
        (directory / 'forbidden-write').write_bytes(b'x')
    except OSError:
        pass
    else:
        raise SystemExit(f'read-only boundary accepted a create in {directory}')
index = git_path('rev-parse', '--git-path', 'index')
for target in (index, Path(sys.argv[4])):
    try:
        with target.open('r+b') as handle:
            first = handle.read(1)
            handle.seek(0)
            handle.write(first)
    except OSError:
        pass
    else:
        raise SystemExit(f'read-only boundary accepted a rewrite/restore of {target}')
result = measure_local(LocalMeasurementInputs(root, plugin, routing))
assert result.repository_evidence['outcome'] == 'available'
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    result = subprocess.run(
        [
            "/usr/bin/unshare", "--user", "--map-root-user", "--mount",
            sys.executable, "-c", script, str(linked), str(plugin), json.dumps(payload["routing"]),
            str(preexisting_object),
        ],
        env=environment, text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2_000:]
    assert administrative_hashes() == hashes_before
    subprocess.run(["git", "worktree", "remove", "--force", str(linked)], cwd=tmp_path, check=True)
    shutil.rmtree(alternate_objects)


def test_git_environment_overrides_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-administration"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "missing-objects"))
    assert measure_local(inputs).repository_evidence["outcome"] == "available"


def test_unmerged_index_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    object_id = subprocess.run(
        ["git", "hash-object", "-w", "tracked.txt"], cwd=tmp_path,
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--index-info"], cwd=tmp_path,
        input=f"100644 {object_id} 1\ttracked.txt\n100644 {object_id} 2\ttracked.txt\n",
        text=True, capture_output=True, check=True,
    )
    with pytest.raises(ValueError, match="unmerged"):
        measure_local(inputs)


def test_git_measurement_supports_detached_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    subprocess.run(["git", "checkout", "--detach", "-q"], cwd=tmp_path, check=True)
    evidence = measure_local(inputs).repository_evidence
    assert evidence["outcome"] == "available"
    assert evidence["fingerprint"]["branch"] is None


def test_git_measurement_rejects_unborn_head_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    repository_id = "https://github.com/example/project"
    subprocess.run(["git", "remote", "add", "origin", repository_id], cwd=tmp_path, check=True)
    plugin = tmp_path / "plugin.py"
    plugin.write_text("plugin\n", encoding="utf-8")
    monkeypatch.setattr(_measurement.odibi_anchor, "__version__", "0.9.0")

    with pytest.raises(ValueError, match="valid commit HEAD"):
        measure_local(LocalMeasurementInputs(
            tmp_path, plugin,
            {"kind": "git", "repository_id": repository_id, "remote_name": "origin"},
        ))


@pytest.mark.parametrize("invalid_head", [
    "missing_tag", "tree_object", "dangling_symbolic_branch", "malformed_branch",
])
def test_git_measurement_rejects_noncommit_or_malformed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_head: str,
) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    if invalid_head == "missing_tag":
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/tags/missing\n", encoding="ascii")
    else:
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=tmp_path,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        (tmp_path / ".git" / "refs" / "heads" / "main").write_text(f"{tree}\n", encoding="ascii")
    if invalid_head == "dangling_symbolic_branch":
        (tmp_path / ".git" / "refs" / "heads" / "main").write_text(
            "ref: refs/heads/missing\n", encoding="ascii"
        )
    if invalid_head == "malformed_branch":
        (tmp_path / ".git" / "refs" / "heads" / "main").write_text("not-an-object\n", encoding="ascii")

    with pytest.raises(ValueError, match="valid commit HEAD"):
        measure_local(inputs)


def test_plugin_symlink_is_not_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    link = tmp_path / "plugin-link.py"
    link.symlink_to(inputs.plugin_path)
    with pytest.raises(ValueError, match="symlink"):
        measure_local(LocalMeasurementInputs(tmp_path, link, inputs.routing))


@pytest.mark.parametrize("route", ["git", "non_git_read_only"])
def test_plugin_must_resolve_strictly_below_canonical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    _, _, _, _, _, inputs, _ = _signed_fixture(tmp_path, monkeypatch)
    routing = inputs.routing if route == "git" else {"kind": "non_git_read_only"}
    outside = tmp_path.parent / f"{tmp_path.name}-external-plugin"
    outside.mkdir()
    external_plugin = outside / "plugin.py"
    external_plugin.write_text("external\n", encoding="utf-8")
    parent_link = tmp_path / "external-parent"
    parent_link.symlink_to(outside, target_is_directory=True)

    for candidate in (external_plugin, parent_link / "plugin.py"):
        with pytest.raises(ValueError, match="under an absolute root"):
            measure_local(LocalMeasurementInputs(tmp_path, candidate, routing))


@pytest.mark.parametrize("path,value", [
    (("release_pin", "compatibility", "protocol_version"), True),
    (("deployment_claim", "readiness_receipt", "observed_at_ms"), True),
])
def test_nested_boolean_integer_is_protocol_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: tuple[str, ...], value: object,
) -> None:
    *_, payload, _, _ = _signed_fixture(tmp_path, monkeypatch)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ReadinessProtocolError):
        validate_bootstrap_payload(payload)
