"""Exact peer-neutral protocol-v1 document schemas (deliberately non-authorizing)."""

from __future__ import annotations

import base64
import re
import string
from collections.abc import Callable
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._canonical import CanonicalError, normalize, safe_integer

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
EFFECTS = (
    "artifact_write", "data_write", "external_mutation", "governance_write", "orient",
    "read", "source_write",
)
SOURCES = ("builtin", "global_plugin", "mcp", "project_plugin", "system_plugin")
SCOPES = ("global", "project", "system")
PYTHON_MINOR = re.compile(r"(?:[1-9][0-9]*|0)\.(?:[1-9][0-9]*|0)\Z")
UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
PCHAR = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+\Z")


class ReadinessProtocolError(ValueError):
    pass


def _fail(message: str = "invalid readiness object") -> None:
    raise ReadinessProtocolError(message)


def _obj(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail()
    try:
        return normalize(value)
    except CanonicalError as exc:
        raise ReadinessProtocolError(str(exc)) from exc


def _text(value: object, *, nonempty: bool = True) -> str:
    try:
        result = normalize(value)
    except CanonicalError as exc:
        raise ReadinessProtocolError(str(exc)) from exc
    if not isinstance(result, str) or (nonempty and not result):
        _fail()
    return result


def _bounded_text(value: object, *, maximum: int = 512) -> str:
    result = _text(value)
    if len(result.encode("utf-8")) > maximum:
        _fail()
    return result


def normalize_repository_id(value: object) -> str:
    raw = _bounded_text(value, maximum=2048)
    if any(char.isspace() for char in raw) or re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        _fail("invalid repository identity")
    try:
        raw.encode("ascii")
    except UnicodeEncodeError:
        _fail("invalid repository identity")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        _fail("invalid repository identity")
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or re.fullmatch(r"[A-Za-z0-9._~-]+(?::443)?", parsed.netloc, re.IGNORECASE) is None
    ):
        _fail("invalid repository identity")
    if parsed.query or parsed.fragment or port not in {None, 443} or "%2f" in raw.lower() or "%5c" in raw.lower():
        _fail("invalid repository identity")
    segments = parsed.path.split("/")
    if segments and segments[-1] == "":
        segments.pop()
    if len(segments) != 3 or segments[0] or not segments[1] or not segments[2]:
        _fail("invalid repository identity")
    decoded: list[str] = []
    for segment in segments[1:]:
        if PCHAR.fullmatch(segment) is None:
            _fail("invalid repository identity")
        # Decode only percent-encoded unreserved bytes; preserve other escapes canonically.
        normalized = re.sub(
            r"%([0-9A-Fa-f]{2})",
            lambda match: chr(int(match.group(1), 16))
            if chr(int(match.group(1), 16)) in UNRESERVED else match.group(0).upper(),
            segment,
        )
        if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
            _fail("invalid repository identity")
        decoded.append(normalized)
    decoded[-1] = re.sub(r"(?i)\.git\Z", "", decoded[-1])
    if not all(decoded):
        _fail("invalid repository identity")
    host = parsed.hostname.lower()
    if host == "github.com":
        decoded = [part.lower() for part in decoded]
    path = "/" + "/".join(decoded)
    return urlunsplit(("https", host, path, "", ""))


def _id(value: object) -> str:
    result = _text(value)
    if not ID_PATTERN.fullmatch(result):
        _fail()
    return result


def _digest(value: object) -> str:
    result = _text(value)
    if not SHA.fullmatch(result):
        _fail()
    return result


def _b64(value: object, length: int) -> str:
    result = _text(value)
    try:
        raw = base64.b64decode(result, validate=True)
    except (ValueError, base64.binascii.Error):
        _fail()
    if len(raw) != length or base64.b64encode(raw).decode("ascii") != result:
        _fail()
    return result


def _absolute(value: object) -> str:
    result = _text(value)
    if not PurePath(result).is_absolute() or result.startswith("~"):
        _fail()
    return result


def _array(value: object, validator: Callable[[object], Any]) -> list[Any]:
    if not isinstance(value, list):
        _fail()
    return [validator(item) for item in value]


def _sorted_unique(items: list[Any], key: Callable[[Any], Any] = lambda item: item) -> list[Any]:
    keys = [key(item) for item in items]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        _fail("array must be sorted and unique")
    return items


