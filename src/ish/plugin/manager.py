from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import re
import shutil
import subprocess
import sys
import traceback
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple
from dataclasses import dataclass, field

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion

from ish.config import config
from ish.lang import i18n
from ish.log import get_logger

__all__ = ['PluginManager', 'PluginInfo']


@dataclass
class PluginInfo:
	name: str
	version: str = "0.0.0"
	description: str = ""
	author: str = ""
	module: Optional[Any] = None
	requirements: List[str] = field(default_factory=list)
	dependencies: List[str] = field(default_factory=list)


class PluginRegistry:
	PLUGIN_META: str = 'PLUGIN_META'
	NAME: str = 'name'
	VERSION: str = 'version'
	DESCRIPTION: str = 'description'
	AUTHOR: str = 'author'
	MODULE: str = 'module'
	REQUIREMENTS: str = 'requirements'
	DEPENDENCIES: str = 'dependencies'

	def __init__(self):
		self._modules: Dict[str, Any] = {}
		self._info: Dict[str, PluginInfo] = {}

	def register(self, name: str, module: Any, info: PluginInfo = None) -> None:
		self._modules[name] = module
		if info:
			self._info[name] = info

	def unregister(self, name: str) -> bool:
		if name in self._modules:
			del self._modules[name]
			self._info.pop(name, None)
			return True
		return False

	def get(self, name: str) -> Optional[Any]:
		return self._modules.get(name)

	def get_info(self, name: str) -> Optional[PluginInfo]:
		return self._info.get(name)

	def has(self, name: str) -> bool:
		return name in self._modules

	def list(self) -> Dict[str, Any]:
		return self._modules.copy()

	def list_info(self) -> List[PluginInfo]:
		return list(self._info.values())

	def count(self) -> int:
		return len(self._modules)


