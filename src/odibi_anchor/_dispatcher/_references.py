"""Validated, offline access to the distributed engineering reference library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

_REGISTRY_KEYS = {"schema_version", "library_version", "research_date", "entries"}
_ENTRY_KEYS = {
    "id", "title", "path", "summary", "topics", "keywords", "versions",
    "verification", "sources", "offline_sources", "refresh_triggers",
}
_PROFILE_ENTRY_KEYS = _ENTRY_KEYS | {"profile_version", "applicability"}
_APPLICABILITY_KEYS = {
    "execution_modes", "domains", "traits", "path_suffixes", "path_names",
    "repository_signals",
}
_VERSION_KEYS = {"name", "range", "baseline"}
_SOURCE_KEYS = {"title", "url"}
_LICENSED_SOURCE_KEYS = _SOURCE_KEYS | {"license"}
_VERIFICATION = {
    "documented", "source-inspected", "runtime-verified", "host-verified",
}
_ID = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
_TOKEN = re.compile(r"[a-z0-9_]+(?:[.+#-][a-z0-9_]+)*")
_MAX_REGISTRY_BYTES = 256_000
_MAX_REFERENCE_BYTES = 512_000
_MAX_MANIFEST_BYTES = 2_000_000
_MAX_SNAPSHOT_FILES = 1_000
_MAX_SNAPSHOT_BYTES = 16_000_000
_MAX_SNAPSHOT_FILE_BYTES = 8_000_000
_MAX_SECTION_BYTES = 32_768
_MAX_SECTION_LINES = 200
_MAX_SECTIONS = 20_000
_MAX_PREVIEW_CHARS = 360
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_RST_ADORNMENT = re.compile(r"^[=\-`:'\"~^_*+#<>]{3,}\s*$")
_SNAPSHOT_FILE_KEYS = {"path", "bytes", "sha256"}
_SNAPSHOT_KEYS = {
    "name", "repository", "revision", "version", "license", "archive_url",
    "archive_sha256", "files",
}


def _nonempty(value: Any, label: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _string_list(value: Any, label: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty list with at most {maximum} entries")
    result = [_nonempty(item, label, maximum=300) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return result


def _contained_file(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix != ".md":
        raise ValueError(f"unsafe reference path: {relative!r}")
    path = root / candidate
    if path.is_symlink():
        raise ValueError(f"reference paths cannot be symlinks: {relative!r}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"reference file is missing: {relative!r}") from exc
    if root.resolve() not in resolved.parents or not resolved.is_file():
        raise ValueError(f"reference path escapes the library: {relative!r}")
    return resolved


def load_reference_registry(references_dir: str | Path) -> dict[str, Any]:
    """Load and validate the closed reference registry and all declared files."""
    root = Path(references_dir).resolve()
    registry_path = root / "registry.json"
    if registry_path.is_symlink() or not registry_path.is_file():
        raise ValueError("reference registry must be a regular non-symlink file")
    registry_before = registry_path.stat()
    raw = registry_path.read_bytes()
    registry_after = registry_path.stat()
    if (
        registry_before.st_dev, registry_before.st_ino, registry_before.st_size,
        registry_before.st_mtime_ns, registry_before.st_ctime_ns,
    ) != (
        registry_after.st_dev, registry_after.st_ino, registry_after.st_size,
        registry_after.st_mtime_ns, registry_after.st_ctime_ns,
    ):
        raise RuntimeError("reference registry changed while being read")
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise ValueError("reference registry exceeds the size limit")
    try:
        registry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("reference registry must be valid UTF-8 JSON") from exc
    if not isinstance(registry, dict) or set(registry) != _REGISTRY_KEYS:
        raise ValueError("reference registry has unsupported or missing fields")
    if registry["schema_version"] != 1:
        raise ValueError("unsupported reference registry schema_version")
    _nonempty(registry["library_version"], "library_version", maximum=40)
    _nonempty(registry["research_date"], "research_date", maximum=40)
    entries = registry["entries"]
    if not isinstance(entries, list) or not entries or len(entries) > 200:
        raise ValueError("entries must be a non-empty list with at most 200 items")
    ids: set[str] = set()
    paths: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or (set(entry) != _ENTRY_KEYS and set(entry) != _PROFILE_ENTRY_KEYS)
        ):
            raise ValueError("reference entry has unsupported or missing fields")
        reference_id = _nonempty(entry["id"], "id", maximum=100)
        if not _ID.fullmatch(reference_id) or reference_id in ids:
            raise ValueError(f"invalid or duplicate reference id: {reference_id!r}")
        ids.add(reference_id)
        relative = _nonempty(entry["path"], "path", maximum=300)
        if relative in paths:
            raise ValueError(f"duplicate reference path: {relative!r}")
        paths.add(relative)
        _contained_file(root, relative)
        _nonempty(entry["title"], "title", maximum=200)
        _nonempty(entry["summary"], "summary")
        _string_list(entry["topics"], "topics")
        _string_list(entry["keywords"], "keywords")
        if set(entry) == _PROFILE_ENTRY_KEYS:
            _nonempty(entry["profile_version"], "profile_version", maximum=100)
            applicability = entry["applicability"]
            if not isinstance(applicability, dict) or set(applicability) != _APPLICABILITY_KEYS:
                raise ValueError(f"invalid applicability metadata for {reference_id}")
            for key in _APPLICABILITY_KEYS:
                values = applicability[key]
                if not isinstance(values, list) or len(values) > 64:
                    raise ValueError(f"applicability.{key} must be a list for {reference_id}")
                normalized = [_nonempty(value, f"applicability.{key}", maximum=100) for value in values]
                if len(set(normalized)) != len(normalized):
                    raise ValueError(f"applicability.{key} contains duplicates for {reference_id}")
        elif reference_id.startswith("development."):
            raise ValueError(f"development reference requires profile metadata: {reference_id}")
        offline_sources = _string_list(entry["offline_sources"], "offline_sources")
        for offline_source in offline_sources:
            source_path = Path(offline_source)
            if source_path.is_absolute() or ".." in source_path.parts:
                raise ValueError(f"unsafe offline source path for {reference_id}")
            current = root
            for part in source_path.parts:
                current /= part
                if current.is_symlink():
                    raise ValueError(f"offline sources cannot use symlinks for {reference_id}")
            resolved_source = (root / source_path).resolve(strict=False)
            if root.resolve() not in resolved_source.parents or not resolved_source.exists():
                raise ValueError(f"offline source is missing for {reference_id}: {offline_source!r}")
        _string_list(entry["refresh_triggers"], "refresh_triggers")
        verification = _string_list(entry["verification"], "verification")
        if not set(verification).issubset(_VERIFICATION):
            raise ValueError(f"unsupported verification state for {reference_id}")
        versions = entry["versions"]
        if not isinstance(versions, list) or not versions:
            raise ValueError(f"versions must be non-empty for {reference_id}")
        for version in versions:
            if not isinstance(version, dict) or set(version) != _VERSION_KEYS:
                raise ValueError(f"invalid version entry for {reference_id}")
            for key in _VERSION_KEYS:
                _nonempty(version[key], f"versions.{key}", maximum=100)
        sources = entry["sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"sources must be non-empty for {reference_id}")
        for source in sources:
            if not isinstance(source, dict) or set(source) not in {
                frozenset(_SOURCE_KEYS), frozenset(_LICENSED_SOURCE_KEYS),
            }:
                raise ValueError(f"invalid source entry for {reference_id}")
            _nonempty(source["title"], "sources.title", maximum=300)
            url = _nonempty(source["url"], "sources.url", maximum=1000)
            if not url.startswith("https://"):
                raise ValueError(f"source URLs must use HTTPS for {reference_id}")
            if "license" in source:
                _nonempty(source["license"], "sources.license", maximum=500)
    return registry


def _snapshot_manifest(references_dir: str | Path) -> tuple[dict[str, Any], str]:
    root = Path(references_dir).resolve()
    path = root / "snapshots" / "manifest.json"
    raw = _read_regular_file(root, path, limit=_MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("snapshot manifest must be valid UTF-8 JSON") from exc
    expected = {"schema_version", "generated_on", "policy", "snapshots", "deneb_public_docs"}
    if not isinstance(manifest, dict) or set(manifest) != expected or manifest["schema_version"] != 1:
        raise ValueError("snapshot manifest has unsupported or missing fields")
    snapshots = manifest["snapshots"]
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("snapshot manifest must contain snapshots")
    names: set[str] = set()
    paths: set[str] = set()
    total_bytes = 0
    total_files = 0
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_KEYS:
            raise ValueError("snapshot manifest entry has unsupported or missing fields")
        name = _nonempty(snapshot["name"], "snapshot.name", maximum=150)
        if name in names:
            raise ValueError(f"duplicate snapshot name: {name!r}")
        names.add(name)
        for field in ("repository", "revision", "version", "license", "archive_url"):
            _nonempty(snapshot[field], f"snapshot.{field}", maximum=1000)
        if not _DIGEST.fullmatch(str(snapshot["archive_sha256"])):
            raise ValueError(f"invalid archive digest for {name}")
        files = snapshot["files"]
        if not isinstance(files, list) or not files:
            raise ValueError(f"snapshot files must be non-empty for {name}")
        for file in files:
            if not isinstance(file, dict) or set(file) != _SNAPSHOT_FILE_KEYS:
                raise ValueError(f"invalid snapshot file entry for {name}")
            relative = _safe_relative_path(file["path"], label="snapshot file")
            if not relative.startswith(f"{name}/"):
                raise ValueError(f"snapshot file is outside its declared snapshot: {relative!r}")
            folded = relative.casefold()
            if folded in paths:
                raise ValueError(f"duplicate snapshot file path: {relative!r}")
            paths.add(folded)
            size = file["bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_SNAPSHOT_FILE_BYTES:
                raise ValueError(f"invalid snapshot file size: {relative!r}")
            if not _DIGEST.fullmatch(str(file["sha256"])):
                raise ValueError(f"invalid snapshot file digest: {relative!r}")
            total_bytes += size
            total_files += 1
    if total_files > _MAX_SNAPSHOT_FILES or total_bytes > _MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot manifest exceeds corpus limits")
    return manifest, hashlib.sha256(raw).hexdigest()


def _safe_relative_path(value: Any, *, label: str) -> str:
    relative = _nonempty(value, label, maximum=500)
    candidate = Path(relative)
    if (
        candidate.is_absolute() or ".." in candidate.parts or not candidate.parts
        or "\\" in relative or "\x00" in relative or any(not part for part in candidate.parts)
    ):
        raise ValueError(f"unsafe {label} path: {relative!r}")
    return candidate.as_posix()


def _read_regular_file(root: Path, path: Path, *, limit: int) -> bytes:
    root = root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("reference resource escapes the library") from exc
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise ValueError(f"reference resource is missing: {relative.as_posix()!r}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError(f"reference resources cannot use symlinks: {relative.as_posix()!r}")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError(f"reference resource exceeds its size limit: {relative.as_posix()!r}")
    with path.open("rb") as handle:
        descriptor_before = os.fstat(handle.fileno())
        raw = handle.read(limit + 1)
        descriptor_after = os.fstat(handle.fileno())
    after = path.stat()

    def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
        return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns

    def stable_identity(item: os.stat_result) -> tuple[int, int, int, int]:
        return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns

    if (
        stable_identity(before) != stable_identity(descriptor_before)
        or identity(descriptor_before) != identity(descriptor_after)
        or stable_identity(descriptor_after) != stable_identity(after)
    ):
        raise RuntimeError("reference resource changed while being read")
    if len(raw) > limit:
        raise ValueError(f"reference resource exceeds its size limit: {relative.as_posix()!r}")
    return raw


def _allowed_snapshot_roots(registry: dict[str, Any], reference_id: str | None) -> dict[str, set[str]]:
    entries = registry["entries"]
    if reference_id is not None:
        entries = [entry for entry in entries if entry["id"] == reference_id]
        if not entries:
            raise ValueError(f"unknown reference id: {reference_id!r}")
    roots: dict[str, set[str]] = {}
    for entry in entries:
        for value in entry["offline_sources"]:
            relative = _safe_relative_path(value, label="offline source")
            if not relative.startswith("snapshots/"):
                continue
            source = relative.removeprefix("snapshots/").rstrip("/")
            roots.setdefault(entry["id"], set()).add(source)
    return roots


def _reference_ids_for_path(roots: dict[str, set[str]], relative: str) -> tuple[str, ...]:
    return tuple(sorted(
        reference_id
        for reference_id, prefixes in roots.items()
        if any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in prefixes)
    ))


def _heading_sections(text: str, suffix: str) -> list[tuple[str, tuple[str, ...], int, int]]:
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, str, int]] = []
    if suffix in {".md", ".markdown"}:
        fenced = False
        for index, line in enumerate(lines):
            stripped = line.rstrip("\r\n")
            if stripped.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                continue
            match = _MARKDOWN_HEADING.match(stripped)
            if match:
                headings.append((index, match.group(2).strip(), len(match.group(1))))
            elif index + 1 < len(lines) and stripped.strip() and re.fullmatch(r"\s*(=+|-+)\s*", lines[index + 1]):
                headings.append((index, stripped.strip(), 1 if "=" in lines[index + 1] else 2))
    elif suffix == ".rst":
        styles: dict[str, int] = {}
        for index in range(len(lines) - 1):
            title = lines[index].rstrip("\r\n")
            adornment = lines[index + 1].rstrip("\r\n")
            if title.startswith((" ", "\t")) or not title.strip() or not _RST_ADORNMENT.fullmatch(adornment):
                continue
            if len(adornment.strip()) < len(title.strip()):
                continue
            style = adornment.strip()[0]
            level = styles.setdefault(style, len(styles) + 1)
            headings.append((index, title.strip(), level))
    if not headings:
        return [("Document", ("Document",), 0, len(lines))]
    sections: list[tuple[str, tuple[str, ...], int, int]] = []
    if headings[0][0] > 0:
        sections.append(("Preamble", ("Preamble",), 0, headings[0][0]))
    stack: list[tuple[int, str]] = []
    for position, (start, title, level) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        sections.append((title, tuple(value for _, value in stack), start, end))
    return sections


def _bounded_prefix(value: str, maximum_bytes: int) -> tuple[str, str]:
    if len(value.encode("utf-8")) <= maximum_bytes:
        return value, ""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if len(value[:middle].encode("utf-8")) <= maximum_bytes:
            low = middle
        else:
            high = middle - 1
    return value[:low], value[low:]


def _split_section(
    lines: list[str], start: int, end: int,
) -> list[tuple[int, int, str]]:
    chunks: list[tuple[int, int, str]] = []
    current: list[str] = []
    current_bytes = 0
    current_start = start
    current_end = start
    for line_index in range(start, end):
        remainder = lines[line_index]
        while remainder:
            piece, remainder = _bounded_prefix(remainder, _MAX_SECTION_BYTES)
            piece_bytes = len(piece.encode("utf-8"))
            if current and (
                current_bytes + piece_bytes > _MAX_SECTION_BYTES
                or len(current) >= _MAX_SECTION_LINES
            ):
                chunks.append((current_start, current_end, "".join(current)))
                current, current_bytes, current_start = [], 0, line_index
            current.append(piece)
            current_bytes += piece_bytes
            current_end = line_index + 1
            if remainder:
                chunks.append((current_start, current_end, "".join(current)))
                current, current_bytes, current_start = [], 0, line_index
    if current or not chunks:
        chunks.append((current_start, current_end, "".join(current)))
    return chunks


def _section_id(
    snapshot: str, path: str, heading_path: tuple[str, ...], start: int, part: int,
) -> str:
    locator = "\0".join((snapshot, path, " > ".join(heading_path), str(start), str(part)))
    return f"sec-v1-{hashlib.sha256(locator.encode()).hexdigest()[:48]}"


def _snapshot_sections(
    references_dir: str | Path, *, reference_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    root = Path(references_dir).resolve()
    registry = load_reference_registry(root)
    roots = _allowed_snapshot_roots(registry, reference_id)
    manifest, manifest_sha256 = _snapshot_manifest(root)
    snapshot_root = root / "snapshots"
    sections: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for snapshot in manifest["snapshots"]:
        for file in sorted(snapshot["files"], key=lambda item: item["path"]):
            relative = file["path"]
            reference_ids = _reference_ids_for_path(roots, relative)
            if not reference_ids:
                continue
            raw = _read_regular_file(snapshot_root, snapshot_root / relative, limit=_MAX_SNAPSHOT_FILE_BYTES)
            if len(raw) != file["bytes"] or hashlib.sha256(raw).hexdigest() != file["sha256"]:
                raise ValueError(f"snapshot file does not match its manifest: {relative!r}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"snapshot file must be UTF-8: {relative!r}") from exc
            suffix = Path(relative).suffix.casefold()
            lines = text.splitlines(keepends=True)
            for heading, heading_path, start, end in _heading_sections(text, suffix):
                for part, (chunk_start, chunk_end, content) in enumerate(_split_section(lines, start, end)):
                    section_id = _section_id(snapshot["name"], relative, heading_path, start, part)
                    if section_id in seen_ids:
                        raise RuntimeError(f"snapshot section id collision: {section_id}")
                    seen_ids.add(section_id)
                    sections.append({
                        "section_id": section_id,
                        "reference_ids": reference_ids,
                        "snapshot": snapshot["name"],
                        "repository": snapshot["repository"],
                        "revision": snapshot["revision"],
                        "version": snapshot["version"],
                        "path": relative,
                        "heading": heading,
                        "heading_path": heading_path,
                        "part": part,
                        "start_line": chunk_start + 1,
                        "end_line": chunk_end,
                        "file_sha256": file["sha256"],
                        "section_sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "content": content,
                    })
                    if len(sections) > _MAX_SECTIONS:
                        raise ValueError("snapshot corpus exceeds the section limit")
    return sections, manifest_sha256


def _preview(content: str, terms: set[str]) -> str:
    collapsed = " ".join(content.split())
    folded = collapsed.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    start = max(0, (min(positions) if positions else 0) - 80)
    value = collapsed[start:start + _MAX_PREVIEW_CHARS]
    return ("…" if start else "") + value + ("…" if start + len(value) < len(collapsed) else "")


def search_snapshot_sections(
    references_dir: str | Path, query: str, *, limit: int = 10,
    reference_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Search licensed offline snapshot sections with deterministic bounded results."""
    query = _nonempty(query, "query", maximum=1000)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 through 20")
    terms = _tokens(query)
    if not terms:
        return [], _snapshot_manifest(references_dir)[1]
    sections, manifest_sha256 = _snapshot_sections(references_dir, reference_id=reference_id)
    matches = []
    for section in sections:
        heading = _tokens(section["heading"])
        ancestry = _tokens(" ".join(section["heading_path"]))
        path = _tokens(section["path"])
        body = _tokens(section["content"])
        score = sum(
            (12 if term in heading else 0)
            + (8 if term in ancestry else 0)
            + (2 if term in body else 0)
            + (1 if term in path else 0)
            for term in terms
        )
        if score:
            public = {key: value for key, value in section.items() if key != "content"}
            matches.append({**public, "score": score, "preview": _preview(section["content"], terms)})
    matches.sort(key=lambda item: (-item["score"], item["snapshot"], item["path"], item["start_line"]))
    return matches[:limit], manifest_sha256