def _release(value: object) -> dict[str, Any]:
    out = _obj(value, {"anchor_version", "source_commit", "wheel_digest", "plugin_digest", "compatibility"})
    out["anchor_version"] = _text(out["anchor_version"])
    if not COMMIT.fullmatch(_text(out["source_commit"])):
        _fail()
    out["wheel_digest"], out["plugin_digest"] = _digest(out["wheel_digest"]), _digest(out["plugin_digest"])
    compatibility = _obj(
        out["compatibility"], {"protocol_version", "amp_version", "plugin_api_digest", "python_min", "python_max"}
    )
    if safe_integer(compatibility["protocol_version"]) != 1:
        _fail()
    compatibility["amp_version"] = _text(compatibility["amp_version"])
    compatibility["plugin_api_digest"] = _digest(compatibility["plugin_api_digest"])
    compatibility["python_min"] = _text(compatibility["python_min"])
    compatibility["python_max"] = _text(compatibility["python_max"])
    if not PYTHON_MINOR.fullmatch(compatibility["python_min"]) or not PYTHON_MINOR.fullmatch(compatibility["python_max"]):
        _fail()
    minimum = tuple(map(int, compatibility["python_min"].split(".")))
    maximum = tuple(map(int, compatibility["python_max"].split(".")))
    if minimum > maximum:
        _fail()
    out["compatibility"] = compatibility
    return out


def _attestor(value: object) -> dict[str, Any]:
    keys = {
        "attestor_id", "algorithm", "public_key_base64", "public_key_digest",
        "declaration_source_manifest_digest", "confirmation_surface_receipt_digest",
        "sidecar_fence_qualification_digest",
    }
    out = _obj(value, keys)
    out["attestor_id"] = _id(out["attestor_id"])
    if out["algorithm"] != "ed25519":
        _fail()
    out["public_key_base64"] = _b64(out["public_key_base64"], 32)
    for key in ("public_key_digest", "declaration_source_manifest_digest", "sidecar_fence_qualification_digest"):
        out[key] = _digest(out[key])
    if out["confirmation_surface_receipt_digest"] is not None:
        out["confirmation_surface_receipt_digest"] = _digest(out["confirmation_surface_receipt_digest"])
    return out


def _provenance(value: object) -> dict[str, Any]:
    out = _obj(value, {"field_path", "source", "evidence_digest"})
    out["field_path"] = _text(out["field_path"])
    if out["source"] not in {
        "reviewed_config", "setup_log", "measured_bytes", "amp_cli", "qualification_test", "operator_attestation"
    }:
        _fail()
    out["evidence_digest"] = _digest(out["evidence_digest"])
    return out


def _adapter(value: object) -> dict[str, Any]:
    keys = {
        "receipt_version", "adapter_id", "adapter_version", "amp_version", "source", "provider_id",
        "effective_name", "provider_artifact_digest", "resolved_configuration_digest", "input_schema_digest",
        "classified_effects", "path_derivation", "qualification_suite_digest", "qualified_at_ms", "limitations",
    }
    out = _obj(value, keys)
    if safe_integer(out["receipt_version"]) != 1 or out["source"] not in SOURCES or out["path_derivation"] != "exact_leaf":
        _fail()
    for key in ("adapter_id", "adapter_version", "amp_version", "provider_id", "effective_name"):
        out[key] = _text(out[key])
    for key in ("provider_artifact_digest", "resolved_configuration_digest", "input_schema_digest", "qualification_suite_digest"):
        out[key] = _digest(out[key])
    effects = _array(out["classified_effects"], _text)
    if any(effect not in EFFECTS for effect in effects):
        _fail()
    out["classified_effects"] = _sorted_unique(effects)
    out["qualified_at_ms"] = safe_integer(out["qualified_at_ms"])
    out["limitations"] = _array(out["limitations"], _text)
    return out


def _tool(value: object) -> dict[str, Any]:
    keys = {
        "effective_name", "source", "provider_id", "provider_artifact_digest", "resolved_configuration_digest",
        "input_schema_digest", "qualified_adapter_receipt", "qualified_adapter_digest", "pre_call_observed",
        "terminal_result_observed",
    }
    out = _obj(value, keys)
    for key in ("effective_name", "provider_id"):
        out[key] = _text(out[key])
    if out["source"] not in SOURCES or type(out["pre_call_observed"]) is not bool or type(out["terminal_result_observed"]) is not bool:
        _fail()
    for key in ("provider_artifact_digest", "resolved_configuration_digest", "input_schema_digest", "qualified_adapter_digest"):
        out[key] = _digest(out[key])
    out["qualified_adapter_receipt"] = _adapter(out["qualified_adapter_receipt"])
    adapter = out["qualified_adapter_receipt"]
    for key in ("source", "provider_id", "effective_name", "provider_artifact_digest", "resolved_configuration_digest", "input_schema_digest"):
        if adapter[key] != out[key]:
            _fail("adapter does not match inventory entry")
    return out


