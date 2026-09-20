"""Stage dependency installs and replace owned files without reloading live modules."""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.utils import canonicalize_name


def has_import(name: str) -> bool:
    """Locate an import without executing parent packages during install validation.

    Loaded packages can supply custom search paths. Unloaded packages are checked
    using their finders' paths; their initialization is left to plugin execution.
    """
    path = None
    parts = name.split(".")
    for index in range(len(parts)):
        fullname = ".".join(parts[: index + 1])
        module = sys.modules.get(fullname)
        if module is not None:
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


def has_duplicate_metadata(name: str, target: Path) -> bool:
    """Detect mixed installations left by pip --target without file replacement."""
    matches = (
        dist
        for dist in importlib.metadata.distributions(path=[str(target)])
        if canonicalize_name(dist.metadata["Name"] or "") == canonicalize_name(name)
    )
    return sum(1 for _ in matches) > 1


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


def check_loaded_files(incoming: dict[Path, Path], removed: set[Path], target: Path):
    """Reject changes to imported packages, including unversioned module shadows."""
    for name, module in tuple(sys.modules.items()):
        if module is None:
            continue
        origin = module.__dict__.get("__file__")
        locations = module.__dict__.get("__path__")
        relative = Path(*name.split("."))
        if not origin:
            # Adding a regular package over an active namespace changes imports.
            if locations is not None and relative / "__init__.py" in incoming:
                raise RuntimeError(
                    f"Package {name!r} is in use; restart ish before replacing it"
                )
            continue
        origin = Path(origin).resolve()
        if locations is not None:
            for path, source in incoming.items():
                if path.is_relative_to(relative):
                    current = origin.parent / path.relative_to(relative)
                    if (
                        not current.is_file()
                        or current.read_bytes() != source.read_bytes()
                    ):
                        raise RuntimeError(
                            f"Package {name!r} is in use; restart ish before replacing it"
                        )
            if any((target / path).is_relative_to(origin.parent) for path in removed):
                raise RuntimeError(
                    f"Package {name!r} is in use; restart ish before replacing it"
                )
        else:
            for path, source in incoming.items():
                if path.parent == relative.parent and (
                    path.name == relative.name + ".py"
                    or path.name.startswith(relative.name + ".")
                    and path.suffix in (".so", ".pyd")
                ):
                    if (
                        not origin.is_file()
                        or origin.read_bytes() != source.read_bytes()
                    ):
                        raise RuntimeError(
                            f"Module {name!r} is in use; restart ish before replacing it"
                        )
            if origin in {target / path for path in removed}:
                raise RuntimeError(
                    f"Module {name!r} is in use; restart ish before replacing it"
                )


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
        canonicalize_name(dist.metadata["Name"]): dist.version
        for dist in loaded_distributions()
        if dist.metadata["Name"]
    }
    with tempfile.TemporaryDirectory(
        prefix=".install-", dir=target.parent
    ) as temporary:
        temporary = Path(temporary)
        stage = temporary / "stage"
        constraints_path = temporary / "constraints.txt"
        constraints_path.write_text(
            "\n".join(
                [
                    *(f"{name}=={version}" for name, version in protected.items()),
                    *constraints,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        cmd = [
            python,
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--target",
            str(stage),
            "--constraint",
            str(constraints_path),
            requirement,
        ]
        subprocess.run(cmd, check=True)
        new = {
            canonicalize_name(dist.metadata["Name"]): dist
            for dist in importlib.metadata.distributions(path=[str(stage)])
            if dist.metadata["Name"]
        }
        if not new:
            raise RuntimeError("pip did not produce an installed distribution")
        # Recheck after pip returns, in case another thread imported a dependency.
        for dist in loaded_distributions():
            name = canonicalize_name(dist.metadata["Name"] or "")
            if name in new and dist.version != new[name].version:
                raise RuntimeError(
                    f"Dependency {name!r} is in use; restart ish before changing its version"
                )
        incoming = {
            path.relative_to(stage): path for path in stage.rglob("*") if path.is_file()
        }
        old_files, retained_files = set(), set()
        for dist in importlib.metadata.distributions(path=[str(target)]):
            files = distribution_files(dist, target)
            if canonicalize_name(dist.metadata["Name"] or "") in new:
                old_files.update(files)
            else:
                retained_files.update(files)
        changed = {
            path: source
            for path, source in incoming.items()
            if not (target / path).is_file()
            or (target / path).read_bytes() != source.read_bytes()
        }
        # Never overwrite another distribution's shared file with different bytes.
        if retained_files & changed.keys():
            raise RuntimeError(
                "Dependency installation would overwrite files owned by another package"
            )
        removed = old_files - incoming.keys() - retained_files
        # Older pip installs may record bytecode. Removing a cache is safe even
        # when its unchanged source package is already imported.
        check_loaded_files(
            changed,
            {
                path
                for path in removed
                if not (path.suffix == ".pyc" and "__pycache__" in path.parts)
            },
            target,
        )
        # Runtime bytecode is not necessarily recorded by pip. Unchanged sources
        # retain their caches; replaced sources must not reuse timestamp caches.
        for path in set(changed) | removed.copy():
            if path.suffix == ".py":
                cache = (target / path).parent / "__pycache__"
                removed.update(
                    p.relative_to(target) for p in cache.glob(path.stem + ".*.pyc")
                )
        affected = set(changed) | removed
        for path in affected:
            if not (target / path).resolve().is_relative_to(target):
                raise RuntimeError(
                    f"Dependency path leaves the plugin directory: {path}"
                )
            if (target / path).is_dir():
                raise RuntimeError(
                    f"Dependency file conflicts with an existing directory: {path}"
                )
        backup = temporary / "backup"
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
            importlib.invalidate_caches()
            yield
        except BaseException:
            for path in reversed(installed):
                (target / path).unlink()
            for path in reversed(saved):
                os.replace(backup / path, target / path)
            raise
        finally:
            _remove_empty_directories({target / path for path in affected}, target)
            importlib.invalidate_caches()
