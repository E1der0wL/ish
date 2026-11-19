"""Manage plugin metadata, dependencies, module lifetimes, and separate install paths.

Metadata is read as AST literals, but loading plugins and installing packages with pip
execute code.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import StrEnum
from itertools import count, islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ish.config import config
from ish.lang import Message, i18n
from ish.log import get_logger

from . import PACKAGE_ENTRY, PYTHON_SOURCE_SUFFIX
from .metadata import DependencySpec, split_requirement

if TYPE_CHECKING:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

__all__ = ["PluginManager", "PluginInfo", "PluginCandidate", "PluginCategory"]

PLUGIN_META = "PLUGIN_META"


def _metadata_string(value: object) -> str:
    """Require a literal string for a declared text field."""
    if not isinstance(value, str):
        raise TypeError(i18n.get(Message.PM_EXPECT_STRING))
    return value


def _metadata_list(value: object) -> list[str]:
    """Copy a sequence of literal requirements into its owned list."""
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise TypeError(i18n.get(Message.PM_EXPECT_STRINGS))
    return list(value)


class PluginCategory(StrEnum):
    """Declare where the package appears in settings, independently of its setup API."""

    PLUGIN = "plugin"
    MODULE = "module"
    BOTH = "both"

    @classmethod
    def declared(cls, value):
        """Keep packages with unrecognized display metadata in the general catalog."""
        try:
            return cls(value)
        except ValueError:
            return cls.PLUGIN


@dataclass
class PluginInfo:
    """Loaded plugin module and its declared version and dependency metadata."""

    NORMALIZER = "normalize"

    name: str
    version: str = field(default="0.0.0", metadata={NORMALIZER: _metadata_string})
    description: str = field(default="", metadata={NORMALIZER: _metadata_string})
    author: str = field(default="", metadata={NORMALIZER: _metadata_string})
    module: Optional[Any] = None
    requirements: List[str] = field(
        default_factory=list, metadata={NORMALIZER: _metadata_list}
    )
    dependencies: List[str] = field(
        default_factory=list, metadata={NORMALIZER: _metadata_list}
    )
    category: PluginCategory = field(
        default=PluginCategory.PLUGIN, metadata={NORMALIZER: PluginCategory.declared}
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
            raise TypeError(i18n.get(Message.PM_META_MAPPING, field=PLUGIN_META))
        if not meta:
            return None
        values = {}
        for declaration in fields(cls):
            normalize = declaration.metadata.get(cls.NORMALIZER)
            field_name = declaration.name
            if normalize is not None and field_name in meta:
                try:
                    values[field_name] = normalize(meta[field_name])
                except TypeError as exc:
                    raise TypeError(f"{PLUGIN_META}.{field_name}: {exc}") from exc
        return cls(name=name, **values)


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    """Describe source metadata without claiming that a plugin can be loaded."""

    name: str
    path: Path
    info: PluginInfo | None
    error: str | None = None


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

    REQUIREMENT = re.compile(r"^([a-zA-Z0-9_./-]+?)([><=!~]+.+)?$")
    HIDDEN_PREFIX = "."
    DEBUG_FILE_LIMIT = 10
    EXTRA_MARKER = "extra"

    def __init__(self, *, script_dir: Path | None = None, lib_dir: Path | None = None):
        """Prepare the registry, installation history, cycle-detection set, and logger."""
        self._script_dir = Path(script_dir) if script_dir is not None else None
        self._lib_dir = Path(lib_dir) if lib_dir is not None else None
        self.registry = PluginRegistry()
        self.python_exe: Optional[str] = None

        self._installed_libs: set = set()
        self._loading_stack: set = set()
        self._module_roots: dict[str, Path] = {}
        self._entry_modules: dict[str, tuple[str, Any]] = {}

        self.logger = get_logger()

    @property
    def script_dir(self) -> Path:
        """Use an explicit source root or the current common configuration."""
        return (
            self._script_dir
            if self._script_dir is not None
            else config.PLUGIN_SCRIPT_DIR
        )

    @property
    def lib_dir(self) -> Path:
        """Use an explicit library root or the current common configuration."""
        return self._lib_dir if self._lib_dir is not None else config.PLUGIN_LIB_DIR

    def _parse_plugin_requirement(
        self, req: str
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Parse ish plugin names separately from Python distribution requirements."""
        req, lib_name = split_requirement(req)

        match = self.REQUIREMENT.match(req.strip())
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
            if candidate.is_file():
                return candidate
        return None

    def _get_plugin_path(self, name: str) -> Path:
        """Resolve the user source directory for a plugin name."""
        if (
            not isinstance(name, str)
            or not name
            or name.startswith(self.HIDDEN_PREFIX)
            or "/" in name
            or "\\" in name
        ):
            raise ValueError(i18n.get(Message.PM_NAME_INVALID))
        return self.script_dir / name

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
                    raise TypeError(
                        i18n.get(Message.PM_META_LITERAL, field=PLUGIN_META)
                    ) from exc
        return None

    def discover(self) -> tuple[PluginCandidate, ...]:
        """List source metadata without imports, dependency installs or registration.

        A candidate is not a readiness or compatibility claim. Loading still
        validates dependencies and the selected execution API at session start.
        """
        return tuple(self.iter_candidates())

    def iter_candidates(self, *, cancelled=None):
        """Yield candidates in display order so a closed catalog can stop reading."""
        root = self.script_dir
        if cancelled is not None and cancelled():
            return
        try:
            entries = os.scandir(root)
        except FileNotFoundError:
            return
        with entries:
            names = []
            for item in entries:
                if cancelled is not None and cancelled():
                    return
                if not item.name.startswith(self.HIDDEN_PREFIX) and item.is_dir():
                    names.append(item.name)
        for name in sorted(names):
            if cancelled is not None and cancelled():
                return
            directory = root / name
            entry = directory
            info = None
            error = None
            try:
                entry = self._get_plugin_entry_point(
                    self._get_plugin_path(directory.name)
                )
                if entry is None:
                    raise ValueError(
                        i18n.get(Message.PM_LOAD_FAIL, plugin_file=directory)
                    )
                info = PluginInfo.from_mapping(
                    directory.name, self._get_plugin_meta(entry)
                )
                if info is None:
                    raise ValueError(i18n.get(Message.PM_LOAD_FAIL, plugin_file=entry))
            except Exception as failure:
                error = str(failure)
            yield PluginCandidate(directory.name, entry or directory, info, error)

    def _is_version_allowed(
        self, installed: str, spec: str | SpecifierSet | None
    ) -> bool:
        """Check a PEP 440 constraint and log and reject invalid constraints."""
        if not spec:
            return True
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion

        try:
            allowed = spec if isinstance(spec, SpecifierSet) else SpecifierSet(spec)
            return allowed.contains(installed)
        except (InvalidSpecifier, InvalidVersion) as exc:
            self.logger.warning(
                i18n.get(
                    Message.PM_VERSION_INVALID,
                    requirement=spec,
                    version=installed,
                    error=exc,
                )
            )
            return False

    def _has_dependencies(
        self, requirement: Requirement, seen=None, checked=None
    ) -> bool:
        """Validate the active distribution tree and its files without importing it."""
        from importlib import metadata

        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        from .dependencies import has_distribution_files, has_duplicate_metadata

        seen = set() if seen is None else seen
        checked = set() if checked is None else checked
        key = (canonicalize_name(requirement.name), frozenset(requirement.extras))
        try:
            distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            return False
        if not self._is_version_allowed(distribution.version, requirement.specifier):
            return False
        name = key[0]
        if name not in checked:
            if has_duplicate_metadata(requirement.name, self.lib_dir):
                return False
            root = Path(distribution.locate_file("")).resolve()
            if not has_distribution_files(
                distribution, required=root.is_relative_to(self.lib_dir.resolve())
            ):
                return False
            checked.add(name)
        if key in seen:
            return True
        seen.add(key)
        extras = {"", *requirement.extras}
        for value in distribution.requires or ():
            child = Requirement(value)
            if child.marker is not None and not any(
                child.marker.evaluate({self.EXTRA_MARKER: extra}) for extra in extras
            ):
                continue
            if not self._has_dependencies(child, seen, checked):
                return False
        return True

    def _is_library_available(
        self, library: str, *, import_parents: bool = True, installed: bool = False
    ) -> bool:
        """Check installed files, active dependencies and the requested import."""
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
        # File validation runs before imports so an incomplete package is repaired
        # while the existing installation transaction can still roll it back.
        if not self._has_dependencies(requirement):
            return False
        name = dependency.import_name
        if not import_parents:
            from .dependencies import has_import

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
        from packaging.requirements import InvalidRequirement

        try:
            return self._is_library_available(library) or self._install_library(library)
        except InvalidRequirement as exc:
            self.logger.warning(
                i18n.get(Message.PM_REQUIREMENT_INVALID, requirement=library, error=exc)
            )
            return False

    def _dependency_constraints(self) -> list[str]:
        """Preserve requirements declared by plugins already registered in this session."""
        from packaging.requirements import InvalidRequirement

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
        from .dependencies import install_dependency

        if not self.lib_dir or not self.python_exe:
            self.logger.warning(i18n.get(Message.PIP_INSTALL_FAIL, library=library))
            return False

        try:
            specification, _ = split_requirement(library)
            with install_dependency(
                self.python_exe,
                specification,
                self.lib_dir,
                self._dependency_constraints(),
            ):
                if not self._is_library_available(
                    library, import_parents=False, installed=True
                ):
                    raise RuntimeError(i18n.get(Message.PM_INSTALLED_INVALID))
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
                i18n.get(Message.PM_ENTRY_CONFLICT, name=full_module_name)
            )
        if full_module_name != plugin_name and plugin_name in sys.modules:
            parent = sys.modules[plugin_name]
            paths = parent.__dict__.get("__path__", ()) if parent is not None else ()
            if not any(Path(path).resolve() == root for path in paths):
                raise ImportError(
                    i18n.get(Message.PM_NAMESPACE_CONFLICT, name=plugin_name)
                )

        spec = importlib.util.spec_from_file_location(full_module_name, plugin_file)
        if not spec or not spec.loader:
            return None

        module = importlib.util.module_from_spec(spec)
        # Keep exact ownership even if the entry file is a symlink outside its
        # package directory, or trusted module code changes its __file__ value.
        self._entry_modules[plugin_name] = (full_module_name, module)
        sys.modules[full_module_name] = module

        # Editable entry files must not reuse timestamp bytecode when an edit
        # preserves both size and the filesystem's timestamp granularity.
        code = compile(
            plugin_file.read_bytes(), str(plugin_file), "exec", dont_inherit=True
        )
        exec(code, module.__dict__)
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

    def load(self, names: Sequence[str]) -> List[Any]:
        """Load an explicit plugin selection in order.

        Use the current interpreter to install dependencies. Log individual plugin
        errors and continue loading other plugins. An empty selection runs none.
        """
        if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
            raise TypeError(i18n.get(Message.PM_SELECTION_INVALID))
        if str(self.lib_dir) not in sys.path:
            sys.path.insert(0, str(self.lib_dir))

        if str(self.script_dir) not in sys.path:
            sys.path.insert(0, str(self.script_dir))

        # Use the interpreter that owns ish's environment, including under uv.
        self.python_exe = sys.executable
        return self._load_plugins(names)

    def _load_plugins(self, names: Sequence[str]) -> List[Any]:
        """Load plugin directories after configuring dependency installation and imports."""
        loaded_infos = []
        visited = set()
        for name in names:
            plugin_dir = self.script_dir / str(name)
            try:
                plugin_dir = self._get_plugin_path(name)
                if name in visited:
                    continue
                visited.add(name)
                info = self._resolve(plugin_dir)
                if info:
                    loaded_infos.append(info)
                else:
                    self.logger.warning(
                        i18n.get(Message.PM_LOAD_FAIL, plugin_file=plugin_dir)
                    )
            except Exception:
                format_exc = str(traceback.format_exc())
                target_path = str(self.lib_dir)
                log_sys_path = i18n.get(
                    Message.PM_DEBUG_PATHS,
                    count=len(sys.path),
                    paths="\n".join(f"  - {p}" for p in sys.path),
                )
                lib_dir_exists = self.lib_dir.exists()
                lib_dir_contents = ""
                if lib_dir_exists:
                    try:
                        dir_contents = islice(
                            self.lib_dir.iterdir(), self.DEBUG_FILE_LIMIT
                        )
                        lib_dir_contents = "\n" + "\n".join(
                            f"  - {f.name}" for f in dir_contents
                        )
                    except Exception:
                        lib_dir_contents = i18n.get(Message.PM_DEBUG_DIRECTORY_READ)

                log_lib_dir = i18n.get(
                    Message.PM_DEBUG_LIBRARY,
                    exists=lib_dir_exists,
                    path=target_path,
                    limit=self.DEBUG_FILE_LIMIT,
                    contents=lib_dir_contents,
                )
                log_python_info = i18n.get(
                    Message.PM_DEBUG_PYTHON,
                    executable=self.python_exe,
                    version=sys.version,
                )
                errors = [
                    "\n[" + i18n.get(Message.PM_DEBUG_TITLE) + "]",
                    log_python_info,
                    "\n[" + i18n.get(Message.PM_DEBUG_SYS_PATH) + "]",
                    log_sys_path,
                    "\n[" + i18n.get(Message.PM_DEBUG_LIB_DIR) + "]",
                    log_lib_dir,
                    "\n[" + i18n.get(Message.PM_DEBUG_TRACEBACK) + "]",
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
