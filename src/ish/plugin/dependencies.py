"""Stage dependency installs and replace owned files without reloading live modules."""

from __future__ import annotations

import base64
import contextlib
import csv
import fcntl
import hashlib
import importlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path

from packaging.utils import canonicalize_name

from ish.lang import Message, i18n

from . import PACKAGE_ENTRY, PYTHON_SOURCE_SUFFIX

INSTALL_LOCK_SUFFIX = ".install.lock"
_LOCK_MODE = 0o600
_STAGE_PREFIX = ".install-"
_STAGE_DIR = "stage"
_BACKUP_DIR = "backup"
_CONSTRAINTS_FILE = "constraints.txt"
_CONSTRAINTS_ENCODING = "utf-8"
_PIP_INSTALL = (
    "-I",
    "-m",
    "pip",
    "install",
    "--disable-pip-version-check",
    "--no-compile",
)
_PIP_TARGET = "--target"
_PIP_CONSTRAINT = "--constraint"
_DIST_NAME = "Name"
_BYTECODE = ".pyc"
_BYTECODE_DIR = "__pycache__"
_NATIVE_EXTENSIONS = (".so", ".pyd")
_COMPARE_BYTES = 128 * 1024
_MISSING_MODULE = object()
_RECORD = "RECORD"
_DIST_INFO_SUFFIX = ".dist-info"
_REQUIRED_RECORD_FILES = frozenset(("METADATA", "WHEEL", _RECORD))


@contextlib.contextmanager
def _installation_lock(target: Path):
    """Protect promotion and rollback without blocking another session's startup.

    The persistent sibling inode must not be unlinked: waiting/open sessions
    must keep referring to the same lock even after a transaction finishes.
    """
    path = target.parent / ("." + target.name + INSTALL_LOCK_SUFFIX)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, _LOCK_MODE)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(i18n.get(Message.PM_INSTALL_BUSY, path=target)) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def has_import(name: str) -> bool:
    """Locate an import without executing parent packages during install validation.

    Loaded packages can supply custom search paths. Unloaded packages are checked
    using their finders' paths; their initialization is left to plugin execution.
    """
    path = None
    parts = name.split(".")
    for index in range(len(parts)):
        fullname = ".".join(parts[: index + 1])
        module = sys.modules.get(fullname, _MISSING_MODULE)
        if module is not _MISSING_MODULE:
            if module is None:
                # The interpreter explicitly blocks this import, even if a finder
                # could locate it on disk.
                return False
            if index == len(parts) - 1:
                return True
            path = module.__dict__.get("__path__")
        else:
            spec = None
            for finder in sys.meta_path:
                spec = finder.find_spec(fullname, path)
                if spec is not None:
                    break
            if spec is None:
                return False
            path = spec.submodule_search_locations
        if index < len(parts) - 1 and path is None:
            return False
    return True


def distribution_files(distribution, root: Path) -> set[Path]:
    """Read owned paths from RECORD, excluding paths outside the installation root."""
    paths = set()
    for entry in distribution.files or ():
        path = Path(distribution.locate_file(entry)).resolve()
        if path.is_relative_to(root):
            paths.add(path.relative_to(root))
    return paths


def has_distribution_files(distribution, *, required: bool = False) -> bool:
    """Validate recorded library files without importing code or reading outside it.

    Managed installs require a RECORD. External editable/legacy distributions
    without one retain metadata/import validation. Console scripts and other
    entries outside the distribution's library root are outside this check.
    Missing bytecode is harmless because Python may regenerate it.
    """
    try:
        record = distribution.read_text(_RECORD)
        if record is None:
            return not required
        base = Path(os.path.abspath(distribution.locate_file("")))
        root = base.resolve()
        # Distribution.files omits missing paths in Python 3.12. Read the raw
        # inventory so an absent recorded file remains a failed requirement.
        rows = list(csv.reader(StringIO(record)))
        if not rows:
            return False
        if any(len(row) != 3 for row in rows):
            return False
        if required:
            recorded = {Path(row[0]) for row in rows}
            metadata_directories = {
                path.parent
                for path in recorded
                if path.name == _RECORD and path.parent.suffix == _DIST_INFO_SUFFIX
            }
            if not any(
                {directory / name for name in _REQUIRED_RECORD_FILES} <= recorded
                for directory in metadata_directories
            ):
                return False
        for row in rows:
            relative, checksum, size = row
            if Path(relative).suffix == _BYTECODE:
                continue
            lexical = Path(os.path.abspath(distribution.locate_file(relative)))
            if not lexical.is_relative_to(base):
                continue
            path = lexical.resolve()
            if not path.is_relative_to(root):
                return False
            if not path.is_file():
                return False
            if size and path.stat().st_size != int(size):
                return False
            if checksum:
                algorithm, separator, expected = checksum.partition("=")
                if not separator or not expected:
                    return False
                with path.open("rb") as source:
                    digest = hashlib.file_digest(source, algorithm).digest()
                encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
                if encoded != expected:
                    return False
        return True
    except (OSError, ValueError, TypeError, csv.Error):
        return False