def load_snapshot_section(
    references_dir: str | Path, section_id: str,
) -> tuple[dict[str, Any], str]:
    """Load one exact bounded snapshot section by its stable identifier."""
    section_id = _nonempty(section_id, "section_id", maximum=80)
    if not re.fullmatch(r"sec-v1-[0-9a-f]{48}", section_id):
        raise ValueError("invalid snapshot section id")
    sections, manifest_sha256 = _snapshot_sections(references_dir)
    section = next((item for item in sections if item["section_id"] == section_id), None)
    if section is None:
        raise ValueError(f"unknown snapshot section id: {section_id!r}")
    content = section["content"]
    raw = content.encode("utf-8")
    if len(raw) > _MAX_SECTION_BYTES:
        raise ValueError("snapshot section exceeds the load limit")
    return {
        **section,
        "bytes": len(raw),
        "lines": len(content.splitlines()),
    }, manifest_sha256


def _tokens(value: str) -> set[str]:
    return set(_TOKEN.findall(value.casefold()))


def match_reference_registry(
    references_dir: str | Path, query: str, *, limit: int = 5,
) -> tuple[dict[str, Any], ...]:
    """Return deterministic positive-score reference matches without content."""
    query = _nonempty(query, "query", maximum=4000)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 through 20")
    terms = _tokens(query)
    if not terms:
        return ()
    registry = load_reference_registry(references_dir)
    matches: list[dict[str, Any]] = []
    for entry in registry["entries"]:
        weighted = (
            (12, _tokens(entry["id"])),
            (10, _tokens(entry["title"])),
            (6, _tokens(" ".join(entry["topics"]))),
            (5, _tokens(" ".join(entry["keywords"]))),
            (2, _tokens(entry["summary"])),
        )
        score = sum(weight for weight, values in weighted for term in terms if term in values)
        if score:
            matches.append({**entry, "score": score})
    matches.sort(key=lambda item: (-item["score"], item["id"]))
    return tuple(matches[:limit])


