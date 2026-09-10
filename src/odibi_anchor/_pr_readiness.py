"""Strict local PR configuration, objective checks, and managed draft rendering."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from odibi_anchor._repository_snapshot import RepositorySnapshot, validate_repository_snapshot
from odibi_anchor.planning._task_policy import EvidenceEntry, _freeze

PR_CONFIG_DEFAULTS: dict[str, Any] = {
    "schema_version": 1, "provider": "azure_devops", "pr_readiness": "recommended",
    "default_target_ref": "main",
    "title_prefixes": ["feat", "fix", "refactor", "docs", "chore", "test"],
    "work_item_required": False, "live_validation_required": False,
    "deny_globs": ["*.env", "*.pem", "*.key", ".agent_memory.db"],
    "max_changed_line_length": None,
}
_TYPES = {"schema_version": int, "provider": str, "pr_readiness": str,
          "default_target_ref": str, "title_prefixes": list, "work_item_required": bool,
          "live_validation_required": bool, "deny_globs": list}


@dataclass(frozen=True)
class PRCheck:
    """One readiness claim linked to deterministic or attested evidence."""

    id: str
    status: Literal["pass", "fail", "unknown", "na"]
    description: str
    evidence_ids: tuple[str, ...]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "provenance", _freeze(self.provenance))


@dataclass(frozen=True)
class PRReadinessResult:
    """Local readiness result; it never represents provider readiness."""

    ready: bool
    checks: tuple[PRCheck, ...]
    scope: Literal["local"] = "local"
    external_actions: Mapping[str, bool] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.external_actions is None:
            object.__setattr__(self, "external_actions", {"fetched": False, "pushed": False,
                                                          "pr_created": False, "work_item_updated": False})
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "external_actions", _freeze(self.external_actions))


def load_pr_config(artifact_root: str | os.PathLike[str], overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load exact-schema standard-library JSON and apply tightening-only overrides."""
    root = Path(artifact_root).resolve(strict=True)
    path = root / ".odibi-anchor" / "pr.json"
    value: dict[str, Any] = dict(PR_CONFIG_DEFAULTS)
    value["title_prefixes"] = list(value["title_prefixes"])
    value["deny_globs"] = list(value["deny_globs"])
    if path.is_symlink() and not path.exists():
        raise ValueError("pr.json must not be a dangling symlink")
    if path.exists():
        try:
            path.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ValueError("pr.json escapes artifact root") from exc
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in items:
                if key in result:
                    raise ValueError(f"duplicate pr.json key: {key}")
                result[key] = item
            return result
        def reject_constant(value: str) -> Any:
            raise ValueError(f"non-finite JSON number is forbidden: {value}")
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                         parse_constant=reject_constant)
        if not isinstance(raw, dict) or set(raw) != set(PR_CONFIG_DEFAULTS):
            raise ValueError("pr.json must contain exactly the version 1 schema keys")
        value = raw
    _validate_config(value)
    if overrides:
        unknown = set(overrides) - set(value)
        if unknown:
            raise ValueError(f"unknown PR config override keys: {sorted(unknown)}")
        candidate = {**value, **dict(overrides)}
        _validate_config(candidate)
        for fixed in ("schema_version", "provider", "default_target_ref"):
            if candidate[fixed] != value[fixed]:
                raise ValueError(f"{fixed} cannot be overridden")
        # Secrets and truthful live validation can only become stricter.
        if not set(candidate["deny_globs"]).issuperset(value["deny_globs"]):
            raise ValueError("deny_globs overrides may only add protections")
        if value["live_validation_required"] and not candidate["live_validation_required"]:
            raise ValueError("live_validation_required cannot be weakened")
        if value["work_item_required"] and not candidate["work_item_required"]:
            raise ValueError("work_item_required cannot be weakened")
        rank = {"disabled": 0, "recommended": 1, "required": 2}
        if rank[candidate["pr_readiness"]] < rank[value["pr_readiness"]]:
            raise ValueError("pr_readiness overrides may only tighten")
        current_line_length = value["max_changed_line_length"]
        candidate_line_length = candidate["max_changed_line_length"]
        if (current_line_length is not None and
                (candidate_line_length is None or candidate_line_length > current_line_length)):
            raise ValueError("max_changed_line_length overrides may only tighten")
        if not set(candidate["title_prefixes"]).issubset(value["title_prefixes"]):
            raise ValueError("title_prefixes overrides may only select configured prefixes")
        value = candidate
    return value


