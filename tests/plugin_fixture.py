"""Create isolated plugin trees and wheel-style distribution records for tests."""

import base64
import csv
import hashlib
import importlib
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from ish.config import config
from ish.plugin.manager import PluginManager


def distribution(root, name, *, version="1.0", files=None, requires=(), record=True):
    """Write independent installed metadata and exact recorded file checksums."""
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {value}\n" for value in requires)
    info = f"{name}-{version}.dist-info"
    contents = {
        f"{name}/__init__.py": b"VALUE = 'complete'\n",
        f"{info}/METADATA": metadata.encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        **(files or {}),
    }
    rows = []
    for relative, data in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append((relative, "sha256=" + digest.decode(), len(data)))
    if record:
        with (root / info / "RECORD").open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerows(rows)
            writer.writerow((f"{info}/RECORD", "", ""))
    importlib.invalidate_caches()
    return root / info


class PluginFixture(TestCase):
    """Keep configuration, imports and all generated files inside one fixture."""

    def setUp(self):
        """Provide the same no-argument manager setup as the legacy CLI."""
        self.temporary = tempfile.TemporaryDirectory(prefix="ish-plugin-test-")
        self.root = Path(self.temporary.name)
        self.config_patch = patch.object(config, "_ish_home", self.root)
        self.config_patch.start()
        self.scripts, self.libs = config.PLUGIN_SCRIPT_DIR, config.PLUGIN_LIB_DIR
        self.scripts.mkdir(parents=True)
        self.libs.mkdir(parents=True)
        self.paths = sys.path.copy()
        sys.path[:0] = [str(self.scripts), str(self.libs)]
        self.manager = PluginManager()

    def tearDown(self):
        """Remove only fixture imports before discarding their source files."""
        for name, module in tuple(sys.modules.items()):
            if module is not None:
                origin = module.__dict__.get("__file__")
                if origin and Path(origin).resolve().is_relative_to(self.root):
                    del sys.modules[name]
        sys.path[:] = self.paths
        importlib.invalidate_caches()
        self.config_patch.stop()
        self.temporary.cleanup()

    def plugin(self, name, *, root=None, requirements=(), dependencies=(), body=""):
        """Create a plugin whose import leaves a separately observable file."""
        directory = (root or self.scripts) / name
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "version": "1.0",
            "requirements": list(requirements),
            "dependencies": list(dependencies),
        }
        (directory / "__init__.py").write_text(
            f"PLUGIN_META = {meta!r}\n"
            "from pathlib import Path\n"
            f"with Path({str(self.root / 'executed')!r}).open('a') as record:\n"
            f"    record.write({name!r} + '\\n')\n" + body,
            encoding="utf-8",
        )
        return directory

    def executed(self):
        """Read import effects rather than relying on the manager's bookkeeping."""
        path = self.root / "executed"
        return path.read_text().splitlines() if path.exists() else []