def _metadata(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "content"}


def _render(result: dict[str, Any]) -> str:
    command = result["command"]
    lines = [f"# Engineering References — {command}", ""]
    if command in {"load", "load-section"}:
        if command == "load-section":
            item = result["section"]
            lines.extend([
                f"**{item['heading']}** (`{item['section_id']}`)", "",
                f"Source: `{item['path']}:{item['start_line']}-{item['end_line']}`  ",
                f"Revision: `{item['revision']}`  ",
                f"SHA-256: `{item['section_sha256']}`", "", item["content"],
            ])
            return "\n".join(lines).strip()
        item = result["reference"]
        lines.extend([
            f"**{item['title']}** (`{item['id']}`)", "",
            f"Path: `{item['path']}`  ", f"SHA-256: `{item['sha256']}`", "", item["content"],
        ])
    elif command == "search":
        for item in result["sections"]:
            lines.append(
                f"- **{item['heading']}** (`{item['section_id']}`) — score {item['score']} — "
                f"`{item['path']}:{item['start_line']}-{item['end_line']}`: {item['preview']}"
            )
    else:
        for item in result["references"]:
            score = f" — score {item['score']}" if "score" in item else ""
            lines.append(f"- **{item['title']}** (`{item['id']}`){score}: {item['summary']}")
    return "\n".join(lines).strip()


