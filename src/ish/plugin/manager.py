"""Manage plugin metadata, dependencies, module lifetimes, and separate install paths.

Metadata is read as AST literals, but loading plugins and installing packages with pip
execute code.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion

from ish.config import config
from ish.lang import i18n
from ish.log import get_logger

from .dependencies import has_duplicate_metadata, has_import, install_dependency

__all__ = ["PluginManager", "PluginInfo"]


@dataclass
class PluginInfo:
    """Loaded plugin module and its declared version and dependency metadata."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    module: Optional[Any] = None
    requirements: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)


class PluginRegistry:
    """In-memory registry mapping plugin names to modules and metadata."""

    PLUGIN_META: str = "PLUGIN_META"
    NAME: str = "name"
    VERSION: str = "version"
    DESCRIPTION: str = "description"
    AUTHOR: str = "author"
    MODULE: str = "module"
    REQUIREMENTS: str = "requirements"
    DEPENDENCIES: str = "dependencies"

    def __init__(self):
        """Initialize empty module and metadata stores."""
        self._modules: Dict[str, Any] = {}
        self._info: Dict[str, PluginInfo] = {}

    def register(self, name: str, module: Any, info: PluginInfo = None) -> None:
        """Register a module and store its metadata when provided."""
        self._modules[name] = module
        if info:
            self._info[name] = info

    def unregister(self, name: str) -> bool:
        """Remove a registered module and its metadata and report whether removal occurred."""
        if name in self._modules:
            del self._modules[name]
            self._info.pop(name, None)
            return True
        return False

    def get(self, name: str) -> Optional[Any]:
        """Return the registered module, or None for an unknown name."""
        return self._modules.get(name)

    def get_info(self, name: str) -> Optional[PluginInfo]:
        """Look up metadata for a registered plugin."""
        return self._info.get(name)

    def has(self, name: str) -> bool:
        """Check whether a module is registered under the given name."""
        return name in self._modules

    def list(self) -> Dict[str, Any]:
        """Return a copy of the module mapping to protect the internal store."""
        return self._modules.copy()

    def list_info(self) -> List[PluginInfo]:
        """Return the currently registered metadata objects as a list."""
        return list(self._info.values())

    def count(self) -> int:
        """Return the number of registered modules."""
        return len(self._modules)


