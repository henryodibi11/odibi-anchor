"""Native assistant guidance discovery, distribution, and intent routing."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from odibi_anchor.planning._task_policy import TaskPolicyContext

NATIVE_SKILLS = (
    "auditing-memory-governance",
    "authoring-governed-memories",
    "building-memory-packs",
    "code-comprehension",
    "cross-functional-pr",
    "data-onboarding",
    "data-operations",
    "data-reconciliation",
    "debugging",
    "dependency-management",
    "documentation",
    "incident-response",
    "performance-investigation",
    "schema-design",
    "work-item-management",
    "writing-specs",
    "writing-tests",
)


@dataclass(frozen=True)
class GuidanceTarget:
    skill: str


@dataclass(frozen=True)
class GuidanceResourceEntry:
    relative_path: str
    byte_count: int
    sha256: str


def _unknown(name: str) -> ValueError:
    return ValueError(f"Unknown skill {name!r}; available skills: {', '.join(NATIVE_SKILLS)}")


def resolve_and_load_guidance(
    skills_dir: str | Path, requested_name: str,
) -> tuple[GuidanceTarget, str, tuple[Path, ...]]:
    """Read exactly one direct native skill, with no aliases or reference loading."""
    if requested_name not in NATIVE_SKILLS:
        raise _unknown(requested_name)
    path = Path(skills_dir) / requested_name / "SKILL.md"
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Guidance {requested_name!r} could not be read: {exc}") from exc
    return GuidanceTarget(requested_name), content, (path,)


def guidance_registry_metadata(skills_dir: str | Path) -> tuple[dict[str, Any], ...]:
    """Describe the native discovery surface and validate every entry is readable."""
    root = Path(skills_dir)
    entries = []
    for name in NATIVE_SKILLS:
        path = root / name / "SKILL.md"
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Native skill {name!r} does not exist")
        entries.append({
            "name": name,
            "path": f"skills/{name}/SKILL.md",
            "guidance_target": {"skill": name},
        })
    return tuple(entries)


def _read_open_file(file_descriptor: int, relative_path: str) -> bytes:
    try:
        before = os.fstat(file_descriptor)
        chunks = []
        while chunk := os.read(file_descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(file_descriptor)
    except OSError as exc:
        raise ValueError(f"guidance resource could not be read: {relative_path}") from exc
    if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(after.st_mode)
            or _stat_identity(before) != _stat_identity(after)):
        raise ValueError(f"guidance resource changed while reading: {relative_path}")
    content = b"".join(chunks)
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"guidance resource is not UTF-8: {relative_path}") from exc
    return content


def _read_regular_file_at(
    directory_descriptor: int,
    name: str,
    relative_path: str,
    enumerated: os.stat_result,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if _stat_identity(opened) != _stat_identity(enumerated):
                raise ValueError(f"guidance resource changed while reading: {relative_path}")
            return _read_open_file(file_descriptor, relative_path)
        finally:
            os.close(file_descriptor)
    except OSError as exc:
        raise ValueError(f"guidance resource could not be read: {relative_path}") from exc


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _stable_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns


def _validate_generated_launcher_cache(
    directory_descriptor: int,
    enumerated: os.stat_result,
) -> None:
    """Ignore only pip's bytecode cache for the packaged assistant launcher."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        cache_descriptor = os.open("__pycache__", flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise ValueError("generated launcher cache could not be read") from exc
    try:
        before = os.fstat(cache_descriptor)
        names = os.listdir(cache_descriptor)
        if _stat_identity(before) != _stat_identity(enumerated):
            raise ValueError("generated launcher cache changed while reading")
        for name in names:
            if not name.startswith("agent_bootstrap.") or not name.endswith(".pyc"):
                raise ValueError("unexpected file in generated launcher cache")
            cached = os.stat(name, dir_fd=cache_descriptor, follow_symlinks=False)
            if not stat.S_ISREG(cached.st_mode):
                raise ValueError("non-regular file in generated launcher cache")
        after = os.fstat(cache_descriptor)
        if _stat_identity(before) != _stat_identity(after) or set(names) != set(os.listdir(cache_descriptor)):
            raise ValueError("generated launcher cache changed while reading")
    except OSError as exc:
        raise ValueError("generated launcher cache changed while reading") from exc
    finally:
        os.close(cache_descriptor)