def reference_action(
    references_dir: str | Path,
    *args: Any,
    limit: int = 5,
    include_content: bool = True,
    reference_id: str | None = None,
    output_format: str = "markdown",
) -> dict[str, Any] | str:
    """List, match, or explicitly load one validated engineering reference."""
    if output_format not in {"dict", "json", "markdown"}:
        raise ValueError("output_format must be dict, json, or markdown")
    command = str(args[0]).strip().lower() if args else "list"
    registry = load_reference_registry(references_dir)
    if command == "list":
        if len(args) > 1:
            raise ValueError("references list accepts no value")
        result = {"command": "list", "library_version": registry["library_version"],
                  "research_date": registry["research_date"],
                  "references": [_metadata(item) for item in registry["entries"]]}
    elif command == "match":
        if len(args) != 2:
            raise ValueError("references match requires exactly one query")
        result = {"command": "match", "query": str(args[1]),
                  "references": list(match_reference_registry(references_dir, str(args[1]), limit=limit))}
    elif command == "load":
        if len(args) != 2:
            raise ValueError("references load requires exactly one reference id")
        if include_content is not True:
            raise ValueError("references load requires include_content=True; use list or match for metadata")
        reference_id = str(args[1])
        entry = next((item for item in registry["entries"] if item["id"] == reference_id), None)
        if entry is None:
            raise ValueError(f"unknown reference id: {reference_id!r}")
        path = _contained_file(Path(references_dir).resolve(), entry["path"])
        raw = _read_regular_file(Path(references_dir).resolve(), path, limit=_MAX_REFERENCE_BYTES)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("reference content must be UTF-8") from exc
        result = {"command": "load", "reference": {
            **entry, "content": content, "bytes": len(raw),
            "lines": len(content.splitlines()), "sha256": hashlib.sha256(raw).hexdigest(),
        }}
    elif command == "search":
        if len(args) != 2:
            raise ValueError("references search requires exactly one query")
        matches, manifest_sha256 = search_snapshot_sections(
            references_dir, str(args[1]), limit=limit, reference_id=reference_id,
        )
        result = {
            "command": "search", "query": str(args[1]), "reference_id": reference_id,
            "manifest_sha256": manifest_sha256, "sections": matches,
        }
    elif command == "load-section":
        if len(args) != 2:
            raise ValueError("references load-section requires exactly one section id")
        section, manifest_sha256 = load_snapshot_section(references_dir, str(args[1]))
        result = {
            "command": "load-section", "manifest_sha256": manifest_sha256,
            "section": section,
        }
    else:
        raise ValueError("references command must be list, match, load, search, or load-section")
    if output_format == "dict":
        return result
    if output_format == "json":
        return json.dumps(result, indent=2, sort_keys=True)
    return _render(result)