class PluginManager:
	SEP: str = '|'

	def __init__(self):
		self.registry = PluginRegistry()
		self.python_exe: Optional[str] = None
		self.plugins: List[Any] = []

		self._installed_libs: set = set()
		self._installed_plugins: set = set()
		self._loading_stack: set = set()

		self.logger = get_logger()

	def _parse_requirement(self, req: str) -> Tuple[str, Optional[str], Optional[str]]:
		sep = self.SEP
		lib_name = None
		if sep in req:
			parts = req.split(sep, 1)
			req = parts[0].strip()
			lib_name = parts[1].strip()

		pattern = r'^([a-zA-Z0-9_./-]+?)([><=!~]+.+)?$'
		match = re.match(pattern, req.strip())
		if match:
			name = match.group(1).strip()
			version_spec = match.group(2).strip() if match.group(2) else None
			return name, version_spec, lib_name
		return req.strip(), None, lib_name

	@staticmethod
	def _get_plugin_entry_point(plugin_dir: Path) -> Optional[Path]:
		name = plugin_dir.name
		candidate_files = [
			plugin_dir / f"{name}.py",
			plugin_dir / "__init__.py"
		]

		for candidate in candidate_files:
			if candidate.exists() and candidate.is_file():
				return candidate
		return None

	@staticmethod
	def _get_plugin_path(name: str) -> Path:
		plugin_dir = config.PLUGIN_SCRIPT_DIR / name
		return plugin_dir

	def _get_plugin_meta(self, path: Path) -> Optional[Dict]:
		try:
			with path.open("r", encoding="utf-8") as f:
				source = f.read()
			tree = ast.parse(source)
			for node in tree.body:
				if isinstance(node, ast.Assign):
					for target in node.targets:
						if isinstance(target, ast.Name) and target.id == self.registry.PLUGIN_META:
							return ast.literal_eval(node.value)
			return None
		except Exception:
			return None

	def _ensure_version(self, installed: str, spec: Optional[str]) -> bool:
		if not spec:
			return True
		try:
			return SpecifierSet(spec).contains(installed)
		except (InvalidSpecifier, InvalidVersion) as exc:
			self.logger.warning('Invalid version requirement %r for %r: %s', spec, installed, exc)
			return False

	def _ensure_library(self, library: str) -> bool:
		library_name, library_version, library_origin = self._parse_requirement(library)
		if importlib.util.find_spec(library_name if not library_origin else library_origin) is None:
			return self._load_library(library)
		try:
			if library_version is None:
				return True
			version = importlib.metadata.version(library_name)
			if not self._ensure_version(version, library_version):
				return self._load_library(library)
		except PackageNotFoundError:
			return self._load_library(library)
		return True

	def _ensure_plugin(self, plugin: str) -> bool:
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
		if not config.PLUGIN_LIB_DIR:
			self.logger.warning(i18n.get("pip_install_fail", library=library))
			return False

		if not self.python_exe:
			self.logger.warning(i18n.get("pip_install_fail", library=library))
			return False

		library = library.split(self.SEP)[0]
		cmd = [
			self.python_exe, "-m", "pip", "install",
			"--target", str(config.PLUGIN_LIB_DIR),
			library
		]
		try:
			result = subprocess.run(cmd, check=True)
			if result.returncode == 0:
				self._installed_libs.add(library)
				return True
			self.logger.warning(i18n.get("pip_install_fail", library=library))
			return False
		except Exception:
			self.logger.warning(i18n.get("pip_install_fail", library=library))
			return False

	def _load_plugin(self, plugin_file: Path, plugin_name: str, meta: Optional[Dict]) -> Optional[PluginInfo]:
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
					version=meta.get(self.registry.VERSION, '0.0.0'),
					author=meta.get(self.registry.AUTHOR, ''),
					description=meta.get(self.registry.DESCRIPTION, ''),
					requirements=meta.get(self.registry.REQUIREMENTS, []),
					dependencies=meta.get(self.registry.DEPENDENCIES,[]),
				)
				self.registry.register(plugin_name, module, info)
				return info

		except Exception:
			sys.modules.pop(full_module_name, None)
			raise

		return None

	@staticmethod
	def _unload_library(library_name: str) -> None:
		target_dist_infos = [
			folder for folder in config.PLUGIN_LIB_DIR.iterdir()
			if folder.is_dir() and folder.name.lower().startswith(f"{library_name.lower()}-") and folder.name.endswith(".dist-info")
		]

		if not target_dist_infos:
			target_dist_infos = [
				folder for folder in config.PLUGIN_LIB_DIR.iterdir()
				if folder.is_dir() and folder.name.lower().startswith(f"{library_name.lower()}-") and folder.name.endswith(".egg-info")
			]

		if not target_dist_infos:
			return

		for dist_info in target_dist_infos:
			shutil.rmtree(dist_info)

			pkg_folder_name = dist_info.name.split('-')[0]
			pkg_folder = config.PLUGIN_LIB_DIR / pkg_folder_name

			if pkg_folder.exists() and pkg_folder.is_dir():
				shutil.rmtree(pkg_folder)

			cache_dir = config.PLUGIN_LIB_DIR / "__pycache__"
			if cache_dir.exists():
				for cache_file in cache_dir.glod(f"{pkg_folder_name}*.pyc"):
					cache_file.unlink()

	def _unload_plugin(self, plugin_name: str) -> None:
		if self.registry.get(plugin_name) is None:
			return None

		module = self.registry.get(plugin_name)
		self.registry.unregister(plugin_name)
		module_name = module.__name__
		for name in list(sys.modules):
			if name == module_name or name.startswith(module_name + '.'):
				sys.modules.pop(name, None)
		importlib.invalidate_caches()
		return None

	def _resolve(self, plugin_dir: Path) -> Optional[PluginInfo]:
		plugin_name = plugin_dir.name
		entry_point = self._get_plugin_entry_point(plugin_dir)

		if not entry_point:
			return None

		if self.registry.has(plugin_name):
			return self.registry.get_info(plugin_name)

		if plugin_name  in self._loading_stack:
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
		if str(config.PLUGIN_LIB_DIR) not in sys.path:
			sys.path.insert(0, str(config.PLUGIN_LIB_DIR))

		if str(config.PLUGIN_SCRIPT_DIR) not in sys.path:
			sys.path.insert(0, str(config.PLUGIN_SCRIPT_DIR))

		self.python_exe = shutil.which('python3') or shutil.which('python')
		try:
			ver_res = subprocess.run(
				[self.python_exe, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
				capture_output=True,
				text=True,
				check=True,
			)
			target_ver = tuple(map(int, ver_res.stdout.strip().split(".")))
			current_ver = sys.version_info[:2]
			if current_ver != target_ver:
				self.logger.warning(i18n.get("pm_python_version_mismatch", value=current_ver, req=target_ver))
		except Exception:
			pass

		if self.python_exe is None:
			self.logger.warning(i18n.get("pm_python_not_found"))

		loaded_infos = []
		plugin_dirs = [d for d in config.PLUGIN_SCRIPT_DIR.iterdir() if d.is_dir() and not d.name.startswith('.')]
		for plugin_dir in plugin_dirs:
			plugin_name = plugin_dir.name
			try:
				info = self._resolve(plugin_dir)
				if info:
					loaded_infos.append(info)
				else:
					self.logger.warning(i18n.get('pm_load_fail', plugin_file=plugin_dir))
			except Exception:
				format_exc = str(traceback.format_exc())
				target_path = str(config.PLUGIN_LIB_DIR)
				log_sys_path = f"sys.path count: {len(sys.path)}\n" + "\n".join([f"  - {p}" for p in sys.path])
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
					"\n---"
				]
				self.logger.error(i18n.get("pm_load_error", plugin_file=plugin_dir, error='\n'.join(errors)))
				self._unload_plugin(plugin_name)
			finally:
				self._loading_stack.discard(plugin_name)

		return loaded_infos

	def get(self, name: str) -> Optional[Any]:
		return self.registry.get(name)