def _validate_config(value: Mapping[str, Any]) -> None:
    if set(value) != set(PR_CONFIG_DEFAULTS):
        raise ValueError("unknown or missing PR config keys")
    for key, expected in _TYPES.items():
        item = value[key]
        if type(item) is not expected:  # bool must not pass as int
            raise TypeError(f"pr.json {key} must be {expected.__name__}")
    if value["schema_version"] != 1 or value["provider"] != "azure_devops":
        raise ValueError("unsupported PR config schema/provider")
    target_ref = value["default_target_ref"]
    if (not target_ref or target_ref.startswith(("-", "/")) or target_ref.endswith((".", "/"))
            or ".." in target_ref or "@{" in target_ref or "//" in target_ref
            or any(char.isspace() or ord(char) < 32 or char in "~^:?*[\\" for char in target_ref)):
        raise ValueError("invalid default_target_ref")
    if value["pr_readiness"] not in {"disabled", "recommended", "required"}:
        raise ValueError("invalid pr_readiness")
    for key in ("title_prefixes", "deny_globs"):
        if not value[key] or any(type(item) is not str or not item for item in value[key]):
            raise TypeError(f"pr.json {key} must be a non-empty list of strings")
        if len(value[key]) != len(set(value[key])):
            raise ValueError(f"pr.json {key} values must be unique")
    line_length = value["max_changed_line_length"]
    if line_length is not None and (type(line_length) is not int or line_length < 1):
        raise ValueError("max_changed_line_length must be null or a positive integer")


def _check(identifier: str, status: str, description: str, **provenance: Any) -> PRCheck:
    evidence = f"pr-check:{identifier}"
    return PRCheck(identifier, status, description, (evidence,),
                   {"evidence_id": evidence, "observed_at": datetime.now(timezone.utc).isoformat(), **provenance})


def _intersects(node: ast.AST, intervals: list[tuple[int, int]]) -> bool:
    start, end = getattr(node, "lineno", 1), getattr(node, "end_lineno", getattr(node, "lineno", 1))
    return any(start <= right and end >= left for left, right in intervals)


_DocumentableNode = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _symbols(tree: ast.Module) -> dict[str, _DocumentableNode]:
    """Index module, class, and callable definitions by qualified lexical identity."""
    result: dict[str, _DocumentableNode] = {"<module>": tree}
    def visit(body: list[ast.stmt], prefix: str = "") -> None:
        for item in body:
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{item.name}" if prefix else item.name
                result[name] = item
                visit(item.body, name)
    visit(tree.body)
    return result