def _profile_value(task_profile: Any, name: str, default: Any) -> Any:
    if isinstance(task_profile, dict):
        return task_profile.get(name, default)
    return getattr(task_profile, name, default)


def _canonical_signals(repository_signals: Any) -> frozenset[str]:
    if repository_signals is None:
        return frozenset()
    if isinstance(repository_signals, dict):
        values: list[str] = []
        for key, value in repository_signals.items():
            if value is True:
                values.append(str(key))
            elif isinstance(value, str):
                values.extend((str(key), value))
            elif isinstance(value, (list, tuple, set, frozenset)):
                values.extend(str(item) for item in value)
        repository_signals = values
    if isinstance(repository_signals, str):
        repository_signals = (repository_signals,)
    return frozenset(str(value).strip().casefold().replace("_", "-") for value in repository_signals)


def _changed_path_flags(changed_files: Any) -> tuple[bool, bool, bool, bool]:
    source = python = data = databricks = False
    source_suffixes = {
        ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
        ".ipynb", ".jsx", ".php", ".py", ".pyi", ".rb", ".rs", ".scala", ".sh",
        ".sql", ".swift", ".ts", ".tsx",
    }
    config_names = {
        "cargo.toml", "composer.json", "databricks.yml", "go.mod", "package.json",
        "pyproject.toml", "requirements.txt", "setup.cfg", "setup.py", "tox.ini",
    }
    for value in changed_files or ():
        path = Path(str(value).replace("\\", "/"))
        folded = path.as_posix().casefold()
        suffix = path.suffix.casefold()
        parts = set(path.parts)
        is_test = "tests" in parts or path.name.casefold().startswith(("test_", "tests."))
        is_config = path.name.casefold() in config_names or any(
            part in {".github", ".circleci"} for part in parts
        )
        source = source or suffix in source_suffixes or is_test or is_config
        python = python or suffix in {".py", ".pyi"}
        data = data or suffix == ".sql" or any(
            marker in folded
            for marker in (
                "/etl/", "/pipelines/", "/data/", "databricks", "unity_catalog",
                "unity-catalog",
            )
        )
        databricks = (
            databricks
            or "databricks" in folded
            or "unity_catalog" in folded
            or "unity-catalog" in folded
            or (suffix == ".ipynb" and "spark" in folded)
        )
    return source, python, data, databricks


