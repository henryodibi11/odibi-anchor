"""In-process fakes for the Databricks SDK surfaces Odibi Anchor uses.

Only the methods and keyword arguments Anchor calls, or that the v0.3.24
hardening needs (``get_directory_metadata``, ``workspace.list``), are modeled, with
the SDK's observable semantics: Workspace paths omit the ``/Workspace`` prefix,
Files API paths address Unity Catalog Volumes, missing objects raise
``databricks.sdk.errors.NotFound`` subclasses, and uploads without ``overwrite``
refuse existing files. Every call is logged, can be delayed, can fail through an
injected fault, and reports remote mutations to registered listeners.
"""
from __future__ import annotations

import enum
import io
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from tests.fixtures.fake_databricks import crash


class DatabricksError(IOError):
    """Mirror of ``databricks.sdk.errors.DatabricksError`` (an ``IOError``)."""

    error_code = "UNKNOWN"
    status_code = 500

    def __init__(self, message: str = "", *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class NotFound(DatabricksError):
    error_code = "NOT_FOUND"
    status_code = 404


class ResourceDoesNotExist(NotFound):
    error_code = "RESOURCE_DOES_NOT_EXIST"


class AlreadyExists(DatabricksError):
    error_code = "ALREADY_EXISTS"
    status_code = 409


class ResourceAlreadyExists(AlreadyExists):
    error_code = "RESOURCE_ALREADY_EXISTS"


class ImportFormat(enum.Enum):
    AUTO = "AUTO"
    SOURCE = "SOURCE"
    RAW = "RAW"


class ObjectType(enum.Enum):
    DIRECTORY = "DIRECTORY"
    FILE = "FILE"
    NOTEBOOK = "NOTEBOOK"
    REPO = "REPO"


class Config:
    """``databricks.sdk.config.Config``: keeps the keyword settings it was given."""

    def __init__(self, **settings: Any) -> None:
        self.host = settings.pop("host", "https://fake.cloud.databricks.invalid")
        self.settings = settings


@dataclass(frozen=True)
class ObjectInfo:
    path: str
    object_type: ObjectType


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    name: str
    is_directory: bool
    file_size: int | None
    last_modified: int


@dataclass
class Fault:
    """One injected failure for matching calls.

    ``when="before"`` fails without applying the call. ``when="after"`` applies a
    mutation and then raises, modeling a remote commit whose response was lost.
    ``times=None`` keeps the fault active for every matching call.
    """

    api: str
    method: str
    error: BaseException | Callable[[str], BaseException]
    match: str | Callable[[str], bool] | None = None
    times: int | None = 1
    when: str = "before"
    fired: int = 0

    def matches(self, api: str, method: str, path: str) -> bool:
        if (api, method) != (self.api, self.method):
            return False
        if self.times is not None and self.fired >= self.times:
            return False
        if self.match is None:
            return True
        if callable(self.match):
            return bool(self.match(path))
        return self.match in path

    def build(self, path: str) -> BaseException:
        self.fired += 1
        return self.error(path) if callable(self.error) else self.error


@dataclass
class CallControl:
    """Shared call log, latency, fault, and mutation-listener state."""

    calls: list[tuple[str, str, str]] = field(default_factory=list)
    latency: dict[tuple[str | None, str | None], float] = field(default_factory=dict)
    faults: list[Fault] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def delay_for(self, api: str, method: str) -> float:
        for key in ((api, method), (api, None), (None, None)):
            if key in self.latency:
                return self.latency[key]
        return 0.0

    def enter(self, api: str, method: str, path: str, *, mutation: bool) -> BaseException | None:
        """Log one call, apply latency, raise a ``before`` fault, report a mutation.

        Returns an ``after`` fault's error for the caller to raise once applied.
        """
        with self.lock:
            self.calls.append((api, method, path))
            fault = next(
                (item for item in self.faults if item.matches(api, method, path)), None
            )
            pending = fault.build(path) if fault is not None else None
        delay = self.delay_for(api, method)
        if delay:
            time.sleep(delay)
        if pending is not None and fault is not None and (fault.when == "before" or not mutation):
            raise pending
        if mutation:
            crash.record_remote_mutation(f"{api}.{method}", path)
        return pending


def _raise_after(pending: BaseException | None) -> None:
    if pending is not None:
        raise pending


def _normalized(path: str, label: str) -> str:
    text = str(path)
    pure = PurePosixPath(text)
    if not pure.is_absolute() or (pure.as_posix() != text.rstrip("/") and text != "/"):
        raise ValueError(f"{label} must be a normalized absolute path: {text!r}")
    if any(part in {".", ".."} for part in pure.parts):
        raise ValueError(f"{label} must not contain relative segments: {text!r}")
    return pure.as_posix()


class FakeWorkspaceAPI:
    """In-memory ``WorkspaceClient().workspace`` store keyed by API path."""

    def __init__(self, control: CallControl) -> None:
        self._control = control
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = {"/"}
        self.repos: set[str] = set()

    def add_directory(self, path: str, *, repo: bool = False) -> None:
        """Seed a directory (or Git Folder) without logging a call."""
        normalized = _normalized(path, "workspace path")
        for parent in (*reversed(PurePosixPath(normalized).parents), PurePosixPath(normalized)):
            self.directories.add(parent.as_posix())
        if repo:
            self.repos.add(normalized)

    def get_status(self, path: str) -> ObjectInfo:
        self._control.enter("workspace", "get_status", path, mutation=False)
        with self._control.lock:
            if path in self.files:
                return ObjectInfo(path, ObjectType.FILE)
            if path in self.directories:
                kind = ObjectType.REPO if path in self.repos else ObjectType.DIRECTORY
                return ObjectInfo(path, kind)
        raise ResourceDoesNotExist(f"Path ({path}) doesn't exist.")

    def download(self, path: str, *, format: Any = None) -> io.BytesIO:
        self._control.enter("workspace", "download", path, mutation=False)
        with self._control.lock:
            if path not in self.files:
                raise ResourceDoesNotExist(f"Path ({path}) doesn't exist.")
            return io.BytesIO(self.files[path])

    def upload(
        self, path: str, content: bytes | io.BytesIO, *, format: Any = None,
        language: Any = None, overwrite: bool = False,
    ) -> None:
        outcome = self._control.enter("workspace", "upload", path, mutation=True)
        data = content.read() if isinstance(content, io.BytesIO) else bytes(content)
        with self._control.lock:
            if PurePosixPath(path).parent.as_posix() not in self.directories:
                raise ResourceDoesNotExist(f"The parent folder of ({path}) does not exist.")
            if path in self.directories:
                raise ResourceAlreadyExists(f"Path ({path}) is a directory.")
            if path in self.files and not overwrite:
                raise ResourceAlreadyExists(f"Path ({path}) already exists.")
            self.files[path] = bytes(data)
        _raise_after(outcome)

    def mkdirs(self, path: str) -> None:
        outcome = self._control.enter("workspace", "mkdirs", path, mutation=True)
        normalized = _normalized(path, "workspace path")
        with self._control.lock:
            for parent in (*reversed(PurePosixPath(normalized).parents), PurePosixPath(normalized)):
                if parent.as_posix() in self.files:
                    raise ResourceAlreadyExists(f"Path ({parent}) is a file.")
                self.directories.add(parent.as_posix())
        _raise_after(outcome)

    def delete(self, path: str, *, recursive: bool = False) -> None:
        outcome = self._control.enter("workspace", "delete", path, mutation=True)
        with self._control.lock:
            if path in self.files:
                del self.files[path]
            elif path in self.directories and path != "/":
                children = [
                    name for name in (*self.files, *self.directories)
                    if name.startswith(path.rstrip("/") + "/")
                ]
                if children and not recursive:
                    raise DatabricksError(
                        f"Folder ({path}) is not empty.", error_code="DIRECTORY_NOT_EMPTY"
                    )
                for name in children:
                    self.files.pop(name, None)
                    self.directories.discard(name)
                self.directories.discard(path)
                self.repos.discard(path)
            else:
                raise ResourceDoesNotExist(f"Path ({path}) doesn't exist.")
        _raise_after(outcome)

    def list(self, path: str, *, recursive: bool = False, **_kwargs: Any) -> Iterator[ObjectInfo]:
        self._control.enter("workspace", "list", path, mutation=False)
        with self._control.lock:
            if path not in self.directories:
                raise ResourceDoesNotExist(f"Path ({path}) doesn't exist.")
            prefix = path.rstrip("/") + "/"
            entries = [
                ObjectInfo(name, ObjectType.FILE) for name in self.files if name.startswith(prefix)
            ] + [
                ObjectInfo(
                    name, ObjectType.REPO if name in self.repos else ObjectType.DIRECTORY
                )
                for name in self.directories
                if name.startswith(prefix)
            ]
        if not recursive:
            entries = [item for item in entries if "/" not in item.path[len(prefix):]]
        return iter(sorted(entries, key=lambda item: item.path))


class FakeFilesAPI:
    """``WorkspaceClient().files`` backed by a temporary directory per Volume.

    A path is valid only inside a provisioned ``/Volumes/<catalog>/<schema>/<volume>``;
    operations under an unprovisioned Volume raise ``NotFound``, as on Databricks.
    """

    def __init__(self, control: CallControl, backing_root: Path) -> None:
        self._control = control
        self._backing_root = backing_root
        self.volumes: set[str] = set()

    def provision_volume(self, volume: str) -> Path:
        normalized = _normalized(volume, "volume")
        parts = PurePosixPath(normalized).parts
        if len(parts) != 5 or parts[1] != "Volumes":
            raise ValueError("volume must be /Volumes/<catalog>/<schema>/<volume>")
        self.volumes.add(normalized)
        backing = self._backing_root.joinpath(*parts[2:])
        with crash.suppressed():
            backing.mkdir(parents=True, exist_ok=True)
        return backing

    def backing_path(self, path: str) -> Path:
        """Return the local backing path for one Volume path (for test inspection)."""
        normalized = _normalized(path, "Volume path")
        volume = next(
            (item for item in self.volumes
             if normalized == item or normalized.startswith(item + "/")),
            None,
        )
        if volume is None:
            raise NotFound(f"Volume for path {path} does not exist.")
        return self._backing_root.joinpath(*PurePosixPath(normalized).parts[2:])

    def list_directory_contents(self, directory_path: str, **_kwargs: Any) -> Iterator[DirectoryEntry]:
        self._control.enter("files", "list_directory_contents", directory_path, mutation=False)
        directory = self.backing_path(directory_path)
        if not directory.is_dir():
            raise NotFound(f"The directory {directory_path} does not exist.")
        entries = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            stat = child.stat()
            entries.append(DirectoryEntry(
                path=f"{directory_path.rstrip('/')}/{child.name}",
                name=child.name,
                is_directory=child.is_dir(),
                file_size=None if child.is_dir() else stat.st_size,
                last_modified=int(stat.st_mtime * 1000),
            ))
        return iter(entries)

    def get_directory_metadata(self, directory_path: str) -> None:
        self._control.enter("files", "get_directory_metadata", directory_path, mutation=False)
        if not self.backing_path(directory_path).is_dir():
            raise NotFound(f"The directory {directory_path} does not exist.")

    def create_directory(self, directory_path: str) -> None:
        outcome = self._control.enter("files", "create_directory", directory_path, mutation=True)
        directory = self.backing_path(directory_path)
        with crash.suppressed():
            if directory.is_file():
                raise AlreadyExists(f"{directory_path} is a file.")
            directory.mkdir(parents=True, exist_ok=True)
        _raise_after(outcome)

    def download_to(
        self, file_path: str, destination: str, *, overwrite: bool = True,
        use_parallel: bool = True, parallelism: int | None = None,
    ) -> None:
        self._control.enter("files", "download_to", file_path, mutation=False)
        source = self.backing_path(file_path)
        if not source.is_file():
            raise NotFound(f"The file {file_path} does not exist.")
        target = Path(destination)
        if target.exists() and not overwrite:
            raise FileExistsError(f"destination already exists: {destination}")
        with crash.suppressed():
            shutil.copyfile(source, target)

    def upload_from(
        self, file_path: str, source_path: str, *, content_type: str | None = None,
        overwrite: bool | None = None, part_size: int | None = None,
        use_parallel: bool = True, parallelism: int | None = None,
    ) -> None:
        outcome = self._control.enter("files", "upload_from", file_path, mutation=True)
        self._write(file_path, Path(source_path).read_bytes(), overwrite=bool(overwrite))
        _raise_after(outcome)

    def delete(self, file_path: str) -> None:
        outcome = self._control.enter("files", "delete", file_path, mutation=True)
        target = self.backing_path(file_path)
        if not target.is_file():
            raise NotFound(f"The file {file_path} does not exist.")
        with crash.suppressed():
            target.unlink()
        _raise_after(outcome)

    def _write(self, file_path: str, data: bytes, *, overwrite: bool) -> None:
        target = self.backing_path(file_path)
        with self._control.lock, crash.suppressed():
            if target.is_dir():
                raise AlreadyExists(f"{file_path} is a directory.")
            if target.exists() and not overwrite:
                raise AlreadyExists(f"The file {file_path} already exists.")
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(f".{target.name}.uploading")
            staged.write_bytes(data)
            staged.replace(target)
