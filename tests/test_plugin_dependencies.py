"""Reproduce partial installations and validate repair using isolated file fixtures."""

import csv
import importlib
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

from ish.plugin.dependencies import install_dependency
from ish.plugin.manager import PluginManager
from plugin_fixture import PluginFixture, distribution

NAME = "ish_fixture_package"
CHILD = "ish_fixture_child"


class DependencyTests(PluginFixture):
    """Exercise real metadata/import discovery and transactional file replacement."""

    def test_missing_recorded_files_are_not_available(self):
        """Metadata and __init__ alone cannot prove internal/data/native completeness."""
        for relative in ("internal.py", "data.txt", "native.so"):
            with self.subTest(relative=relative):
                distribution(
                    self.libs, NAME, files={f"{NAME}/{relative}": b"recorded data"}
                )
                (self.libs / NAME / relative).unlink()
                self.assertFalse(self.manager._is_library_available(NAME))

    def test_same_size_corruption_is_not_available(self):
        """A stale file with the correct length still fails its recorded digest."""
        distribution(self.libs, NAME, files={f"{NAME}/data.txt": b"original"})
        (self.libs / NAME / "data.txt").write_bytes(b"modified")
        self.assertFalse(self.manager._is_library_available(NAME))

    def test_missing_managed_record_is_not_available(self):
        """Managed installs without their file inventory cannot be accepted."""
        distribution(self.libs, NAME, record=False)
        self.assertFalse(self.manager._is_library_available(NAME))

    def test_missing_transitive_dependency_without_extras_is_not_available(self):
        """Ordinary Requires-Dist edges need validation even without extras."""
        distribution(self.libs, NAME, requires=[f"{CHILD}>=1"])
        self.assertFalse(self.manager._is_library_available(NAME))

    def test_transitive_corruption_is_not_available(self):
        """The root package cannot hide a missing file in an installed dependency."""
        distribution(self.libs, NAME, requires=[CHILD])
        distribution(self.libs, CHILD, files={f"{CHILD}/data.txt": b"needed"})
        (self.libs / CHILD / "data.txt").unlink()
        self.assertFalse(self.manager._is_library_available(NAME))

    def test_complete_tree_supports_markers_extras_and_cycles(self):
        """Inactive dependencies are skipped while extra and cyclic edges terminate."""
        distribution(
            self.libs,
            NAME,
            requires=[
                f'{CHILD}; extra == "ui"',
                'missing_fixture; python_version < "1"',
            ],
        )
        self.assertTrue(self.manager._is_library_available(NAME))
        self.assertFalse(self.manager._is_library_available(NAME + "[ui]"))
        distribution(self.libs, CHILD, requires=[NAME])
        self.assertTrue(self.manager._is_library_available(NAME + "[ui]"))

    def test_missing_generated_bytecode_is_harmless(self):
        """Optional interpreter caches do not force installation on every start."""
        info = distribution(self.libs, NAME)
        with (info / "RECORD").open("a", newline="") as output:
            csv.writer(output).writerow((f"{NAME}/__pycache__/__init__.pyc", "", ""))
        self.assertTrue(self.manager._is_library_available(NAME))

    def test_outside_record_paths_are_not_hashed(self):
        """Metadata cannot extend the validation read scope outside its library root."""
        info = distribution(self.libs, NAME)
        with (info / "RECORD").open("a", newline="") as output:
            csv.writer(output).writerow(("../../outside", "sha256=invalid", "999"))
        self.assertTrue(self.manager._is_library_available(NAME))

    def test_internal_symlink_escape_is_not_available(self):
        """An owned payload symlink cannot turn a missing file into an excluded path."""
        distribution(self.libs, NAME, files={f"{NAME}/data.txt": b"required"})
        path = self.libs / NAME / "data.txt"
        path.unlink()
        path.symlink_to(self.root / "missing-outside")
        self.assertFalse(self.manager._is_library_available(NAME))
        (self.root / "missing-outside").write_bytes(b"required")
        self.assertFalse(self.manager._is_library_available(NAME))

    def test_managed_record_requires_metadata_inventory(self):
        """A truncated inventory containing only its own entry is not complete."""
        info = distribution(self.libs, NAME, files={f"{NAME}/data.txt": b"required"})
        (self.libs / NAME / "data.txt").unlink()
        (info / "RECORD").write_text(f"{info.name}/RECORD,,\n")
        self.assertFalse(self.manager._is_library_available(NAME))

    def test_library_root_symlink_still_checks_payload(self):
        """An aliased library directory does not bypass its recorded file checks."""
        distribution(self.libs, NAME, files={f"{NAME}/data.txt": b"required"})
        alias = self.root / "library-alias"
        alias.symlink_to(self.libs, target_is_directory=True)
        sys.path.insert(0, str(alias))
        manager = PluginManager(lib_dir=alias)
        self.assertTrue(manager._is_library_available(NAME))
        (self.libs / NAME / "data.txt").unlink()
        self.assertFalse(manager._is_library_available(NAME))

    def test_external_legacy_distribution_without_record_remains_usable(self):
        """External metadata retains its previous metadata/import-only limitation."""
        external = self.root / "external"
        distribution(external, NAME, record=False)
        sys.path.insert(0, str(external))
        self.assertTrue(self.manager._is_library_available(NAME))

    def test_dotted_import_does_not_execute_parent_during_validation(self):
        """Post-install checking locates the requested child without running user code."""
        distribution(
            self.libs,
            NAME,
            files={
                f"{NAME}/__init__.py": b"raise RuntimeError('not for validation')\n",
                f"{NAME}/child.py": b"VALUE = 1\n",
            },
        )
        self.assertTrue(
            self.manager._is_library_available(
                NAME + "|" + NAME + ".child", import_parents=False
            )
        )

    def stage(self, contents):
        """Replace only the installer process with a deterministic offline producer."""

        def produce(command, *, check):
            self.assertTrue(check)
            self.assertIn("pip", command)
            target = Path(command[command.index("--target") + 1])
            shutil.copytree(contents, target)

        return patch("ish.plugin.dependencies.subprocess.run", side_effect=produce)

    def test_repair_restores_missing_data_before_plugin_executes(self):
        """The real manager and transaction repair files before dependent import."""
        distribution(self.libs, NAME, files={f"{NAME}/data.txt": b"usable"})
        (self.libs / NAME / "data.txt").unlink()
        incoming = self.root / "incoming"
        distribution(incoming, NAME, files={f"{NAME}/data.txt": b"usable"})
        self.plugin(
            "fixture_reader",
            dependencies=[NAME],
            body=f"import {NAME}\nPath({str(self.root / 'result')!r}).write_bytes((Path({NAME}.__file__).parent / 'data.txt').read_bytes())\n",
        )
        with self.stage(incoming) as installer:
            self.manager.load()
        self.assertEqual(installer.call_count, 1)
        self.assertEqual((self.root / "result").read_bytes(), b"usable")
        self.assertIsNotNone(self.manager.get("fixture_reader"))

    def test_failed_validation_rolls_back_and_preserves_unrelated_package(self):
        """Bad staged metadata cannot leave an upgraded or mixed installation behind."""
        distribution(self.libs, NAME, files={f"{NAME}/data.txt": b"old"})
        distribution(self.libs, CHILD)
        before = {
            p.relative_to(self.libs): p.read_bytes()
            for p in self.libs.rglob("*")
            if p.is_file()
        }
        incoming = self.root / "incoming"
        distribution(
            incoming,
            NAME,
            version="2.0",
            files={f"{NAME}/data.txt": b"new", f"{NAME}/required.txt": b"required"},
        )
        (incoming / NAME / "required.txt").unlink()
        self.manager.python_exe = sys.executable
        with (
            self.stage(incoming),
            self.assertLogs(self.manager.logger, level="WARNING"),
        ):
            self.assertFalse(self.manager._install_library(NAME + ">=2"))
        after = {
            p.relative_to(self.libs): p.read_bytes()
            for p in self.libs.rglob("*")
            if p.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(importlib.metadata.version(NAME), "1.0")

    def test_valid_upgrade_removes_obsolete_files(self):
        """Existing ownership-based promotion still removes old module data."""
        distribution(self.libs, NAME, files={f"{NAME}/obsolete.txt": b"old"})
        incoming = self.root / "incoming"
        distribution(incoming, NAME, version="2.0")
        with self.stage(incoming), install_dependency(sys.executable, NAME, self.libs):
            self.assertEqual(importlib.metadata.version(NAME), "2.0")
        self.assertFalse((self.libs / NAME / "obsolete.txt").exists())
        self.assertFalse((self.libs / f"{NAME}-1.0.dist-info").exists())