def _confirmation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in {"available", "unavailable"}:
        _fail()
    if value["status"] == "unavailable":
        out = _obj(value, {"status", "reason"})
        if out["reason"] not in {"host_receipt_api_absent", "surface_unqualified"}:
            _fail()
        return out
    out = _obj(value, {"status", "surface_receipt", "surface_receipt_digest"})
    keys = {
        "receipt_version", "surface_id", "amp_version", "plugin_api_digest", "surface_build_digest",
        "renderer_version", "exact_utf8_rendering", "markup_interpretation", "truncation_reporting",
        "max_card_utf8_bytes", "result_signature_algorithm", "result_public_key_base64",
        "result_public_key_digest", "qualification_suite_digest", "qualified_at_ms", "limitations",
    }
    receipt = _obj(out["surface_receipt"], keys)
    if safe_integer(receipt["receipt_version"]) != 1 or receipt["exact_utf8_rendering"] is not True:
        _fail()
    if receipt["markup_interpretation"] is not False or receipt["truncation_reporting"] != "trusted_receipt":
        _fail()
    if receipt["result_signature_algorithm"] != "ed25519":
        _fail()
    for key in ("surface_id", "amp_version", "renderer_version"):
        receipt[key] = _text(receipt[key])
    for key in ("plugin_api_digest", "surface_build_digest", "result_public_key_digest", "qualification_suite_digest"):
        receipt[key] = _digest(receipt[key])
    receipt["result_public_key_base64"] = _b64(receipt["result_public_key_base64"], 32)
    receipt["max_card_utf8_bytes"] = safe_integer(receipt["max_card_utf8_bytes"], minimum=1)
    receipt["qualified_at_ms"] = safe_integer(receipt["qualified_at_ms"])
    receipt["limitations"] = _array(receipt["limitations"], _text)
    out["surface_receipt"], out["surface_receipt_digest"] = receipt, _digest(out["surface_receipt_digest"])
    return out


def _fence(value: object) -> dict[str, Any]:
    out = _obj(value, {"fence_version", "host_instance_id", "process_instance_id", "primitive", "qualification_suite_digest"})
    if safe_integer(out["fence_version"]) != 1 or out["primitive"] != "pidfd_cgroup":
        _fail()
    out["host_instance_id"], out["process_instance_id"] = _id(out["host_instance_id"]), _id(out["process_instance_id"])
    out["qualification_suite_digest"] = _digest(out["qualification_suite_digest"])
    return out


def _manifest(value: object) -> dict[str, Any]:
    out = _obj(value, {"manifest_version", "amp_version", "plugin_api_digest", "sources"})
    if safe_integer(out["manifest_version"]) != 1:
        _fail()
    out["amp_version"], out["plugin_api_digest"] = _text(out["amp_version"]), _digest(out["plugin_api_digest"])
    def source(item: object) -> dict[str, Any]:
        result = _obj(item, {"scope", "source_id", "location_kind", "locator", "resolver_version"})
        if result["scope"] not in SCOPES or result["location_kind"] not in {"config_file", "plugin_directory", "amp_registry"}:
            _fail()
        for key in ("source_id", "locator", "resolver_version"):
            result[key] = _text(result[key])
        return result
    out["sources"] = _sorted_unique(_array(out["sources"], source), lambda item: (item["scope"], item["source_id"]))
    return out