def _distribution_name(distribution) -> str:
    """Read a distribution's canonical metadata identity for install ownership checks."""
    return canonicalize_name(distribution.metadata[_DIST_NAME] or "")


def has_duplicate_metadata(name: str, target: Path) -> bool:
    """Detect mixed installations left by pip --target without file replacement."""
    wanted = canonicalize_name(name)
    matches = (
        dist
        for dist in importlib.metadata.distributions(path=[str(target)])
        if _distribution_name(dist) == wanted
    )
    return next(matches, None) is not None and next(matches, None) is not None


def loaded_distributions():
    """Find distributions owning modules already imported by this interpreter."""
    origins = {
        Path(module.__dict__["__file__"]).resolve()
        for module in tuple(sys.modules.values())
        if module is not None and module.__dict__.get("__file__")
    }
    for distribution in importlib.metadata.distributions():
        if any(
            Path(distribution.locate_file(entry)).resolve() in origins
            for entry in distribution.files or ()
        ):
            yield distribution


def _same_contents(existing: Path, staged: Path) -> bool:
    """Compare regular files without caching mutable contents or loading whole wheels."""
    if not existing.is_file():
        return False
    with existing.open("rb") as old, staged.open("rb") as new:
        while True:
            block = old.read(_COMPARE_BYTES)
            if block != new.read(_COMPARE_BYTES):
                return False
            if not block:
                return True


def check_loaded_files(incoming: dict[Path, Path], removed: set[Path], target: Path):
    """Reject changes to imported packages, including unversioned module shadows."""
    removed_files = {target / path for path in removed}
    for name, module in tuple(sys.modules.items()):
        if module is None:
            continue
        origin = module.__dict__.get("__file__")
        locations = module.__dict__.get("__path__")
        relative = Path(*name.split("."))
        if not origin:
            # Adding a regular package over an active namespace changes imports.
            if locations is not None and relative / PACKAGE_ENTRY in incoming:
                raise RuntimeError(i18n.get(Message.PM_PACKAGE_BUSY, name=name))
            continue
        origin = Path(origin).resolve()
        if locations is not None:
            for path, source in incoming.items():
                if path.is_relative_to(relative):
                    current = origin.parent / path.relative_to(relative)
                    if not _same_contents(current, source):
                        raise RuntimeError(i18n.get(Message.PM_PACKAGE_BUSY, name=name))
            if any(path.is_relative_to(origin.parent) for path in removed_files):
                raise RuntimeError(i18n.get(Message.PM_PACKAGE_BUSY, name=name))
        else:
            for path, source in incoming.items():
                if path.parent == relative.parent and (
                    path.name == relative.name + PYTHON_SOURCE_SUFFIX
                    or path.name.startswith(relative.name + ".")
                    and path.suffix in _NATIVE_EXTENSIONS
                ):
                    if not _same_contents(origin, source):
                        raise RuntimeError(i18n.get(Message.PM_MODULE_BUSY, name=name))
            if origin in removed_files:
                raise RuntimeError(i18n.get(Message.PM_MODULE_BUSY, name=name))


def _remove_empty_directories(paths: set[Path], root: Path) -> None:
    """Remove empty installation directories without leaving the installation root."""
    parents = {
        parent
        for path in paths
        for parent in path.parents
        if parent != root and parent.is_relative_to(root)
    }
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        with contextlib.suppress(OSError):
            parent.rmdir()