def _read_regular_file_path(
    path: Path, relative_path: str, enumerated: os.stat_result,
) -> bytes:
    """Read a regular file safely where directory descriptors are unavailable."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path, flags)
        try:
            opened = os.fstat(file_descriptor)
            # Windows updates st_ctime_ns when a file handle is opened. The
            # stable object identity still prevents an enumeration/open swap;
            # _read_open_file verifies ctime across the actual read.
            if _stable_file_identity(opened) != _stable_file_identity(enumerated):
                raise ValueError(f"guidance resource changed while reading: {relative_path}")
            return _read_open_file(file_descriptor, relative_path)
        finally:
            os.close(file_descriptor)
    except OSError as exc:
        raise ValueError(f"guidance resource could not be read: {relative_path}") from exc


def _validate_generated_launcher_cache_path(
    directory: Path, enumerated: os.stat_result,
) -> None:
    """Validate the one permitted generated cache without opening a directory."""
    try:
        before = directory.stat(follow_symlinks=False)
        names = os.listdir(directory)
        if _stat_identity(before) != _stat_identity(enumerated):
            raise ValueError("generated launcher cache changed while reading")
        for name in names:
            if not name.startswith("agent_bootstrap.") or not name.endswith(".pyc"):
                raise ValueError("unexpected file in generated launcher cache")
            cached = (directory / name).stat(follow_symlinks=False)
            if not stat.S_ISREG(cached.st_mode):
                raise ValueError("non-regular file in generated launcher cache")
        after = directory.stat(follow_symlinks=False)
        if _stat_identity(before) != _stat_identity(after) or set(names) != set(os.listdir(directory)):
            raise ValueError("generated launcher cache changed while reading")
    except OSError as exc:
        raise ValueError("generated launcher cache changed while reading") from exc


def _path_guidance_resources(
    root: Path,
    selected: list[tuple[bytes, str, bytes]],
    directories: set[str],
) -> bytes:
    """Scan guidance by path on Windows, which cannot open directory handles."""
    assistant = root / ".assistant"

    def visit(directory: Path, relative_directory: str) -> None:
        try:
            before_directory = directory.stat(follow_symlinks=False)
            names = os.listdir(directory)
            after_listing = directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"guidance directory changed while scanning: {directory.name}") from exc
        if (not stat.S_ISDIR(before_directory.st_mode)
                or _stat_identity(before_directory) != _stat_identity(after_listing)):
            raise ValueError(f"guidance directory changed while scanning: {directory.name}")
        for name in names:
            relative = f"{relative_directory}/{name}"
            if relative != unicodedata.normalize("NFC", relative) or any(
                part in {"", ".", ".."} for part in relative.split("/")
            ):
                raise ValueError(f"invalid guidance distribution path: {relative!r}")
            path = directory / name
            try:
                enumerated = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"guidance resource changed while reading: {relative}") from exc
            if stat.S_ISLNK(enumerated.st_mode):
                raise ValueError(f"symlink is not allowed in guidance distribution: {name}")
            if stat.S_ISDIR(enumerated.st_mode):
                if name == "__pycache__":
                    if relative_directory != ".assistant":
                        raise ValueError("cache directories are not allowed in guidance distribution")
                    launcher = (directory / "agent_bootstrap.py").stat(follow_symlinks=False)
                    if not stat.S_ISREG(launcher.st_mode):
                        raise ValueError("assistant launcher must be a regular file")
                    _validate_generated_launcher_cache_path(path, enumerated)
                    continue
                directories.add(relative)
                visit(path, relative)
            elif stat.S_ISREG(enumerated.st_mode):
                if name.endswith((".pyc", ".crc")):
                    raise ValueError(f"generated file is not allowed in guidance distribution: {relative}")
                selected.append((
                    relative.encode("utf-8"), relative,
                    _read_regular_file_path(path, relative, enumerated),
                ))
            else:
                raise ValueError(f"non-regular guidance resource: {relative}")
        try:
            after_directory = directory.stat(follow_symlinks=False)
            final_names = os.listdir(directory)
        except OSError as exc:
            raise ValueError(f"guidance directory changed while scanning: {directory.name}") from exc
        if (_stat_identity(before_directory) != _stat_identity(after_directory)
                or set(names) != set(final_names)):
            raise ValueError(f"guidance directory changed while scanning: {directory.name}")

    try:
        root_before = root.stat(follow_symlinks=False)
        assistant_enumerated = assistant.stat(follow_symlinks=False)
        instructions = root / ".assistant_instructions.md"
        instructions_enumerated = instructions.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(assistant_enumerated.st_mode)
                or not stat.S_ISREG(instructions_enumerated.st_mode)):
            raise ValueError("resource root must contain regular .assistant resources and instructions")
        visit(assistant, ".assistant")
        instruction_content = _read_regular_file_path(
            instructions, ".assistant_instructions.md", instructions_enumerated,
        )
        root_after = root.stat(follow_symlinks=False)
        current_assistant = assistant.stat(follow_symlinks=False)
        current_instructions = instructions.stat(follow_symlinks=False)
        if (_stat_identity(root_before) != _stat_identity(root_after)
                or _stat_identity(assistant_enumerated) != _stat_identity(current_assistant)
                or _stat_identity(instructions_enumerated) != _stat_identity(current_instructions)):
            raise ValueError("guidance resource root changed while scanning")
        return instruction_content
    except OSError as exc:
        raise ValueError(
            "resource root must contain regular .assistant resources and instructions"
        ) from exc


def guidance_distribution_manifest(resource_root: str | Path) -> Mapping[str, Any]:
    """Manifest the complete ``.assistant`` tree and sibling instructions file."""
    root = Path(resource_root)
    assistant = root / ".assistant"

    selected: list[tuple[bytes, str, bytes]] = []
    directories = {".assistant"}

    instruction_content = (
        _path_guidance_resources(root, selected, directories) if os.name == "nt" else None
    )

    def visit(directory: Path, directory_descriptor: int, relative_directory: str) -> None:
        try:
            before_directory = os.fstat(directory_descriptor)
            names = os.listdir(directory_descriptor)
        except OSError as exc:
            raise ValueError(f"guidance directory changed while scanning: {directory.name}") from exc
        if not stat.S_ISDIR(before_directory.st_mode):
            raise ValueError(f"guidance directory changed while scanning: {directory.name}")
        for name in names:
            relative = f"{relative_directory}/{name}"
            if relative != unicodedata.normalize("NFC", relative) or any(p in {"", ".", ".."} for p in relative.split("/")):
                raise ValueError(f"invalid guidance distribution path: {relative!r}")
            try:
                enumerated = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False,
                )
            except OSError as exc:
                raise ValueError(f"guidance resource changed while reading: {relative}") from exc
            if stat.S_ISLNK(enumerated.st_mode):
                raise ValueError(f"symlink is not allowed in guidance distribution: {name}")
            if stat.S_ISDIR(enumerated.st_mode):
                if name == "__pycache__":
                    if relative_directory != ".assistant":
                        raise ValueError("cache directories are not allowed in guidance distribution")
                    launcher = os.stat(
                        "agent_bootstrap.py",
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISREG(launcher.st_mode):
                        raise ValueError("assistant launcher must be a regular file")
                    _validate_generated_launcher_cache(directory_descriptor, enumerated)
                    continue
                directories.add(relative)
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    child_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
                except OSError as exc:
                    raise ValueError(f"guidance directory changed while scanning: {name}") from exc
                try:
                    if _stat_identity(os.fstat(child_descriptor)) != _stat_identity(enumerated):
                        raise ValueError(f"guidance directory changed while scanning: {name}")
                    visit(directory / name, child_descriptor, relative)
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(enumerated.st_mode):
                if name.endswith((".pyc", ".crc")):
                    raise ValueError(f"generated file is not allowed in guidance distribution: {relative}")
                content = _read_regular_file_at(
                    directory_descriptor, name, relative, enumerated,
                )
                selected.append((relative.encode("utf-8"), relative, content))
            else:
                raise ValueError(f"non-regular guidance resource: {relative}")
        try:
            after_directory = os.fstat(directory_descriptor)
            current_directory = directory.stat(follow_symlinks=False)
            final_names = os.listdir(directory_descriptor)
        except OSError as exc:
            raise ValueError(f"guidance directory changed while scanning: {directory.name}") from exc
        if (_stat_identity(before_directory) != _stat_identity(after_directory)
                or _stat_identity(after_directory) != _stat_identity(current_directory)
                or set(names) != set(final_names)):
            raise ValueError(f"guidance directory changed while scanning: {directory.name}")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt":
        try:
            root_descriptor = os.open(root, directory_flags)
            try:
                root_before = os.fstat(root_descriptor)
                if (_stat_identity(root_before)
                        != _stat_identity(root.stat(follow_symlinks=False))):
                    raise ValueError("guidance resource root changed while scanning")
                assistant_enumerated = os.stat(
                    ".assistant", dir_fd=root_descriptor, follow_symlinks=False,
                )
                instructions_enumerated = os.stat(
                    ".assistant_instructions.md", dir_fd=root_descriptor, follow_symlinks=False,
                )
                if (not stat.S_ISDIR(assistant_enumerated.st_mode)
                        or not stat.S_ISREG(instructions_enumerated.st_mode)):
                    raise ValueError(
                        "resource root must contain regular .assistant resources and instructions"
                    )
                assistant_descriptor = os.open(
                    ".assistant", directory_flags, dir_fd=root_descriptor,
                )
                try:
                    if (_stat_identity(os.fstat(assistant_descriptor))
                            != _stat_identity(assistant_enumerated)):
                        raise ValueError("guidance directory changed while scanning: .assistant")
                    visit(assistant, assistant_descriptor, ".assistant")
                finally:
                    os.close(assistant_descriptor)
                instruction_content = _read_regular_file_at(
                    root_descriptor,
                    ".assistant_instructions.md",
                    ".assistant_instructions.md",
                    instructions_enumerated,
                )
                root_after = os.fstat(root_descriptor)
                current_root = root.stat(follow_symlinks=False)
                current_assistant = os.stat(
                    ".assistant", dir_fd=root_descriptor, follow_symlinks=False,
                )
                current_instructions = os.stat(
                    ".assistant_instructions.md", dir_fd=root_descriptor, follow_symlinks=False,
                )
                if (_stat_identity(root_before) != _stat_identity(root_after)
                        or _stat_identity(root_after) != _stat_identity(current_root)
                        or _stat_identity(assistant_enumerated) != _stat_identity(current_assistant)
                        or _stat_identity(instructions_enumerated) != _stat_identity(current_instructions)):
                    raise ValueError("guidance resource root changed while scanning")
            finally:
                os.close(root_descriptor)
        except OSError as exc:
            raise ValueError("resource root must contain regular .assistant resources and instructions") from exc
    if any(not any(item[1].startswith(f"{directory}/") for item in selected) for directory in directories):
        raise ValueError("empty directories are not allowed in guidance distribution")

    selected.sort(key=lambda item: item[0])
    digest = hashlib.sha256()
    entries = []
    total = 0
    for path_bytes, relative, content in (*selected, (b".assistant_instructions.md", ".assistant_instructions.md", instruction_content)):
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        if relative.startswith(".assistant/"):
            total += len(content)
            entries.append(GuidanceResourceEntry(relative, len(content), hashlib.sha256(content).hexdigest()))
    return MappingProxyType({
        "file_count": len(entries),
        "directory_count": len(directories),
        "byte_count": total,
        "instruction_byte_count": len(instruction_content),
        "instruction_sha256": hashlib.sha256(instruction_content).hexdigest(),
        "sha256": digest.hexdigest(),
        "files": tuple(entries),
    })


def select_guidance(context: TaskPolicyContext) -> tuple[GuidanceTarget, ...]:
    """Select native guidance through the accepted-task resolver."""
    from odibi_anchor.planning._task_builders import required_skills_for_task
    from odibi_anchor.planning._task_policy import evaluate_task_policies

    disposition = evaluate_task_policies(context.profile, context).specification.disposition
    return tuple(
        GuidanceTarget(name) for name in required_skills_for_task(
            context, specification_disposition=disposition,
        )
    )