def _modifiers(value: object) -> dict[str, Any]:
    outer = _obj(value, {"status", "evidence", "evidence_digest"})
    if outer["status"] != "none_declared":
        _fail()
    keys = {"evidence_version", "declaration_source_manifest", "declaration_source_manifest_digest", "inspections", "declared_call_modifiers", "observed_at_ms", "limitations"}
    evidence = _obj(outer["evidence"], keys)
    if safe_integer(evidence["evidence_version"]) != 1:
        _fail()
    evidence["declaration_source_manifest"] = _manifest(evidence["declaration_source_manifest"])
    evidence["declaration_source_manifest_digest"] = _digest(evidence["declaration_source_manifest_digest"])
    def modifier(item: object, scoped: bool = False) -> dict[str, Any]:
        keys = {"scope", "provider_id", "provider_digest"} if scoped else {"provider_id", "provider_digest"}
        result = _obj(item, keys)
        if scoped and result["scope"] not in SCOPES:
            _fail()
        result["provider_id"], result["provider_digest"] = _text(result["provider_id"]), _digest(result["provider_digest"])
        return result
    def inspection(item: object) -> dict[str, Any]:
        result = _obj(item, {"scope", "source_id", "outcome", "configuration_digest", "declared_call_modifiers"})
        if result["scope"] not in SCOPES or result["outcome"] not in {"absent", "parsed"}:
            _fail()
        result["source_id"] = _text(result["source_id"])
        result["declared_call_modifiers"] = _sorted_unique(_array(result["declared_call_modifiers"], modifier), lambda x: x["provider_id"])
        if result["outcome"] == "absent":
            if result["configuration_digest"] is not None or result["declared_call_modifiers"]:
                _fail()
        else:
            result["configuration_digest"] = _digest(result["configuration_digest"])
        return result
    evidence["inspections"] = _sorted_unique(_array(evidence["inspections"], inspection), lambda x: (x["scope"], x["source_id"]))
    manifest_keys = [(x["scope"], x["source_id"]) for x in evidence["declaration_source_manifest"]["sources"]]
    if [(x["scope"], x["source_id"]) for x in evidence["inspections"]] != manifest_keys:
        _fail()
    evidence["declared_call_modifiers"] = _sorted_unique(_array(evidence["declared_call_modifiers"], lambda x: modifier(x, True)), lambda x: (x["scope"], x["provider_id"]))
    if evidence["declared_call_modifiers"] or any(x["declared_call_modifiers"] for x in evidence["inspections"]):
        _fail()
    evidence["observed_at_ms"] = safe_integer(evidence["observed_at_ms"])
    evidence["limitations"] = _array(evidence["limitations"], _text)
    outer["evidence"], outer["evidence_digest"] = evidence, _digest(outer["evidence_digest"])
    return outer


def _receipt(value: object) -> dict[str, Any]:
    keys = {
        "receipt_version", "attestor_id", "sidecar_instance_id", "challenge", "closure_public_key_base64",
        "closure_public_key_digest", "checkout_instance_id", "ledger_generation_id", "amp_workspace_id",
        "amp_project_id", "canonical_root", "repository_id", "checkout_reuse", "setup_plugin_digest",
        "setup_wheel_digest", "amp_version", "enabled_tools", "enabled_tools_digest", "confirmation",
        "sidecar_fence", "sidecar_fence_digest", "competing_modifiers", "gateway_handshake_digest",
        "observed_at_ms", "expires_at_ms", "provenance", "limitations", "signature_base64",
    }
    out = _obj(value, keys)
    if safe_integer(out["receipt_version"]) != 1:
        _fail()
    for key in ("attestor_id", "sidecar_instance_id", "checkout_instance_id", "ledger_generation_id"):
        out[key] = _id(out[key])
    for key in ("amp_workspace_id", "amp_project_id"):
        out[key] = _bounded_text(out[key])
    out["amp_version"] = _text(out["amp_version"])
    out["challenge"], out["closure_public_key_base64"] = _b64(out["challenge"], 32), _b64(out["closure_public_key_base64"], 32)
    out["canonical_root"] = _absolute(out["canonical_root"])
    if out["repository_id"] is not None:
        normalized_repository = normalize_repository_id(out["repository_id"])
        if normalized_repository != out["repository_id"]:
            _fail("repository identity must be canonical")
        out["repository_id"] = normalized_repository
    reuse = _obj(out["checkout_reuse"], {"status", "prior_closure_digest"})
    if reuse["status"] not in {"fresh", "cleanly_closed"}:
        _fail()
    if (reuse["status"] == "fresh") != (reuse["prior_closure_digest"] is None):
        _fail()
    if reuse["prior_closure_digest"] is not None:
        reuse["prior_closure_digest"] = _digest(reuse["prior_closure_digest"])
    out["checkout_reuse"] = reuse
    for key in ("closure_public_key_digest", "setup_plugin_digest", "setup_wheel_digest", "enabled_tools_digest", "sidecar_fence_digest", "gateway_handshake_digest"):
        out[key] = _digest(out[key])
    out["enabled_tools"] = _sorted_unique(_array(out["enabled_tools"], _tool), lambda item: item["effective_name"])
    out["confirmation"], out["sidecar_fence"], out["competing_modifiers"] = _confirmation(out["confirmation"]), _fence(out["sidecar_fence"]), _modifiers(out["competing_modifiers"])
    out["observed_at_ms"], out["expires_at_ms"] = safe_integer(out["observed_at_ms"]), safe_integer(out["expires_at_ms"])
    out["provenance"] = _array(out["provenance"], _provenance)
    out["limitations"] = _array(out["limitations"], _text)
    out["signature_base64"] = _b64(out["signature_base64"], 64)
    return out