def resolve_reference_guidance(
    references_dir: str | Path,
    task_profile: Any,
    changed_files: Any = (),
    repository_signals: Any = (),
) -> list[dict[str, Any]]:
    """Resolve deterministic advisory development guidance from canonical inputs."""
    registry = load_reference_registry(references_dir)
    entries = {entry["id"]: entry for entry in registry["entries"]}
    required = ("development.house", "development.python", "development.data-platform")
    if any(reference_id not in entries for reference_id in required):
        raise ValueError("deterministic development references are missing from the registry")

    execution_mode = str(_profile_value(task_profile, "execution_mode", "")).casefold()
    domains = {str(value).casefold() for value in _profile_value(task_profile, "domains", ())}
    traits = {str(value).casefold() for value in _profile_value(task_profile, "traits", ())}
    signals = _canonical_signals(repository_signals)
    changed_files = tuple(changed_files or ())
    path_source, path_python, path_data, path_databricks = _changed_path_flags(changed_files)
    canonical_source_change = execution_mode == "source_change" or "source-change" in traits
    source_change = path_source or canonical_source_change
    python = source_change and (
        path_python or "python" in domains or "python" in traits or "python" in signals
    )
    data_task_signal = (
        "data" in domains
        or bool(traits & {"data", "data-change", "data-write"})
        or bool(
            signals
            & {"data", "data-platform", "spark", "sql", "databricks", "unity-catalog"}
        )
    )
    data = (
        execution_mode == "data_change"
        or path_data
        or path_databricks
        or (source_change and data_task_signal)
    )
    databricks = data and (
        path_databricks or bool(signals & {"databricks", "spark-notebook", "unity-catalog"})
    )
    flags = {
        "source_change": source_change,
        "python": python,
        "data_platform": data,
        "databricks": databricks,
    }
    selected: list[tuple[str, str]] = []
    if source_change:
        selected.append(("development.house", "canonical source-change applicability"))
    if python:
        reason = "changed Python path" if path_python else "canonical Python repository or task signal"
        selected.append(("development.python", reason))
    if data:
        reason = "explicit Databricks, Spark notebook, or Unity Catalog signal" if databricks else "canonical data/platform applicability"
        selected.append(("development.data-platform", reason))

    return [
        {
            "id": reference_id,
            "title": entries[reference_id]["title"],
            "summary": entries[reference_id]["summary"],
            "path": f"references/{entries[reference_id]['path']}",
            "version": entries[reference_id]["profile_version"],
            "reason": reason,
            "applicability": dict(flags),
            "load": f'anchor("references", "load", "{reference_id}")',
            "search": (
                f'anchor("references", "search", "<specific query>", '
                f'reference_id="{reference_id}")'
            ),
        }
        for reference_id, reason in selected
    ]