class PluginManager:
    """Check dependencies and dynamically load trusted user plugins.

    Plugins run with the current user's privileges and are not sandboxed. Detect
    dependency cycles with a loading stack and unregister failed modules.
    """

    SEP: str = "|"

    def __init__(self):
        """Prepare the registry, installation history, cycle-detection set, and logger."""
        self.registry = PluginRegistry()
        self.python_exe: Optional[str] = None
        self.plugins: List[Any] = []

        self._installed_libs: set = set()
        self._installed_plugins: set = set()
        self._loading_stack: set = set()

        self.logger = get_logger()

    def _parse_requirement(self, req: str) -> Tuple[str, Optional[str], Optional[str]]:
        """Separate the distribution name, version constraint, and optional |import_name."""
        sep = self.SEP
        lib_name = None
        if sep in req:
            parts = req.split(sep, 1)
            req = parts[0].strip()
            lib_name = parts[1].strip()

        pattern = r"^([a-zA-Z0-9_./-]+?)([><=!~]+.+)?$"
        match = re.match(pattern, req.strip())
        if match:
            name = match.group(1).strip()
            version_spec = match.group(2).strip() if match.group(2) else None
            return name, version_spec, lib_name
        return req.strip(), None, lib_name

    @staticmethod
    def _get_plugin_entry_point(plugin_dir: Path) -> Optional[Path]:
        """Find the plugin's named Python file or __init__.py entry point."""
        name = plugin_dir.name
        candidate_files = [plugin_dir / f"{name}.py", plugin_dir / "__init__.py"]

        for candidate in candidate_files:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _get_plugin_path(name: str) -> Path:
        """Resolve the user source directory for a plugin name."""
        plugin_dir = config.PLUGIN_SCRIPT_DIR / name
        return plugin_dir

    def _get_plugin_meta(self, path: Path) -> Optional[Dict]:
        """Read the literal PLUGIN_META value without executing the module."""
        try:
            with path.open("r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source)
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id == self.registry.PLUGIN_META
                        ):
                            return ast.literal_eval(node.value)
            return None
        except Exception:
            return None

    def _ensure_version(self, installed: str, spec: Optional[str]) -> bool:
        """Check a PEP 440 constraint and log and reject invalid constraints."""
        if not spec:
            return True
        try:
            return SpecifierSet(spec).contains(installed)
        except (InvalidSpecifier, InvalidVersion) as exc:
            self.logger.warning(
                "Invalid version requirement %r for %r: %s", spec, installed, exc
            )
            return False

    def _library_available(self, library: str, *, import_parents: bool = True) -> bool:
        """Check metadata and imports, distinguishing missing parents from broken imports."""
        library_name, library_version, library_origin = self._parse_requirement(library)
        if has_duplicate_metadata(library_name, config.PLUGIN_LIB_DIR):
            return False
        # Check the version before find_spec can import a dotted name's parent.
        if library_version is not None:
            try:
                version = importlib.metadata.version(library_name)
            except PackageNotFoundError:
                return False
            if not self._ensure_version(version, library_version):
                return False
        name = library_origin or library_name
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
        return self._library_available(library) or self._load_library(library)

    def _dependency_constraints(self) -> list[str]:
        """Preserve requirements declared by plugins already registered in this session."""
        constraints = []
        for info in self.registry.list_info():
            for dependency in info.dependencies:
                try:
                    req = Requirement(dependency.split(self.SEP, 1)[0])
                except InvalidRequirement:
                    continue
                if req.marker is not None and not req.marker.evaluate():
                    continue
                if req.url:
                    constraints.append(f"{req.name} @ {req.url}")
                else:
                    constraints.append(f"{req.name}{req.specifier}")
        return constraints

    def _ensure_plugin(self, plugin: str) -> bool:
        """Check an already loaded plugin or recursively load a required plugin."""
        plugin_name, plugin_version, _ = self._parse_requirement(plugin)
        if self.registry.has(plugin_name):
            if plugin_version is None:
                return True
            info = self.registry.get_info(plugin_name)
            if info and self._ensure_version(info.version, plugin_version):
                return True
            else:
                return False
        else:
            plugin_dir = self._get_plugin_path(plugin_name)
            if plugin_dir.exists():
                info = self._resolve(plugin_dir)
                if info is not None:
                    return self._ensure_version(info.version, plugin_version)
            return False

    def _load_library(self, library: str) -> bool:
        """Install dependencies into the separate plugin directory using the selected
        Python's pip.

        Pass an argument vector rather than a shell string, and report installation
        failures as False.
        """
        if not config.PLUGIN_LIB_DIR:
            self.logger.warning(i18n.get("pip_install_fail", library=library))
            return False

        if not self.python_exe:
            self.logger.warning(i18n.get("pip_install_fail", library=library))
            return False

        try:
            with install_dependency(
                self.python_exe,
                library.split(self.SEP, 1)[0],
                config.PLUGIN_LIB_DIR,
                self._dependency_constraints(),
            ):
                if not self._library_available(library, import_parents=False):
                    raise RuntimeError(
                        "Installed dependency does not provide the requested import and version"
                    )
            self._installed_libs.add(library.split(self.SEP, 1)[0])
            return True
        except Exception as exc:
            self.logger.warning(
                "%s: %s", i18n.get("pip_install_fail", library=library), exc
            )
            return False

    def _load_plugin(
        self, plugin_file: Path, plugin_name: str, meta: Optional[Dict]
    ) -> Optional[PluginInfo]:
        """Check library dependencies, execute the module, and register its metadata."""
        dependencies = []
        if meta:
            dependencies = meta.get(self.registry.DEPENDENCIES, [])
        for dep in dependencies:
            if not self._ensure_library(dep):
                return None

        entry_name = plugin_file.stem
        if entry_name == "__init__":
            full_module_name = plugin_name
        else:
            full_module_name = f"{plugin_name}.{entry_name}"

        spec = importlib.util.spec_from_file_location(full_module_name, plugin_file)
        if not spec or not spec.loader:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[full_module_name] = module

        try:
            spec.loader.exec_module(module)
            if meta:
                info = PluginInfo(
                    module=module,
                    name=plugin_name,
                    version=meta.get(self.registry.VERSION, "0.0.0"),
                    author=meta.get(self.registry.AUTHOR, ""),
                    description=meta.get(self.registry.DESCRIPTION, ""),
                    requirements=meta.get(self.registry.REQUIREMENTS, []),
                    dependencies=meta.get(self.registry.DEPENDENCIES, []),
                )
                self.registry.register(plugin_name, module, info)
                return info

        except Exception:
            sys.modules.pop(full_module_name, None)
            raise

        return None

    @staticmethod
    def _unload_library(library_name: str) -> None:
        """Remove a library and its metadata using the existing directory naming
        convention.

        This is not a complete pip RECORD-based uninstall and does not identify shared
        namespace packages.
        """
        target_dist_infos = [
            folder
            for folder in config.PLUGIN_LIB_DIR.iterdir()
            if folder.is_dir()
            and folder.name.lower().startswith(f"{library_name.lower()}-")
            and folder.name.endswith(".dist-info")
        ]

        if not target_dist_infos:
            target_dist_infos = [
                folder
                for folder in config.PLUGIN_LIB_DIR.iterdir()
                if folder.is_dir()
                and folder.name.lower().startswith(f"{library_name.lower()}-")
                and folder.name.endswith(".egg-info")
            ]

        if not target_dist_infos:
            return

        for dist_info in target_dist_infos:
            shutil.rmtree(dist_info)

            pkg_folder_name = dist_info.name.split("-")[0]
            pkg_folder = config.PLUGIN_LIB_DIR / pkg_folder_name

            if pkg_folder.exists() and pkg_folder.is_dir():
                shutil.rmtree(pkg_folder)

            cache_dir = config.PLUGIN_LIB_DIR / "__pycache__"
            if cache_dir.exists():
                for cache_file in cache_dir.glob(f"{pkg_folder_name}*.pyc"):
                    cache_file.unlink()

    def _unload_plugin(self, plugin_name: str) -> None:
        """Remove the plugin tree from the registry and sys.modules."""
        if self.registry.get(plugin_name) is None:
            return None

        module = self.registry.get(plugin_name)
        self.registry.unregister(plugin_name)
        module_name = module.__name__
        for name in list(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                sys.modules.pop(name, None)
        importlib.invalidate_caches()
        return None

    def _resolve(self, plugin_dir: Path) -> Optional[PluginInfo]:
        """Load a plugin and its dependencies and clean up failed or cyclic loading state."""
        plugin_name = plugin_dir.name
        entry_point = self._get_plugin_entry_point(plugin_dir)

        if not entry_point:
            return None

        if self.registry.has(plugin_name):
            return self.registry.get_info(plugin_name)

        if plugin_name in self._loading_stack:
            return None

        self._loading_stack.add(plugin_name)
        try:
            meta = self._get_plugin_meta(entry_point)
            plugin_info = self._load_plugin(entry_point, plugin_name, meta)
            if not plugin_info:
                return self._unload_plugin(plugin_name)

            for plugin_dep in plugin_info.dependencies:
                if not self._ensure_library(plugin_dep):
                    return self._unload_plugin(plugin_name)

            for plugin_req in plugin_info.requirements:
                if not self._ensure_plugin(plugin_req):
                    return self._unload_plugin(plugin_name)
            return plugin_info
        except Exception:
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
            if d.is_dir() and not d.name.startswith(".")
        ]
        for plugin_dir in plugin_dirs:
            plugin_name = plugin_dir.name
            try:
                info = self._resolve(plugin_dir)
                if info:
                    loaded_infos.append(info)
                else:
                    self.logger.warning(
                        i18n.get("pm_load_fail", plugin_file=plugin_dir)
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
                        dir_contents = config.PLUGIN_LIB_DIR.iterdir()
                        if dir_contents:
                            lib_dir_contents = "\n" + "\n".join(
                                [f"  - {f.name}" for f in dir_contents][:10]
                            )
                    except Exception:
                        lib_dir_contents = "Error reading directory"

                log_lib_dir = (
                    f"Plugin Lib Dir exists: {lib_dir_exists}\n"
                    f"Plugin Lib Dir path: {target_path}\n"
                    f"Plugin Lib Dir contents (top 10): {lib_dir_contents}"
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
                        "pm_load_error", plugin_file=plugin_dir, error="\n".join(errors)
                    )
                )
                self._unload_plugin(plugin_name)
            finally:
                self._loading_stack.discard(plugin_name)

        return loaded_infos

    def get(self, name: str) -> Optional[Any]:
        """Look up a registered plugin module by name."""
        return self.registry.get(name)
