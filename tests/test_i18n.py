"""Check language selection, read-only queries and safe custom translation fallback."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ish.lang.i18n import I18N, Message


class LanguageTests(unittest.TestCase):
    """Assert visible messages and independent files instead of loader internals."""

    def setUp(self):
        """Give each language manager an absent user settings directory."""
        self.directory = tempfile.TemporaryDirectory(prefix="ish-language-")
        self.root = Path(self.directory.name)
        self.language = I18N()
        self.language.base_path = self.root / "lang"

    def tearDown(self):
        """Remove only this test's isolated translations."""
        self.directory.cleanup()

    def write(self, language, values):
        """Write an independent user translation file."""
        self.language.base_path.mkdir(exist_ok=True)
        path = self.language.base_path / (language + ".json")
        path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        return path

    def test_korean_is_available_without_creating_settings(self):
        """Selecting Korean works before any user language file exists."""
        self.language.load("ko", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        self.assertIn("셸", self.language.get(Message.STREAM_DESCRIPTION))
        self.assertEqual(
            self.language.get(Message.STREAM_SHELL_ERROR, shell="missing-shell"),
            "셸 실행 파일을 찾을 수 없습니다: missing-shell",
        )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_korean_regions_use_shipped_language(self):
        """A regional code retains its own user filename and Korean base messages."""
        self.write("ko_KR", {"stream_description": "사용자 한국어 설명"})
        self.language.load("ko_KR", create=False)
        self.assertEqual(self.language.current_lang, "ko_KR")
        self.assertEqual(
            self.language.get(Message.STREAM_DESCRIPTION), "사용자 한국어 설명"
        )
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        self.language.load("ko-KR", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")

    def test_generated_defaults_do_not_hide_korean_on_next_start(self):
        """An automatically created English file remains a fallback on later starts."""
        self.language.load("ko", create=True)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        self.assertTrue((self.language.base_path / "en.json").is_file())
        self.language.load("ko", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        self.assertEqual(
            self.language.get(
                Message.PM_LOAD_ERROR, plugin_file="sample.py", error="broken"
            ),
            "플러그인 sample.py을 불러오는 중 오류가 발생했습니다:\r\nbroken",
        )

    def test_user_language_overrides_shipped_translation_and_keeps_fallback(self):
        """Selected user text wins while untranslated extension keys retain English fallback."""
        self.write(
            "en",
            {"custom_message": "English {value}", "cli_description": "User fallback"},
        )
        self.write("ko", {"stream_description": "내 설명"})
        self.language.load("ko", create=False)
        self.assertEqual(self.language.get(Message.STREAM_DESCRIPTION), "내 설명")
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        self.assertEqual(self.language.get(Message.CLI_DESCRIPTION), "User fallback")
        self.assertEqual(
            self.language.get("custom_message", value="data"), "English data"
        )

    def test_language_switch_drops_previous_translation(self):
        """A new language starts from its own fallback instead of stale UI strings."""
        self.language.load("ko", create=False)
        self.language.load("en", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "options")
        self.assertEqual(
            self.language.get(Message.STREAM_VERSION, version="1.2.1"),
            "ish-stream 1.2.1",
        )

    def test_unknown_language_uses_english_without_creating_files(self):
        """An absent language file is an ordinary English fallback."""
        self.language.load("zz", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "options")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_language_name_cannot_read_outside_its_directory(self):
        """A language option is a code, not a path to another JSON file."""
        self.language.base_path.mkdir()
        (self.root / "outside.json").write_text('{"cli_options_title": "OUTSIDE"}')
        with self.assertRaises(ValueError):
            self.language.load("../outside", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "options")

    def test_invalid_language_names_are_rejected_without_files(self):
        """Empty, absolute, traversal and malformed language names are not accepted."""
        for name in (
            "",
            ".",
            "..",
            "/tmp/ko",
            "ko/../en",
            r"..\ko",
            "ko\x00",
            "ko.json",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.language.load(name, create=False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_default_file_does_not_save_previous_language_messages(self):
        """Creating English defaults never persists stale runtime overrides."""
        self.language.update({"cli_options_title": "STALE"})
        self.language.load("en", create=True)
        saved = json.loads((self.language.base_path / "en.json").read_text())
        self.assertEqual(saved["cli_options_title"], "options")
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "options")

    def test_existing_user_file_is_preserved(self):
        """Loading in creation mode does not overwrite a user's valid file."""
        path = self.write("en", {"cli_options_title": "My options"})
        original = path.read_bytes()
        self.language.load("en", create=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "My options")

    def test_bad_known_placeholders_keep_a_working_translation(self):
        """A malformed user template cannot hide required diagnostic values."""
        for template in (
            "버전 {oops}",
            "버전",
            "버전 {version",
            "버전 {version.secret}",
        ):
            with self.subTest(template=template):
                self.write("ko", {"cli_version": template})
                with self.assertLogs("global", level="WARNING"):
                    self.language.load("ko", create=False)
                self.assertEqual(
                    self.language.get(Message.CLI_VERSION, version="1.2.1"), "ish 1.2.1"
                )

    def test_bad_format_conversion_uses_english_instead_of_raw_placeholder(self):
        """Formatting failures retain a usable known message and its supplied value."""
        self.write("ko", {"cli_version": "버전 {version:d}"})
        self.language.load("ko", create=False)
        self.assertEqual(
            self.language.get(Message.CLI_VERSION, version="1.2.1"), "ish 1.2.1"
        )

    def test_invalid_json_or_value_does_not_replace_shipped_messages(self):
        """A broken user file is ignored without disabling the shipped locale."""
        path = self.write("ko", {"cli_options_title": 7})
        with self.assertLogs("global", level="WARNING"):
            self.language.load("ko", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        path.write_text("{", encoding="utf-8")
        with self.assertLogs("global", level="WARNING"):
            self.language.load("ko", create=False)
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")

    def test_dynamic_updates_keep_unknown_keys_and_validate_known_contracts(self):
        """Plugins retain custom message keys; known parameters remain consistent."""
        self.language.update({"custom": "Value: {item}"})
        self.assertEqual(self.language.get("custom", item="x"), "Value: x")
        with self.assertRaises(ValueError):
            self.language.update({"cli_version": "Version without its value"})
        self.assertEqual(
            self.language.get(Message.CLI_VERSION, version="2.0"), "ish 2.0"
        )

    def test_missing_arguments_and_unknown_keys_retain_existing_behavior(self):
        """Callers missing format arguments still receive the template or key."""
        self.assertEqual(self.language.get(Message.CLI_VERSION), "ish {version}")
        self.assertEqual(self.language.get("not_registered"), "not_registered")


if __name__ == "__main__":
    unittest.main()