def validate_sidecar_handshake(value: object) -> dict[str, Any]:
    out = _obj(value, {"handshake_version", "sidecar_instance_id", "challenge", "closure_public_key_base64", "closure_public_key_digest", "issued_at_ms"})
    if safe_integer(out["handshake_version"]) != 1:
        _fail()
    out["sidecar_instance_id"] = _id(out["sidecar_instance_id"])
    out["challenge"] = _b64(out["challenge"], 32)
    out["closure_public_key_base64"] = _b64(out["closure_public_key_base64"], 32)
    out["closure_public_key_digest"] = _digest(out["closure_public_key_digest"])
    out["issued_at_ms"] = safe_integer(out["issued_at_ms"])
    return out


def validate_release_pin(value: object) -> dict[str, Any]:
    return _release(value)


def validate_readiness_receipt(value: object) -> dict[str, Any]:
    return _receipt(value)


def validate_repository_evidence(value: object) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("outcome") == "unavailable":
        out = _obj(value, {"outcome", "reason"})
        if out["reason"] != "non_git_read_only":
            _fail()
        return out
    out = _obj(value, {"outcome", "fingerprint"})
    if out["outcome"] != "available":
        _fail()
    fp = _obj(out["fingerprint"], {"repository_id", "canonical_root", "head_sha", "branch", "index_fingerprint", "fingerprinted_paths", "worktree_fingerprint"})
    repository_id = normalize_repository_id(fp["repository_id"])
    if repository_id != fp["repository_id"]:
        _fail("repository identity must be canonical")
    fp["repository_id"] = repository_id
    fp["canonical_root"] = _absolute(fp["canonical_root"])
    if not COMMIT.fullmatch(_text(fp["head_sha"])):
        _fail()
    if fp["branch"] is not None:
        fp["branch"] = _text(fp["branch"])
    fp["index_fingerprint"] = _digest(fp["index_fingerprint"])
    fp["worktree_fingerprint"] = _digest(fp["worktree_fingerprint"])
    fp["fingerprinted_paths"] = _sorted_unique(_array(fp["fingerprinted_paths"], _text), str.encode)
    out["fingerprint"] = fp
    return out


def _validate_bootstrap_payload(value: object) -> dict[str, Any]:
    out = _obj(value, {"project_root", "routing", "release_pin", "attestor_pin", "deployment_claim"})
    out["project_root"] = _absolute(out["project_root"])
    routing = out["routing"]
    if not isinstance(routing, dict) or routing.get("kind") not in {"git", "non_git_read_only"}:
        _fail()
    if routing["kind"] == "git":
        routing = _obj(routing, {"kind", "repository_id", "remote_name"})
        repository_id = normalize_repository_id(routing["repository_id"])
        if repository_id != routing["repository_id"]:
            _fail("repository identity must be canonical")
        routing["repository_id"], routing["remote_name"] = repository_id, _bounded_text(routing["remote_name"])
    else:
        routing = _obj(routing, {"kind"})
    out["routing"], out["release_pin"], out["attestor_pin"] = routing, _release(out["release_pin"]), _attestor(out["attestor_pin"])
    claim = _obj(out["deployment_claim"], {"plugin_path", "readiness_receipt", "readiness_receipt_digest"})
    claim["plugin_path"], claim["readiness_receipt"] = _absolute(claim["plugin_path"]), _receipt(claim["readiness_receipt"])
    claim["readiness_receipt_digest"] = _digest(claim["readiness_receipt_digest"])
    out["deployment_claim"] = claim
    return out


def validate_bootstrap_payload(value: object) -> dict[str, Any]:
    """Validate at the public boundary, consistently classifying malformed input."""
    try:
        return _validate_bootstrap_payload(value)
    except ReadinessProtocolError:
        raise
    except (CanonicalError, ValueError, KeyError, TypeError) as exc:
        raise ReadinessProtocolError("invalid readiness object") from exc
