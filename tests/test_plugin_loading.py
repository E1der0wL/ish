"""Check existing and explicitly selected plugin loading through actual imports."""

import os
import py_compile

from ish.lang import i18n
from ish.plugin.manager import PluginManager
from plugin_fixture import PluginFixture


class LoadingTests(PluginFixture):
    """Keep one shared loader while selecting which user code may run."""

    def test_korean_metadata_diagnostics_do_not_execute_invalid_plugin(self):
        """Actual loading translates its own error frame and keeps metadata inert."""
        previous = i18n.current_lang, i18n.base_path, i18n.message
        self.addCleanup(setattr, i18n, "current_lang", previous[0])
        self.addCleanup(setattr, i18n, "base_path", previous[1])
        self.addCleanup(setattr, i18n, "message", previous[2])
        i18n.base_path = self.root / "lang"
        i18n.load("ko", create=False)
        directory = self.plugin("fixture_invalid")
        entry = directory / "__init__.py"
        entry.write_text(entry.read_text().replace("'version': '1.0'", "'version': 10"))
        with self.assertLogs(self.manager.logger, level="DEBUG") as captured:
            self.manager.load(["fixture_invalid"])
        diagnostic = "\n".join(captured.output)
        self.assertIn("문자열이 필요합니다", diagnostic)
        self.assertIn("[디버깅 정보]", diagnostic)
        self.assertIn("[모듈 탐색 경로]", diagnostic)
        self.assertIn("PLUGIN_META.version", diagnostic)
        self.assertNotIn("[Debug Info]", diagnostic)
        self.assertIsNone(self.manager.get("fixture_invalid"))
        self.assertEqual(self.executed(), [])

    def test_selected_plugins_run_in_requested_order_once(self):
        """Unselected modules do not execute; duplicate selections reuse the module."""
        for name in ("fixture_first", "fixture_second", "fixture_unused"):
            self.plugin(name)
        loaded = self.manager.load(
            ["fixture_second", "fixture_first", "fixture_second"]
        )
        self.assertEqual(self.executed(), ["fixture_second", "fixture_first"])
        self.assertEqual(
            [info.name for info in loaded], ["fixture_second", "fixture_first"]
        )
        self.assertIsNone(self.manager.get("fixture_unused"))

    def test_empty_selection_does_not_execute_plugins(self):
        """An explicit empty list differs from the legacy all-directory policy."""
        self.plugin("fixture_unused")
        self.assertEqual(self.manager.load([]), [])
        self.assertEqual(self.executed(), [])

    def test_no_argument_load_preserves_legacy_directory_discovery(self):
        """Existing callers still load visible directories from current config."""
        for name in ("fixture_first", "fixture_second", ".fixture_hidden"):
            self.plugin(name)
        self.manager.load()
        self.assertCountEqual(self.executed(), ["fixture_first", "fixture_second"])

    def test_explicit_root_includes_required_plugins(self):
        """Dependencies resolve beside the selected plugin, never in global config."""
        selected = self.root / "selected"
        libraries = self.root / "libraries"
        libraries.mkdir()
        self.plugin("fixture_dependency", body="ORIGIN = 'global'\n")
        self.plugin("fixture_dependency", root=selected, body="ORIGIN = 'selected'\n")
        self.plugin(
            "fixture_main", root=selected, requirements=["fixture_dependency>=1"]
        )
        manager = PluginManager(script_dir=selected, lib_dir=libraries)
        manager.load(["fixture_main"])
        self.assertEqual(self.executed(), ["fixture_dependency", "fixture_main"])
        self.assertEqual(manager.get("fixture_dependency").ORIGIN, "selected")

    def test_failure_does_not_publish_module_or_stop_later_selection(self):
        """A real import failure leaves no registered module but preserves progress."""
        self.plugin("fixture_broken", body="raise RuntimeError('fixture failure')\n")
        self.plugin("fixture_later")
        with self.assertLogs(self.manager.logger, level="WARNING"):
            self.manager.load(["fixture_broken", "fixture_missing", "fixture_later"])
        self.assertIsNone(self.manager.get("fixture_broken"))
        self.assertIsNone(self.manager.get("fixture_missing"))
        self.assertIsNotNone(self.manager.get("fixture_later"))
        self.assertEqual(self.executed(), ["fixture_broken", "fixture_later"])

    def test_selected_names_cannot_leave_plugin_root(self):
        """A path-like selection cannot execute another directory's entry file."""
        self.plugin("outside", root=self.root)
        with self.assertLogs(self.manager.logger, level="WARNING"):
            self.manager.load(["../../outside", str(self.root / "outside")])
        self.assertEqual(self.executed(), [])

    def test_string_selection_is_rejected(self):
        """Accidental character-by-character loading is not a valid selection."""
        with self.assertRaises(TypeError):
            self.manager.load("fixture_name")

    def test_edited_entry_ignores_matching_timestamp_bytecode(self):
        """A new load reads edited entry bytes even when its cached metadata matches."""
        directory = self.plugin("fixture_edit", body="VALUE = 'first'\n")
        source = directory / "__init__.py"
        original = source.stat()
        py_compile.compile(str(source), doraise=True)
        source.write_text(source.read_text().replace("'first'", "'later'"))
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.manager.load(["fixture_edit"])
        self.assertEqual(self.manager.get("fixture_edit").VALUE, "later")