def _structural(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _repository_line_length(root: Path, config: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Resolve a deterministic line length from repository tooling, then explicit PR policy."""
    pyproject = root / "pyproject.toml"
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        document = {}
    tool = document.get("tool", {}) if isinstance(document, dict) else {}
    for name, key in (("ruff", "line-length"), ("black", "line-length")):
        section = tool.get(name, {}) if isinstance(tool, dict) else {}
        value = section.get(key) if isinstance(section, dict) else None
        if type(value) is int and value > 0:
            return value, f"pyproject.toml:[tool.{name}].{key}"
    configured = config.get("max_changed_line_length")
    if type(configured) is int and configured > 0:
        return configured, ".odibi-anchor/pr.json:max_changed_line_length"
    return None, None


def _base_source(snapshot: RepositorySnapshot, name: str) -> bytes | None:
    """Read one base blob using Git's pathspec-free object identity syntax."""
    if snapshot.merge_base_sha is None or name.startswith(("/", "-")) or ".." in Path(name).parts:
        return None
    result = subprocess.run(
        ["git", "show", f"{snapshot.merge_base_sha}:{name}"], cwd=snapshot.target_worktree,
        capture_output=True, timeout=30, check=False,
    )
    return result.stdout if result.returncode == 0 else None


def evaluate_pr_readiness(
    snapshot: RepositorySnapshot, config: Mapping[str, Any], *,
    attestations: tuple[EvidenceEntry, ...] = (), intended_pr_paths: tuple[str, ...] = (),
) -> PRReadinessResult:
    """Evaluate deterministic EAAI checks only over changed Python source and symbols."""
    _validate_config(config)
    fresh, stale = validate_repository_snapshot(snapshot)
    checks = [_check("snapshot.fresh", "pass" if fresh else "fail", "Snapshot matches local state.", stale=stale)]
    complete = snapshot.target_sha is not None and snapshot.merge_base_sha is not None
    checks.append(_check("snapshot.base", "pass" if complete else "unknown", "Configured local base and merge-base exist."))
    clean = not (snapshot.staged_paths or snapshot.unstaged_paths or snapshot.untracked_paths)
    committed = bool(snapshot.merge_base_sha and snapshot.head_sha != snapshot.merge_base_sha)
    checks.append(_check("snapshot.committed", "pass" if clean and committed else "fail",
                         "Intended PR changes are committed.", uncommitted=sorted(set(snapshot.staged_paths + snapshot.unstaged_paths + snapshot.untracked_paths))))
    checks.append(_check("snapshot.conflict", "pass" if snapshot.local_conflict_result == "clear" else
                         "fail" if snapshot.local_conflict_result == "conflict" else "unknown",
                         "Conflict status is against recorded local SHAs only."))
    denied = [path for path in snapshot.changed_paths if any(fnmatch(path, pattern) or fnmatch(Path(path).name, pattern)
                                                                  for pattern in config["deny_globs"])]
    unrelated = sorted(set(snapshot.changed_paths) - set(intended_pr_paths)) if intended_pr_paths else []
    checks.append(_check("paths.denied", "fail" if denied else "pass", "Denied files are absent.", paths=denied))
    checks.append(_check("paths.scope", "fail" if unrelated else "pass" if intended_pr_paths else "unknown",
                         "Changed files match the attested intended scope.", paths=unrelated))

    root = Path(snapshot.target_worktree)
    line_length, line_length_source = _repository_line_length(root, config)
    ranges: dict[str, list[tuple[int, int]]] = {}
    for item in snapshot.changed_line_ranges:
        ranges.setdefault(item.path, []).append((item.start, item.end))
    for name in sorted(path for path in snapshot.changed_paths if path.endswith(".py")):
        path = root / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            base_bytes = _base_source(snapshot, name)
            if base_bytes is None:
                checks.append(_check(f"python.deleted:{name}", "unknown",
                                     "Deleted Python path had no readable merge-base blob."))
            else:
                try:
                    ast.parse(base_bytes.decode("utf-8"), filename=name)
                except (UnicodeError, SyntaxError) as exc:
                    checks.append(_check(f"python.deleted:{name}", "unknown",
                                         "Deleted Python base blob could not be parsed.",
                                         error=type(exc).__name__))
                else:
                    checks.append(_check(f"python.deleted:{name}", "pass",
                                         "Deleted Python path was verified against the merge-base blob."))
            continue
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
                target.relative_to(root)
            except (FileNotFoundError, ValueError) as exc:
                checks.append(_check(f"python.symlink:{name}", "fail",
                                     "Python symlink must resolve inside the repository.",
                                     error=type(exc).__name__))
            else:
                checks.append(_check(f"python.symlink:{name}", "pass",
                                     "Contained Python symlink was recorded without parsing its target."))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            checks.append(_check(f"python.type:{name}", "fail",
                                 "Changed Python path must be a regular file or contained symlink."))
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=name)
        except (OSError, UnicodeError, SyntaxError) as exc:
            checks.append(_check(f"python.parse:{name}", "fail", "Changed Python module parses.", error=type(exc).__name__))
            continue
        checks.append(_check(f"python.parse:{name}", "pass", "Changed Python module parses."))
        intervals = ranges.get(name, [])
        current_symbols = _symbols(tree)
        base_bytes = _base_source(snapshot, name)
        base_symbols: dict[str, _DocumentableNode] = {}
        if base_bytes is not None:
            try:
                base_symbols = _symbols(ast.parse(base_bytes.decode("utf-8"), filename=name))
            except (UnicodeError, SyntaxError):
                checks.append(_check(f"python.base-parse:{name}", "unknown",
                                     "Base Python module could not be structurally compared."))
        changed_symbols = {
            symbol for symbol, node in current_symbols.items()
            if symbol == "<module>" or symbol not in base_symbols
            or _structural(node) != _structural(base_symbols[symbol])
            or _intersects(node, intervals)
            or any(_intersects(decorator, intervals) for decorator in getattr(node, "decorator_list", ()))
        }
        deleted_symbols = sorted(set(base_symbols) - set(current_symbols))
        checks.append(_check(f"python.deleted-symbols:{name}", "pass", "Deleted symbols were mechanically compared.",
                             symbols=deleted_symbols))
        documented_symbols = sorted(
            symbol for symbol in changed_symbols
            if ast.get_docstring(current_symbols[symbol], clean=False)
        )
        checks.append(_check(
            f"subjective.docstring-usefulness:{name}", "unknown",
            "Docstring presence and format remain advisory unless repository tooling enforces them.",
            documented_symbols=documented_symbols,
        ))
        body = tree.body
        first_code = next((index for index, node in enumerate(body)
                           if not isinstance(node, (ast.Import, ast.ImportFrom)) and
                           not (index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                                and isinstance(node.value.value, str))), len(body))
        late = any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in body[first_code:])
        checks.append(_check(f"python.imports:{name}", "fail" if late else "pass", "Imports are at module top."))
        lines = source.splitlines()
        if line_length is None:
            checks.append(_check(
                f"subjective.line-length:{name}", "unknown",
                "Line length remains advisory because repository tooling defines no limit.",
            ))
        else:
            long_lines = [
                line for left, right in intervals for line in range(left, min(right, len(lines)) + 1)
                if len(lines[line - 1]) > line_length
            ]
            checks.append(_check(
                f"python.line-length:{name}", "fail" if long_lines else "pass",
                "Changed lines respect the repository-configured length.",
                lines=sorted(set(long_lines)), limit=line_length, source=line_length_source,
            ))

    if any(not isinstance(item, EvidenceEntry) for item in attestations):
        raise TypeError("attestations must be typed EvidenceEntry values")
    allowed_kinds = {"work-item", "live-validation", "docstring-usefulness", "readability", "abstraction",
                     "shared-value", "jargon", "usage-quality", "reviewer-comprehension", "narrative"}
    ids = [item.id for item in attestations]
    if any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids) or len(ids) != len(set(ids)):
        raise ValueError("attestation IDs must be nonempty and unique")
    def valid_attestation(item: EvidenceEntry) -> bool:
        try:
            observed = datetime.fromisoformat(item.observed_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return False
        generic_valid = bool(
            item.kind in allowed_kinds and item.status == "pass" and isinstance(item.source, str) and item.source.strip()
            and isinstance(item.provenance.get("attestor"), str)
            and item.provenance["attestor"].strip() and observed.tzinfo is not None
            and observed.utcoffset() == timezone.utc.utcoffset(observed) and observed.isoformat().endswith("+00:00")
        )
        if not generic_valid or item.kind != "work-item":
            return generic_valid
        work_item_id = item.provenance.get("work_item_id")
        provider = item.provenance.get("provider")
        provider_id = item.provenance.get("provider_id")
        provider_url = item.provenance.get("provider_url")
        fingerprint = item.provenance.get("fingerprint")
        operations = item.provenance.get("operations")
        parsed = urlsplit(provider_url) if isinstance(provider_url, str) else None
        return bool(
            isinstance(work_item_id, str) and re.fullmatch(r"WI-\d{4}-\d{4}", work_item_id)
            and isinstance(provider, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", provider)
            and isinstance(provider_id, str) and provider_id.strip()
            and parsed is not None and parsed.scheme == "https" and parsed.netloc
            and item.source == provider_url
            and isinstance(fingerprint, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint)
            and isinstance(operations, (list, tuple)) and bool(operations)
            and all(operation in {"create_tasks", "update_tasks", "add_comment"} for operation in operations)
        )
    valid_attestations = tuple(item for item in attestations if valid_attestation(item))
    work_attestations = tuple(item for item in valid_attestations if item.kind == "work-item")
    work_evidence = tuple(item.id for item in work_attestations)
    work_status = "pass" if work_evidence else "unknown" if config["work_item_required"] else "na"
    checks.append(PRCheck("work-item", work_status, "Required work item has stored evidence.", work_evidence,
                          {"observed_at": datetime.now(timezone.utc).isoformat(),
                           "links": tuple({
                               "evidence_id": item.id,
                               "work_item_id": item.provenance.get("work_item_id"),
                               "provider": item.provenance.get("provider"),
                               "provider_id": item.provenance.get("provider_id"),
                               "provider_url": item.provenance.get("provider_url") or item.source,
                           } for item in work_attestations)}))
    live = any(item.kind == "live-validation" and item.provenance.get("environment") not in
               {None, "local", "mock", "fake", "local-spark"} for item in valid_attestations)
    live_evidence = tuple(item.id for item in valid_attestations if item.kind == "live-validation" and
                          item.provenance.get("environment") not in {None, "local", "mock", "fake", "local-spark"})
    live_status = "pass" if live else "unknown" if config["live_validation_required"] else "na"
    checks.append(PRCheck("live-validation", live_status, "Target-environment validation has stored evidence.",
                          live_evidence, {"observed_at": datetime.now(timezone.utc).isoformat()}))
    for subject in ("docstring-usefulness", "readability", "abstraction", "shared-value", "jargon",
                    "usage-quality", "reviewer-comprehension", "narrative"):
        subject_evidence = tuple(item.id for item in valid_attestations if item.kind == subject)
        checks.append(PRCheck(f"subjective.{subject}", "pass" if subject_evidence else "unknown",
                              "Human judgment requires source/time attestation.", subject_evidence,
                              {"observed_at": datetime.now(timezone.utc).isoformat()}))
    blocking = any(item.status == "fail" or (item.status == "unknown" and not item.id.startswith("subjective."))
                   for item in checks)
    return PRReadinessResult(not blocking and bool(snapshot.committed_diff_range), tuple(checks))


def pr_checks_to_evidence(result: PRReadinessResult) -> tuple[EvidenceEntry, ...]:
    """Convert checks to uniquely identified mechanical ledger evidence with provenance intact."""
    observed = datetime.now(timezone.utc).isoformat()
    mechanical = tuple(check for check in result.checks
                       if check.evidence_ids == (f"pr-check:{check.id}",))
    entries = tuple(EvidenceEntry(
        check.evidence_ids[0], "pr-readiness-mechanical", check.status if check.status != "na" else "unknown",
        "odibi_anchor._pr_readiness", str(check.provenance.get("observed_at", observed)),
        {"check_id": check.id, "description": check.description, "evidence_ids": check.evidence_ids,
         "check_provenance": check.provenance},
    ) for check in mechanical)
    if len({item.id for item in entries}) != len(entries):
        raise ValueError("PR check IDs must be unique")
    return entries


_START = "<!-- odibi-anchor:generated:start -->"
_END = "<!-- odibi-anchor:generated:end -->"


def render_pr_draft(
    artifact_root: str | os.PathLike[str], snapshot: RepositorySnapshot, result: PRReadinessResult, *,
    title: str = "chore: describe local changes",
    narratives: Mapping[str, Mapping[str, Any]] | None = None,
    title_prefixes: tuple[str, ...] = tuple(PR_CONFIG_DEFAULTS["title_prefixes"]),
    post_persist: Callable[[Path, str], None] | None = None,
) -> Path:
    """Atomically render marker-owned UTF-8 text while preserving human-authored text."""
    root = Path(artifact_root).resolve(strict=True)
    branch_identity = snapshot.branch or f"detached-{snapshot.head_sha}"
    slug_base = re.sub(r"[^a-zA-Z0-9._-]+", "-", branch_identity).strip(".-") or "branch"
    slug = f"{slug_base}-{hashlib.sha256(branch_identity.encode('utf-8')).hexdigest()[:12]}"
    destination = root / "pull_requests" / slug / "PR_DRAFT.md"
    relative_parent = destination.parent.relative_to(root)
    cursor = root
    for part in relative_parent.parts:
        cursor /= part
        if cursor.is_symlink() or (cursor.exists() and cursor.resolve(strict=True) != cursor.absolute()):
            raise ValueError("draft parent contains a symlink or reparse point")
    if destination.is_symlink():
        raise ValueError("draft destination must not be a symlink")
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError("draft path escapes artifact root") from exc
    if "\n" in title or "\r" in title or not any(title.startswith(prefix + ":") for prefix in title_prefixes):
        raise ValueError("draft title must use an allowed prefix and one line")
    if _START in title or _END in title:
        raise ValueError("generated marker injection rejected")
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", destination.relative_to(root).as_posix()],
        cwd=root, capture_output=True, check=False,
    )
    if tracked.returncode == 0:
        raise ValueError("tracked draft destinations are not managed artifacts")
    narrative = dict(narratives or {})
    sections = ["Summary", "Design decisions", "Usage", "Validation", "Not yet validated", "Scope",
                "Work item", "Branch and conflict status", "Changed-file hygiene",
                "Readability and documentation", "Checklist"]
    lines = [_START, f"# {title}", "",
             "> Local and stored attested evidence only; no remote state was queried while rendering this draft.", ""]
    for section in sections:
        claim = narrative.get(section, {})
        if section == "Work item" and not claim:
            work_check = next((check for check in result.checks
                               if check.id == "work-item" and check.status == "pass"), None)
            links = tuple(work_check.provenance.get("links", ())) if work_check else ()
            if links:
                claim = {
                    "text": "\n".join(
                        f"- [{link.get('work_item_id') or link.get('provider_id')}]"
                        f"({link.get('provider_url')}) on {link.get('provider')}"
                        for link in links
                    ),
                    "evidence_ids": tuple(link["evidence_id"] for link in links),
                }
        text = claim.get("text") if isinstance(claim, Mapping) else None
        ids = tuple(claim.get("evidence_ids", ())) if isinstance(claim, Mapping) else ()
        valid_ids = {evidence for check in result.checks if check.status == "pass"
                     for evidence in check.evidence_ids}
        if any(_START in str(value) or _END in str(value) for value in (section, text, *ids)):
            raise ValueError("generated marker injection rejected")
        rendered = str(text) if text and ids and set(ids).issubset(valid_ids) else "Unknown — unsupported by validated cited evidence."
        lines.extend([f"## {section}", "", rendered, ""])
    lines.extend(["### Evidence", ""])
    for item in result.checks:
        if any(_START in str(value) or _END in str(value)
               for value in (item.status, item.id, item.description, *item.evidence_ids)):
            raise ValueError("generated marker injection rejected")
        lines.append(f"- [{item.status}] `{item.id}` — {item.description} ({', '.join(item.evidence_ids)})")
    lines.extend(["", "### External actions", "", "- fetched: false", "- pushed: false",
                  "- PR created: false", "- work item updated: false", _END])
    generated = "\n".join(lines) + "\n"
    # All interpolated values are validated before creating directories.
    destination.parent.mkdir(parents=True, exist_ok=True)
    prior_exists = destination.exists()
    prior_bytes = destination.read_bytes() if prior_exists else b""
    prior = prior_bytes.decode("utf-8")
    if prior.count(_START) != prior.count(_END) or prior.count(_START) > 1 or (
            _START in prior and prior.index(_START) > prior.index(_END)):
        raise ValueError("prior draft has malformed or duplicate generated markers")
    if _START in prior and _END in prior and prior.index(_START) < prior.index(_END):
        start = prior_bytes.index(_START.encode())
        end = prior_bytes.index(_END.encode()) + len(_END.encode())
        content = prior_bytes[:start] + generated.encode("utf-8") + prior_bytes[end:]
    elif prior:
        content = prior_bytes + (b"" if prior_bytes.endswith((b"\n", b"\r")) else b"\n") + b"\n" + generated.encode("utf-8")
    else:
        content = generated.encode("utf-8")
    temporary: str | None = None
    rollback: str | None = None
    try:
        if prior_exists:
            with tempfile.NamedTemporaryFile(
                "wb", delete=False, dir=destination.parent,
                prefix=".PR_DRAFT.rollback.", suffix=".tmp",
            ) as handle:
                rollback = handle.name
                handle.write(prior_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        with tempfile.NamedTemporaryFile("wb", delete=False,
                                         dir=destination.parent, prefix=".PR_DRAFT.", suffix=".tmp") as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            if post_persist is not None:
                post_persist(destination, hashlib.sha256(content).hexdigest())
        except Exception:
            if prior_exists:
                os.replace(rollback, destination)
                rollback = None
            else:
                destination.unlink(missing_ok=True)
            raise
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        if rollback and os.path.exists(rollback):
            os.unlink(rollback)
    return destination


# Explicit names useful to integrations without adding a dispatcher action.
load_repository_pr_config = load_pr_config
check_pr_readiness = evaluate_pr_readiness
write_pr_draft = render_pr_draft