def task_reference_guidance(
    references_dir: str | Path,
    query: str,
    *,
    task_profile: Any = None,
    changed_files: Any = (),
    repository_signals: Any = (),
    overlay_selection: Any = None,
) -> list[dict[str, Any]]:
    """Project deterministic profiles followed by backward-compatible technology matches."""
    guidance = resolve_reference_guidance(
        references_dir, task_profile, changed_files, repository_signals,
    ) if task_profile is not None else []
    if overlay_selection is not None and getattr(overlay_selection, "selected", ()):
        registry = load_reference_registry(references_dir)
        entries = {entry["id"]: entry for entry in registry["entries"]}
        reference_id = "assurance.standards-overlays"
        entry = entries.get(reference_id)
        if entry is None:
            raise ValueError("selected assurance overlay reference is missing from the registry")
        guidance.append({
            "id": reference_id,
            "title": entry["title"],
            "summary": entry["summary"],
            "path": f"references/{entry['path']}",
            "version": entry["versions"][0]["baseline"],
            "reason": "structured assurance overlay selection",
            "selection": [
                {
                    "overlay_id": item.overlay_id,
                    "version": item.version,
                    "reason_codes": list(item.reason_codes),
                }
                for item in overlay_selection.selected
            ],
            "load": f'anchor("references", "load", "{reference_id}")',
            "search": (
                f'anchor("references", "search", "<specific query>", '
                f'reference_id="{reference_id}")'
            ),
        })
    if not query.strip():
        return guidance
    technology = [
        {
            "id": item["id"], "title": item["title"], "summary": item["summary"],
            "path": f"references/{item['path']}", "score": item["score"],
            "load": f'anchor("references", "load", "{item["id"]}")',
            "search": (
                f'anchor("references", "search", "<specific query>", '
                f'reference_id="{item["id"]}")'
            ),
        }
        for item in match_reference_registry(references_dir, query, limit=20)
        if not item["id"].startswith("development.")
        and item["id"] != "assurance.standards-overlays"
    ][:5]
    selected_ids = {item["id"] for item in guidance}
    guidance.extend(item for item in technology if item["id"] not in selected_ids)
    return guidance