@contextlib.contextmanager
def install_dependency(python: str, requirement: str, target: Path, constraints=()):
    """Install into a fresh stage, then roll back if promotion or validation fails.

    Existing imported distributions are pinned for resolution. Files are merged by
    ownership so independent distributions can share a namespace package. This
    transaction protects against ordinary failures, not process or power loss.
    """
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    protected = {
        name: dist.version
        for dist in loaded_distributions()
        if (name := _distribution_name(dist))
    }
    with tempfile.TemporaryDirectory(
        prefix=_STAGE_PREFIX, dir=target.parent
    ) as temporary:
        temporary = Path(temporary)
        stage = temporary / _STAGE_DIR
        constraints_path = temporary / _CONSTRAINTS_FILE
        constraints_path.write_text(
            "\n".join(
                [
                    *(f"{name}=={version}" for name, version in protected.items()),
                    *constraints,
                ]
            )
            + "\n",
            encoding=_CONSTRAINTS_ENCODING,
        )
        cmd = [
            python,
            *_PIP_INSTALL,
            _PIP_TARGET,
            str(stage),
            _PIP_CONSTRAINT,
            str(constraints_path),
            requirement,
        ]
        subprocess.run(cmd, check=True)
        new = {
            name: dist
            for dist in importlib.metadata.distributions(path=[str(stage)])
            if (name := _distribution_name(dist))
        }
        if not new:
            raise RuntimeError(i18n.get(Message.PM_PIP_EMPTY))
        # Pip stages independently. Re-read target ownership only after taking
        # the lock, and hold it through the caller's validation and any rollback.
        with _installation_lock(target):
            for dist in loaded_distributions():
                name = _distribution_name(dist)
                if name in new and dist.version != new[name].version:
                    raise RuntimeError(i18n.get(Message.PM_DEPENDENCY_BUSY, name=name))
            incoming = {
                path.relative_to(stage): path
                for path in stage.rglob("*")
                if path.is_file()
            }
            old_files, retained_files = set(), set()
            for dist in importlib.metadata.distributions(path=[str(target)]):
                files = distribution_files(dist, target)
                if _distribution_name(dist) in new:
                    old_files.update(files)
                else:
                    retained_files.update(files)
            changed = {
                path: source
                for path, source in incoming.items()
                if not _same_contents(target / path, source)
            }
            # Never overwrite another distribution's shared file with different bytes.
            if retained_files & changed.keys():
                raise RuntimeError(i18n.get(Message.PM_FILE_OWNERSHIP))
            removed = old_files - incoming.keys() - retained_files
            # Older pip installs may record bytecode. Removing a cache is safe even
            # when its unchanged source package is already imported.
            check_loaded_files(
                changed,
                {
                    path
                    for path in removed
                    if not (path.suffix == _BYTECODE and _BYTECODE_DIR in path.parts)
                },
                target,
            )
            # Runtime bytecode is not necessarily recorded by pip. Unchanged sources
            # retain their caches; replaced sources must not reuse timestamp caches.
            for path in changed.keys() | removed:
                if path.suffix == PYTHON_SOURCE_SUFFIX:
                    cache = (target / path).parent / _BYTECODE_DIR
                    removed.update(
                        p.relative_to(target)
                        for p in cache.glob(path.stem + ".*" + _BYTECODE)
                    )
            affected = set(changed) | removed
            for path in affected:
                if not (target / path).resolve().is_relative_to(target):
                    raise RuntimeError(i18n.get(Message.PM_PATH_OUTSIDE, path=path))
                if (target / path).is_dir():
                    raise RuntimeError(i18n.get(Message.PM_PATH_DIRECTORY, path=path))
            backup = temporary / _BACKUP_DIR
            saved, installed = [], []
            try:
                for path in sorted(affected):
                    destination = target / path
                    if destination.exists():
                        previous = backup / path
                        previous.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(destination, previous)
                        saved.append(path)
                    if path in changed:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(changed[path], destination)
                        installed.append(path)
                # Empty old .dist-info directories can shadow the new metadata.
                # Remove them before the caller validates the installed version.
                _remove_empty_directories({target / path for path in removed}, target)
                importlib.invalidate_caches()
                yield
            except BaseException:
                for path in reversed(installed):
                    (target / path).unlink()
                for path in reversed(saved):
                    destination = target / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup / path, destination)
                raise
            finally:
                _remove_empty_directories({target / path for path in affected}, target)
                importlib.invalidate_caches()
