"""Isolate public startup settings, imports and native subprocess fixtures."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


class StartupFixture:
    """Share observable startup helpers without inheriting another test suite."""

    ROOT = Path(__file__).resolve().parents[1]
    DEADLINE = 30

    def setUp(self):
        """Isolate settings, executable traps and all prospective output paths."""
        self.directory = tempfile.TemporaryDirectory(prefix="ish-startup-")
        self.root = Path(self.directory.name)
        self.home = self.root / "absent-settings"
        self.native_home = self.root / "native-home"
        self.native_home.mkdir()
        self.env = dict(os.environ)
        for key in ("ZDOTDIR", "BASH_ENV", "ENV", "ISH_BUNDLE_ROOT"):
            self.env.pop(key, None)
        self.env.update(
            HOME=str(self.native_home),
            LANG="C.UTF-8",
            LC_ALL="C.UTF-8",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=str(self.ROOT / "src"),
        )

    def tearDown(self):
        """Remove only this test's isolated files."""
        self.directory.cleanup()

    def cli(self, *args, module="ish"):
        """Run the public module without a TTY, giving timeout no production meaning."""
        return subprocess.run(
            [sys.executable, "-m", module, "--home", str(self.home), *map(str, args)],
            env=self.env,
            cwd=self.root,
            input="",
            text=True,
            capture_output=True,
            timeout=self.DEADLINE,
            check=False,
        )

    def files(self):
        """Record independent file contents and directory names for write detection."""
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            if path.is_file()
            else None
            for path in self.root.rglob("*")
        }

    def field(self, output, name):
        """Read a displayed diagnostic value by its documented human-readable label."""
        values = [
            line.partition(": ")[2]
            for line in output.splitlines()
            if line.startswith(name + ": ")
        ]
        self.assertEqual(len(values), 1, output)
        return values[0]

    def write_language(self, values):
        """Create a user override using the shared settings layout."""
        directory = self.home / "lang"
        directory.mkdir(parents=True)
        (directory / "ko.json").write_text(json.dumps(values), encoding="utf-8")

    def trap_plugin(self):
        """Make plugin import observable even when no registration function is called."""
        directory = self.root / "plugins" / "selected"
        directory.mkdir(parents=True)
        marker = self.root / "plugin-executed"
        (directory / "selected.py").write_text(
            "PLUGIN_META = {'name': 'selected', 'version': '1.0'}\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "raise RuntimeError('plugin executed during information query')\n",
            encoding="utf-8",
        )
        return directory.parent

    def trap_shell(self):
        """Create an executable whose invocation leaves a file rather than a shell UI."""
        shell = self.root / "shell-trap"
        shell.write_text(
            "#!/bin/sh\n"
            f"printf executed > {shlex.quote(str(self.root / 'shell-executed'))}\n",
            encoding="utf-8",
        )
        shell.chmod(0o700)
        return shell

    def write_rc(self, text):
        """Persist user choices in the public rc file."""
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".ishrc.py").write_text(text, encoding="utf-8")

    def recording_plugin(self, name):
        """Make actual PluginManager import, setup and cleanup independently visible."""
        directory = self.home / "plugins" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.py").write_text(
            "from pathlib import Path\n"
            "PLUGIN_META = {'version':'1', 'requirements':[], 'dependencies':[]}\n"
            "TERMINAL_API = 1\n"
            f"target = Path({str(self.root / (name + '-runs'))!r})\n"
            "def record(text):\n"
            "    with target.open('a') as file: file.write(text + '\\n')\n"
            "record('import')\n"
            "def setup(api):\n"
            "    record('setup')\n"
            "    return lambda: record('cleanup')\n",
            encoding="utf-8",
        )
