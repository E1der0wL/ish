"""Manage plugin metadata, dependencies, module lifetimes, and separate install paths.

Metadata is read as AST literals, but loading plugins and installing packages with pip
execute code.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import re
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from importlib.metadata import PackageNotFoundError
from itertools import count, islice
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion

from ish.config import config
from ish.lang import Message, i18n
from ish.log import get_logger

from .dependencies import (
    PACKAGE_ENTRY,
    PYTHON_SOURCE_SUFFIX,
    has_duplicate_metadata,
    has_import,
    install_dependency,
)
from .metadata import DependencySpec, split_requirement

__all__ = ["PluginManager", "PluginInfo"]

PLUGIN_META = "PLUGIN_META"
_NORMALIZER = "normalize"
_PLUGIN_REQUIREMENT = re.compile(r"^([a-zA-Z0-9_./-]+?)([><=!~]+.+)?$")
_HIDDEN_PREFIX = "."
_DEBUG_FILE_LIMIT = 10
_EXTRA_MARKER = "extra"


def _metadata_string(value: object) -> str:
    """Require a literal string for a declared text field."""
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def _metadata_list(value: object) -> list[str]:
    """Copy a sequence of literal requirements into its owned list."""
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise TypeError("expected a list or tuple of strings")
    return list(value)


@dataclass
class PluginInfo:
    """Loaded plugin module and its declared version and dependency metadata."""

    name: str
    version: str = field(default="0.0.0", metadata={_NORMALIZER: _metadata_string})
    description: str = field(default="", metadata={_NORMALIZER: _metadata_string})
    author: str = field(default="", metadata={_NORMALIZER: _metadata_string})
    module: Optional[Any] = None
    requirements: List[str] = field(
        default_factory=list, metadata={_NORMALIZER: _metadata_list}
    )
    dependencies: List[str] = field(
        default_factory=list, metadata={_NORMALIZER: _metadata_list}
    )

    @classmethod
    def from_mapping(cls, name: str, meta: object) -> PluginInfo | None:
        """Normalize declared fields without overriding directory or module identity.

        Absent metadata and empty mappings retain their unregistered behavior.
        Unknown fields remain allowed; only fields owned by this declaration are
        interpreted. Defaults and normalizers come from the dataclass fields.
        """
        if meta is None:
            return None
        if not isinstance(meta, Mapping):
            raise TypeError(f"{PLUGIN_META} must be a mapping")
        if not meta:
            return None
        values = {}
        for field_name, normalize in _METADATA_FIELDS:
            if field_name in meta:
                try:
                    values[field_name] = normalize(meta[field_name])
                except TypeError as exc:
                    raise TypeError(f"{PLUGIN_META}.{field_name}: {exc}") from exc
        return cls(name=name, **values)


_METADATA_FIELDS = tuple(
    (declaration.name, declaration.metadata[_NORMALIZER])
    for declaration in fields(PluginInfo)
    if _NORMALIZER in declaration.metadata
)


@dataclass(frozen=True, slots=True)
class _Registration:
    """Record one published module with its optional metadata and metadata-list position."""

    module: Any
    info: PluginInfo | None
    info_order: int


class PluginRegistry:
    """In-memory registry mapping plugin names to modules and metadata."""

    def __init__(self):
        """Initialize a single registration store with independently ordered metadata."""
        self._entries: dict[str, _Registration] = {}
        self._info_order = count()

    def register(self, name: str, module: Any, info: PluginInfo = None) -> None:
        """Publish a module, retaining existing metadata only for that same module."""
        previous = self._entries.get(name)
        if info is None and previous is not None and previous.module is module:
            return
        order = 0
        if info is not None:
            order = (
                previous.info_order
                if previous is not None and previous.info is not None
                else next(self._info_order)
            )
        self._entries[name] = _Registration(module, info, order)

    def unregister(self, name: str) -> bool:
        """Remove a registered module and its metadata and report whether removal occurred."""
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> Optional[Any]:
        """Return the registered module, or None for an unknown name."""
        entry = self._entries.get(name)
        return entry.module if entry is not None else None

    def get_info(self, name: str) -> Optional[PluginInfo]:
        """Look up metadata for a registered plugin."""
        entry = self._entries.get(name)
        return entry.info if entry is not None else None

    def has(self, name: str) -> bool:
        """Check whether a module is registered under the given name."""
        return name in self._entries

    def list(self) -> Dict[str, Any]:
        """Return a copy of the module mapping to protect the internal store."""
        return {name: entry.module for name, entry in self._entries.items()}

    def list_info(self) -> List[PluginInfo]:
        """Return the currently registered metadata objects as a list."""
        with_info = (
            entry for entry in self._entries.values() if entry.info is not None
        )
        return [
            entry.info
            for entry in sorted(with_info, key=lambda entry: entry.info_order)
        ]

    def count(self) -> int:
        """Return the number of registered modules."""
        return len(self._entries)


class PluginManager:
    """Check dependencies and dynamically load trusted user plugins.

    Plugins run with the current user's privileges and are not sandboxed. Detect
    dependency cycles with a loading stack and unregister failed modules.
    """

    def __init__(self):
        """Prepare the registry, installation history, cycle-detection set, and logger."""
        self.registry = PluginRegistry()
        self.python_exe: Optional[str] = None
        self.plugins: List[Any] = []

        self._installed_libs: set = set()
        self._loading_stack: set = set()
        self._module_roots: dict[str, Path] = {}
        self._entry_modules: dict[str, tuple[str, Any]] = {}

        self.logger = get_logger()

    def _parse_plugin_requirement(
        self, req: str
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Parse ish plugin names separately from Python distribution requirements."""
        req, lib_name = split_requirement(req)

        match = _PLUGIN_REQUIREMENT.match(req.strip())
        if match:
            name = match.group(1).strip()
            version_spec = match.group(2).strip() if match.group(2) else None
            return name, version_spec, lib_name
        return req.strip(), None, lib_name

    @staticmethod
    def _get_plugin_entry_point(plugin_dir: Path) -> Optional[Path]:
        """Find the plugin's named Python file or __init__.py entry point."""
        name = plugin_dir.name
        candidate_files = (
            plugin_dir / (name + PYTHON_SOURCE_SUFFIX),
            plugin_dir / PACKAGE_ENTRY,
        )

        for candidate in candidate_files:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _get_plugin_path(name: str) -> Path:
        """Resolve the user source directory for a plugin name."""
        plugin_dir = config.PLUGIN_SCRIPT_DIR / name
        return plugin_dir

    def _get_plugin_meta(self, path: Path) -> object:
        """Read the literal PLUGIN_META value without executing the module."""
        # Parse the bytes just as the import loader does, including PEP 263 headers.
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = (node.target,)
            else:
                continue
            if any(
                isinstance(target, ast.Name) and target.id == PLUGIN_META
                for target in targets
            ):
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, TypeError) as exc:
                    raise TypeError(f"{PLUGIN_META} must be literal") from exc
        return None

    def _is_version_allowed(
        self, installed: str, spec: str | SpecifierSet | None
    ) -> bool:
        """Check a PEP 440 constraint and log and reject invalid constraints."""
        if not spec:
            return True
        try:
            allowed = spec if isinstance(spec, SpecifierSet) else SpecifierSet(spec)
            return allowed.contains(installed)
        except (InvalidSpecifier, InvalidVersion) as exc:
            self.logger.warning(
                "Invalid version requirement %r for %r: %s", spec, installed, exc
            )
            return False

    def _has_extras(self, requirement: Requirement, seen=None) -> bool:
        """Check installed metadata for an extras dependency tree without importing it.

        Only extras requests traverse dependency metadata. The visited set is
        local to this check, so cycles terminate without caching stale installs.
        """
        seen = set() if seen is None else seen
        key = (canonicalize_name(requirement.name), frozenset(requirement.extras))
        try:
            distribution = importlib.metadata.distribution(requirement.name)
        except PackageNotFoundError:
            return False
        if not self._is_version_allowed(distribution.version, requirement.specifier):
            return False
        if key in seen:
            return True
        seen.add(key)
        extras = {"", *requirement.extras}
        for value in distribution.requires or ():
            child = Requirement(value)
            if child.marker is not None and not any(
                child.marker.evaluate({_EXTRA_MARKER: extra}) for extra in extras
            ):
                continue
            if not self._has_extras(child, seen):
                return False
        return True

    def _is_library_available(
        self, library: str, *, import_parents: bool = True, installed: bool = False
    ) -> bool:
        """Check metadata and imports, distinguishing missing parents from broken imports."""
        dependency = DependencySpec.parse(library)
        requirement = dependency.requirement
        if not dependency.active:
            return True
        # A name/import alone cannot prove a direct URL's origin. Let pip resolve
        # it once per session, then retain the existing successful-install record.
        if (
            requirement.url
            and not installed
            and dependency.specification not in self._installed_libs
        ):
            return False
        if has_duplicate_metadata(requirement.name, config.PLUGIN_LIB_DIR):
            return False
        # Check the version before find_spec can import a dotted name's parent.
        if requirement.extras:
            if not self._has_extras(requirement):
                return False
        elif requirement.specifier:
            try:
                version = importlib.metadata.version(requirement.name)
            except PackageNotFoundError:
                return False
            if not self._is_version_allowed(version, requirement.specifier):
                return False
        name = dependency.import_name
        if not import_parents:
            return has_import(name)
        try:
            return importlib.util.find_spec(name) is not None
        except ModuleNotFoundError as exc:
            if exc.name and (exc.name == name or name.startswith(exc.name + ".")):
                return False
            # An existing parent's failed dependency import is not an absent parent.
            raise

    def _ensure_library(self, library: str) -> bool:
        """Use an available dependency or install and validate it before loading a plugin."""
        try:
            return self._is_library_available(library) or self._install_library(library)
        except InvalidRequirement as exc:
            self.logger.warning("Invalid Python requirement %r: %s", library, exc)
            return False

    def _dependency_constraints(self) -> list[str]:
        """Preserve requirements declared by plugins already registered in this session."""
        constraints = []
        for info in self.registry.list_info():
            for dependency in info.dependencies:
                try:
                    constraint = DependencySpec.parse(dependency).constraint
                except InvalidRequirement:
                    continue
                if constraint is not None:
                    constraints.append(constraint)
        return constraints

    def _ensure_plugin(self, plugin: str) -> bool:
        """Check an already loaded plugin or recursively load a required plugin."""
        plugin_name, plugin_version, _ = self._parse_plugin_requirement(plugin)
        if plugin_name in self._loading_stack:
            return False
        if self.registry.has(plugin_name):
            return self._matches_plugin_version(
                self.registry.get_info(plugin_name), plugin_version
            )
        plugin_dir = self._get_plugin_path(plugin_name)
        return (
            plugin_dir.exists()
            and self._resolve(plugin_dir, plugin_version) is not None
        )

    def _matches_plugin_version(
        self, info: PluginInfo | None, spec: str | None
    ) -> bool:
        """Check the same version admission rule before execution and for registered plugins."""
        return not spec or (
            info is not None and self._is_version_allowed(info.version, spec)
        )

    def _install_library(self, library: str) -> bool:
        """Install dependencies into the separate plugin directory using the selected
        Python's pip.

        Pass an argument vector rather than a shell string, and report installation
        failures as False.
        """
        if not config.PLUGIN_LIB_DIR or not self.python_exe:
            self.logger.warning(i18n.get(Message.PIP_INSTALL_FAIL, library=library))
            return False

        try:
            specification, _ = split_requirement(library)
            with install_dependency(
                self.python_exe,
                specification,
                config.PLUGIN_LIB_DIR,
                self._dependency_constraints(),
            ):
                if not self._is_library_available(
                    library, import_parents=False, installed=True
                ):
                    raise RuntimeError(
                        "Installed dependency does not provide the requested import and version"
                    )
            self._installed_libs.add(specification)
            return True
        except Exception as exc:
            self.logger.warning(
                "%s: %s", i18n.get(Message.PIP_INSTALL_FAIL, library=library), exc
            )
            return False

    def _load_plugin(
        self, plugin_file: Path, plugin_name: str, info: PluginInfo | None
    ) -> Optional[PluginInfo]:
        """Execute an admitted module without publishing an incomplete registration."""
        entry_name = plugin_file.stem
        if plugin_file.name == PACKAGE_ENTRY:
            full_module_name = plugin_name
        else:
            full_module_name = f"{plugin_name}.{entry_name}"

        root = plugin_file.parent.resolve()
        if full_module_name in sys.modules and not self._owns_module(
            sys.modules[full_module_name], root
        ):
            raise ImportError(
                f"Plugin entry {full_module_name!r} conflicts with an existing module"
            )
        if full_module_name != plugin_name and plugin_name in sys.modules:
            parent = sys.modules[plugin_name]
            paths = parent.__dict__.get("__path__", ()) if parent is not None else ()
            if not any(Path(path).resolve() == root for path in paths):
                raise ImportError(
                    f"Plugin namespace {plugin_name!r} is owned by another package"
                )

        spec = importlib.util.spec_from_file_location(full_module_name, plugin_file)
        if not spec or not spec.loader:
            return None

        module = importlib.util.module_from_spec(spec)
        # Keep exact ownership even if the entry file is a symlink outside its
        # package directory, or trusted module code changes its __file__ value.
        self._entry_modules[plugin_name] = (full_module_name, module)
        sys.modules[full_module_name] = module

        spec.loader.exec_module(module)
        if info is not None:
            info.module = module
        return info

    @staticmethod
    def _owns_module(module: Any, root: Path) -> bool:
        """Identify modules from this source root, including exclusive namespaces."""
        if module is None:
            return False
        origin = module.__dict__.get("__file__")
        if origin:
            return Path(origin).resolve().is_relative_to(root)
        locations = list(module.__dict__.get("__path__", ()))
        return bool(locations) and all(
            Path(path).resolve().is_relative_to(root) for path in locations
        )

    def _unload_plugin(self, plugin_name: str) -> None:
        """Remove owned namespace members after failed or completed plugin loading."""
        self.registry.unregister(plugin_name)
        root = self._module_roots.pop(plugin_name, None)
        entry = self._entry_modules.pop(plugin_name, None)
        if root is None:
            return None
        for name, module in tuple(sys.modules.items()):
            if (
                (name == plugin_name or name.startswith(plugin_name + "."))
                and (
                    module is entry[1]
                    if entry is not None and name == entry[0]
                    else self._owns_module(module, root)
                )
                and sys.modules.get(name) is module
            ):
                del sys.modules[name]
                parent_name, _, attribute = name.rpartition(".")
                parent = sys.modules.get(parent_name)
                if parent is not None and parent.__dict__.get(attribute) is module:
                    delattr(parent, attribute)
        importlib.invalidate_caches()
        return None

    def _resolve(
        self, plugin_dir: Path, version_spec: Optional[str] = None
    ) -> Optional[PluginInfo]:
        """Load a plugin and its dependencies and clean up failed or cyclic loading state."""
        plugin_name = plugin_dir.name
        entry_point = self._get_plugin_entry_point(plugin_dir)

        if not entry_point:
            return None

        if plugin_name in self._loading_stack:
            return None

        if self.registry.has(plugin_name):
            info = self.registry.get_info(plugin_name)
            return info if self._matches_plugin_version(info, version_spec) else None

        self._loading_stack.add(plugin_name)
        try:
            meta = self._get_plugin_meta(entry_point)
            info = PluginInfo.from_mapping(plugin_name, meta)
            if not self._matches_plugin_version(info, version_spec):
                return None
            if info is not None:
                for requirement in info.requirements:
                    if not self._ensure_plugin(requirement):
                        return None
                for dependency in info.dependencies:
                    if not self._ensure_library(dependency):
                        return None

            self._module_roots[plugin_name] = plugin_dir.resolve()
            plugin_info = self._load_plugin(entry_point, plugin_name, info)
            if not plugin_info:
                return self._unload_plugin(plugin_name)

            for plugin_dep in plugin_info.dependencies:
                if not self._ensure_library(plugin_dep):
                    return self._unload_plugin(plugin_name)

            for requirement in plugin_info.requirements:
                if not self._ensure_plugin(requirement):
                    return self._unload_plugin(plugin_name)
            self.registry.register(plugin_name, plugin_info.module, plugin_info)
            return plugin_info
        except BaseException:
            self._unload_plugin(plugin_name)
            raise
        finally:
            self._loading_stack.discard(plugin_name)

    def load(self) -> List[Any]:
        """Add installation directories to the import path and load user plugins
        sequentially.

        Use the current interpreter to install dependencies. Log individual plugin
        errors and continue loading other plugins.
        """
        if str(config.PLUGIN_LIB_DIR) not in sys.path:
            sys.path.insert(0, str(config.PLUGIN_LIB_DIR))

        if str(config.PLUGIN_SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(config.PLUGIN_SCRIPT_DIR))

        # Use the interpreter that owns ish's environment, including under uv.
        self.python_exe = sys.executable
        return self._load_plugins()

    def _load_plugins(self) -> List[Any]:
        """Load plugin directories after configuring dependency installation and imports."""
        loaded_infos = []
        plugin_dirs = [
            d
            for d in config.PLUGIN_SCRIPT_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(_HIDDEN_PREFIX)
        ]
        for plugin_dir in plugin_dirs:
            try:
                info = self._resolve(plugin_dir)
                if info:
                    loaded_infos.append(info)
                else:
                    self.logger.warning(
                        i18n.get(Message.PM_LOAD_FAIL, plugin_file=plugin_dir)
                    )
            except Exception:
                format_exc = str(traceback.format_exc())
                target_path = str(config.PLUGIN_LIB_DIR)
                log_sys_path = f"sys.path count: {len(sys.path)}\n" + "\n".join(
                    [f"  - {p}" for p in sys.path]
                )
                lib_dir_exists = config.PLUGIN_LIB_DIR.exists()
                lib_dir_contents = ""
                if lib_dir_exists:
                    try:
                        dir_contents = islice(
                            config.PLUGIN_LIB_DIR.iterdir(), _DEBUG_FILE_LIMIT
                        )
                        lib_dir_contents = "\n" + "\n".join(
                            f"  - {f.name}" for f in dir_contents
                        )
                    except Exception:
                        lib_dir_contents = "Error reading directory"

                log_lib_dir = (
                    f"Plugin Lib Dir exists: {lib_dir_exists}\n"
                    f"Plugin Lib Dir path: {target_path}\n"
                    f"Plugin Lib Dir contents (top {_DEBUG_FILE_LIMIT}): {lib_dir_contents}"
                )
                log_python_info = (
                    f"Python Executable: {self.python_exe}\n"
                    f"Python Version: {sys.version}"
                )
                errors = [
                    "\n[Debug Info]",
                    log_python_info,
                    "\n[Sys Path]",
                    log_sys_path,
                    "\n[Plugin Lib Dir]",
                    log_lib_dir,
                    "\n[Traceback]",
                    format_exc,
                    "\n---",
                ]
                self.logger.error(
                    i18n.get(
                        Message.PM_LOAD_ERROR,
                        plugin_file=plugin_dir,
                        error="\n".join(errors),
                    )
                )

        return loaded_infos

    def get(self, name: str) -> Optional[Any]:
        """Look up a registered plugin module by name."""
        return self.registry.get(name)
