"""Verify saved Python settings and inert plugin discovery through public APIs."""

from __future__ import annotations

import codecs
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ish.manage.store import SettingsStore
from ish.plugin.manager import PluginManager
from ish.terminal.settings import Settings, load


class SettingsStoreTests(unittest.TestCase):
    """A save preserves user code and produces settings the next session can read."""

    def setUp(self):
        """Keep every settings file and executable fixture in an isolated directory."""
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.path = self.root / ".ishrc.py"

    def store(self, **options):
        """Open the same public loader used by the next actual terminal session."""
        return SettingsStore(self.path, settings_type=Settings, loader=load, **options)

    def test_opening_and_cancelling_missing_settings_creates_nothing(self):
        """Simply opening a manager is not a settings save or installation."""
        store = self.store()
        self.assertIsNone(store.error)
        self.assertIsNone(store.save({}))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_saved_appearance_is_available_to_help_without_rc_execution(self):
        """Help reads both selected styles without rerunning user Python."""
        marker = self.root / "rc-counter"
        self.path.write_text(
            "from pathlib import Path\n"
            f"with Path({str(marker)!r}).open('a') as stream:\n"
            "    stream.write('x')\n"
            "config.ui_palette = {'accent': 'blue-500'}\n"
            "config.ui_icons = 'unicode'\n"
            "config.color_depth = '4'\n"
        )
        self.assertEqual(
            SettingsStore.read_appearance(self.path),
            {"ui_palette": {"accent": "blue-500"}, "ui_icons": "unicode"},
        )
        self.assertFalse(marker.exists())
        store = self.store()
        self.assertIsNone(store.error)
        choice = {"selection": "violet-800", "accent": "#1a2b3c"}
        store.save({"ui_palette": choice, "ui_icons": "nerdfont"})
        self.assertEqual(
            SettingsStore.read_appearance(self.path),
            {"ui_palette": choice, "ui_icons": "nerdfont"},
        )
        self.assertEqual(marker.read_text(), "x")
        next_session = load(self.path)
        self.assertEqual(next_session.ui_palette, choice)
        self.assertEqual(next_session.ui_icons, "nerdfont")
        self.assertEqual(next_session.color_depth, "4")
        store.save({"ui_palette": {}})
        self.assertEqual(
            SettingsStore.read_appearance(self.path),
            {"ui_palette": {}, "ui_icons": "nerdfont"},
        )

    def test_help_palette_falls_back_for_unknown_or_unreadable_sources(self):
        """Skipped Python expressions and special files must not affect help."""
        self.assertEqual(SettingsStore.read_appearance(self.path), {})
        self.assertFalse(self.path.exists())
        for tail in (
            "config.ui_palette = choose_palette()",
            "config.ui_palette['accent'] = 'slate-800'",
            "if active: config.ui_palette = {}",
            "config.ui_palette = {'accent': 'madeup-400'}",
            "config.ui_palette = {'accent': '#1a2b3g'}",
            "config.ui_palette = {",
        ):
            with self.subTest(tail=tail):
                self.path.write_text(
                    "config.ui_palette = {'accent': 'blue-500'}\n" + tail
                )
                self.assertEqual(SettingsStore.read_appearance(self.path), {})
        self.path.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.path)
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from ish.manage.store import SettingsStore;"
                    "assert SettingsStore.read_appearance(sys.argv[1]) == {}",
                    str(self.path),
                ],
                timeout=15,
                check=True,
            )

    def test_unsupported_or_computed_icon_mode_keeps_the_literal_palette(self):
        """Each help preference falls back independently of the other one."""
        source = "config.ui_palette = {'accent': 'orange-300'}\nconfig.ui_icons = 'nerdfont'\n"
        for tail in (
            "config.ui_icons = 'unknown'",
            "if active: config.ui_icons = 'unicode'",
            "config.ui_icons = get_mode()",
        ):
            with self.subTest(tail=tail):
                self.path.write_text(source + tail)
                self.assertEqual(
                    SettingsStore.read_appearance(self.path),
                    {"ui_palette": {"accent": "orange-300"}},
                )
        self.path.write_text("config.ui_icons = 'unknown'\n")
        self.assertIn("ui_icons", str(self.store().error))
        self.path.write_text(source + "config.ui_palette.clear()")
        self.assertEqual(
            SettingsStore.read_appearance(self.path), {"ui_icons": "nerdfont"}
        )

    def test_save_reuses_validation_and_executes_rc_only_on_explicit_load(self):
        """The Python file runs once at load, never during validation or save."""
        counter = self.root / "count"
        source = (
            "# User comments and calculations stay intact.\n"
            "from pathlib import Path\n"
            f"counter = Path({str(counter)!r})\n"
            "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')\n"
            "config.shell = 'bash'\n"
        ).encode()
        self.path.write_bytes(source)
        store = self.store()
        self.assertIsNone(store.error)
        backup = store.save(
            {"lang": "ko", "plugins": ["chosen"], "features": {"hyperlinks": "off"}}
        )
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(backup.read_bytes(), source)
        self.assertTrue(self.path.read_bytes().startswith(source))
        selected = load(self.path)
        self.assertEqual(counter.read_text(), "2")
        self.assertEqual(
            (selected.lang, selected.plugins, selected.shell),
            ("ko", ["chosen"], "bash"),
        )
        self.assertEqual(selected.features["hyperlinks"], "off")

    def test_second_save_replaces_one_managed_block_and_keeps_prior_choices(self):
        """Reopening and changing a field never grows competing assignment blocks."""
        store = self.store()
        store.save({"lang": "ko", "plugins": ["one"]})
        reopened = self.store()
        self.assertIsNone(reopened.error)
        reopened.save({"lang": None})
        content = self.path.read_text()
        self.assertEqual(content.count("# >>> ish settings"), 1)
        self.assertEqual(content.count("# <<< ish settings"), 1)
        self.assertEqual(content.count("config.lang ="), 1)
        self.assertIsNone(load(self.path).lang)
        self.assertEqual(load(self.path).plugins, ["one"])

    def test_existing_encoding_newlines_and_bytes_are_preserved(self):
        """PEP 263 encodings and a BOM are preserved; new literals remain encodable."""
        samples = (
            b"# coding: latin-1\r\n# caf\xe9\r\nconfig.shell = 'bash'\r\n",
            codecs.BOM_UTF8 + "# 한글\r\nconfig.shell = 'zsh'\r\n".encode(),
        )
        for source in samples:
            with self.subTest(source=source):
                self.path.write_bytes(source)
                store = self.store()
                self.assertIsNone(store.error)
                store.save({"plugins": ["한글"]})
                saved = self.path.read_bytes()
                self.assertTrue(saved.startswith(source))
                self.assertNotIn(b"\n", saved[len(source) :].replace(b"\r\n", b""))
                self.assertEqual(load(self.path).plugins, ["한글"])

    def test_failed_validation_never_changes_file_or_creates_backup(self):
        """An unqueryable auto value fails before any persistent mutation."""
        self.path.write_text("config.lang = 'en'\n")
        before = self.path.read_bytes()
        store = self.store()
        with self.assertRaises(ValueError):
            store.save({"features": {"hyperlinks": "auto"}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_invalid_rc_can_be_opened_then_fixed_and_reloaded(self):
        """Broken Python is displayed as an error instead of being overwritten."""
        self.path.write_text("raise RuntimeError('fix me')\n")
        store = self.store()
        self.assertIsNotNone(store.error)
        self.assertEqual(store.settings.features["hyperlinks"], "on")
        self.assertEqual(store.settings.features["enhanced_keyboard"], "auto")
        with self.assertRaises(ValueError):
            store.save({"lang": "ko"})
        self.assertEqual(self.path.read_text(), "raise RuntimeError('fix me')\n")
        self.path.write_text("# Fixed in the editor\n")
        store.reload()
        self.assertIsNone(store.error)
        store.save({"lang": "ko"})
        self.assertEqual(load(self.path).lang, "ko")

    def test_rebinding_config_cannot_make_saved_choices_disappear(self):
        """The injected settings object remains the target of generated assignments."""
        source = "from types import SimpleNamespace\nconfig = SimpleNamespace()\n"
        self.path.write_text(source)
        store = self.store()
        self.assertIsNotNone(store.error)
        with self.assertRaises(ValueError):
            store.save({"lang": "ko"})
        self.assertEqual(self.path.read_text(), source)

    def test_changed_file_is_rejected_until_explicit_reload(self):
        """A file saved by another editor is never replaced using an old snapshot."""
        self.path.write_text("config.lang = 'en'\n")
        store = self.store()
        self.path.write_text("config.lang = 'ko'\n")
        with self.assertRaises(ValueError):
            store.save({"plugins": ["chosen"]})
        self.assertEqual(self.path.read_text(), "config.lang = 'ko'\n")
        store.reload()
        store.save({"plugins": ["chosen"]})
        self.assertEqual(load(self.path).lang, "ko")

    def test_external_edit_during_staging_is_not_overwritten(self):
        """The pre-publication check repeats after the new file has been flushed."""
        self.path.write_text("config.lang = 'en'\n")
        store = self.store()
        stage = store._stage

        def concurrent_edit(*args, **kwargs):
            staged = stage(*args, **kwargs)
            self.path.write_text("config.lang = 'ko'\n")
            return staged

        with (
            patch.object(store, "_stage", side_effect=concurrent_edit),
            self.assertRaises(ValueError),
        ):
            store.save({"plugins": ["chosen"]})
        self.assertEqual(self.path.read_text(), "config.lang = 'ko'\n")
        self.assertFalse(list(self.root.glob(".ish-settings-*")))

    def test_new_file_is_private_and_unknown_fields_cannot_be_injected(self):
        """Only declared settings become assignments in a newly created rc."""
        store = self.store()
        with self.assertRaises(ValueError):
            store.save({"unknown": "value"})
        self.assertFalse(self.path.exists())
        store.save({"lang": "ko"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_malformed_duplicate_or_middle_blocks_are_rejected(self):
        """Only the single complete tail block is owned by the manager."""
        cases = (
            "# >>> ish settings\nconfig.lang = 'en'\n",
            "# >>> ish settings\n# <<< ish settings\nconfig.lang = 'ko'\n",
            "# >>> ish settings\n# <<< ish settings\n# >>> ish settings\n# <<< ish settings\n",
            "# >>> ish settings\nprint('not a managed assignment')\n# <<< ish settings\n",
            "# >>> ish settings\nconfig.lang = 'en'\nconfig.lang = 'ko'\n# <<< ish settings\n",
            " # >>> ish settings\n # <<< ish settings\n",
        )
        for source in cases:
            with self.subTest(source=source):
                self.path.write_text(source)
                store = self.store()
                self.assertIsNotNone(store.error)
                with self.assertRaises(ValueError):
                    store.save({"lang": "ko"})
                self.assertEqual(self.path.read_text(), source)

    def test_marker_text_inside_a_string_is_not_owned(self):
        """A user's literal documentation is preserved as data, not a boundary."""
        source = "example = '''\n# >>> ish settings\n# <<< ish settings\n'''\n"
        self.path.write_text(source)
        store = self.store()
        self.assertIsNone(store.error)
        store.save({"lang": "ko"})
        self.assertTrue(self.path.read_text().startswith(source))
        self.assertEqual(load(self.path).lang, "ko")

    def test_backup_and_original_permissions_survive_an_atomic_save(self):
        """The retained backup is an exact private copy of the previous file."""
        self.path.write_text("# Original\n")
        self.path.chmod(0o640)
        store = self.store()
        backup = store.save({"lang": "ko"})
        self.assertEqual(backup.read_text(), "# Original\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)

    def test_replace_failure_preserves_original_and_does_not_publish_settings(self):
        """An operating-system write failure cannot leave a partial Python file."""
        self.path.write_text("config.lang = 'en'\n")
        store = self.store()
        with (
            patch("ish.manage.store.os.replace", side_effect=OSError("replace failed")),
            self.assertRaises(OSError),
        ):
            store.save({"lang": "ko"})
        self.assertEqual(self.path.read_text(), "config.lang = 'en'\n")
        self.assertEqual(store.settings.lang, "en")
        self.assertFalse(list(self.root.glob(".ish-settings-*")))

    def test_symlink_stays_link_and_retargeting_is_rejected(self):
        """The explicit target is updated without replacing or following a new link."""
        first, second = self.root / "first.py", self.root / "second.py"
        first.write_text("config.lang = 'en'\n")
        second.write_text("config.lang = 'en'\n")
        self.path.symlink_to(first)
        store = self.store()
        store.save({"lang": "ko"})
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(load(first).lang, "ko")
        self.path.unlink()
        self.path.symlink_to(second)
        with self.assertRaises(ValueError):
            store.save({"lang": None})
        self.assertEqual(second.read_text(), "config.lang = 'en'\n")

    def test_no_rc_does_not_execute_the_selected_python_file(self):
        """The explicit skip policy also applies to opening the settings manager."""
        marker = self.root / "executed"
        self.path.write_text(f"open({str(marker)!r}, 'w').write('executed')\n")
        store = self.store(no_rc=True)
        self.assertIsNone(store.error)
        store.save({"lang": "ko"})
        self.assertFalse(marker.exists())

    def test_no_rc_cannot_publish_syntactically_invalid_source(self):
        """Skipping execution never permits a partial or invalid Python save."""
        source = "unfinished =\n"
        self.path.write_text(source)
        store = self.store(no_rc=True)
        with self.assertRaises(ValueError):
            store.save({"lang": "ko"})
        self.assertEqual(self.path.read_text(), source)
        self.assertFalse(list(self.root.glob(".ishrc-backup-*")))


class DiscoveryTests(unittest.TestCase):
    """The public plugin catalogue reads metadata without taking loader ownership."""

    def setUp(self):
        """Create source and library paths without touching installed plugins."""
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.sources = self.root / "plugins"
        self.manager = PluginManager(
            script_dir=self.sources, lib_dir=self.root / "libraries"
        )

    def plugin(self, name, source):
        """Write a real editable package whose module must never execute here."""
        directory = self.sources / name
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text(source)
        return directory

    def test_missing_directory_creates_nothing(self):
        """Discovery alone does not install packages or create settings directories."""
        self.assertEqual(self.manager.discover(), ())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_metadata_listing_is_inert_and_does_not_claim_dependency_readiness(self):
        """Metadata and unknown requirements are reported without module imports."""
        marker = self.root / "executed"
        self.plugin(
            "chosen",
            "PLUGIN_META = {'version': '1.2', 'description': 'Useful', 'dependencies': ['missing-package'] }\n"
            + f"open({str(marker)!r}, 'w').write('executed')\n",
        )
        paths = list(sys.path)
        self.assertNotIn("chosen", sys.modules)
        candidates = self.manager.discover()
        self.assertEqual([item.name for item in candidates], ["chosen"])
        self.assertEqual(candidates[0].info.version, "1.2")
        self.assertEqual(candidates[0].info.dependencies, ["missing-package"])
        self.assertIsNone(candidates[0].info.module)
        self.assertIsNone(candidates[0].error)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "libraries").exists())
        self.assertEqual(sys.path, paths)
        self.assertNotIn("chosen", sys.modules)
        self.assertEqual(self.manager.registry.count(), 0)

    def test_invalid_and_missing_metadata_are_visible_but_hidden_packages_are_not(self):
        """One malformed candidate does not hide other plugin choices."""
        self.plugin("bad", "PLUGIN_META = get_metadata()\n")
        self.plugin("empty", "pass\n")
        self.plugin(".hidden", "PLUGIN_META = {'version': '1'}\n")
        self.plugin("good", "PLUGIN_META = {'version': '2'}\n")
        candidates = {item.name: item for item in self.manager.discover()}
        self.assertEqual(set(candidates), {"bad", "empty", "good"})
        self.assertIsNotNone(candidates["bad"].error)
        self.assertIsNotNone(candidates["empty"].error)
        self.assertIsNone(candidates["good"].error)


if __name__ == "__main__":
    unittest.main()
