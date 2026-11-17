"""Check language selection, read-only queries and safe custom translation fallback."""

from __future__ import annotations

import ast
import json
import locale
import logging
import os
import tempfile
import unittest
from pathlib import Path
from string import Formatter
from unittest.mock import patch

from ish.lang.i18n import I18N, Message

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_SOURCES = (
    "src/ish/stream",
    "src/ish/log.py",
    "src/ish/lang/i18n.py",
    "src/ish/plugin/manager.py",
    "src/ish/plugin/dependencies.py",
    "src/ish/runtime/diagnostics.py",
    "src/ish/runtime/startup.py",
    "src/ish/runtime/fdio.py",
    "src/ish/terminal",
    "examples/stream_plugins/stream_bar/stream_bar.py",
    "examples/stream_plugins/stream_popup/stream_popup.py",
)


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

    def test_lang_is_detected_without_installed_system_locales(self):
        """POSIX locale spellings select shipped text without consulting libc locales."""
        for value in ("ko", "ko_KR.UTF-8", "ko_KR@modifier", "KO-kr.utf8@variant"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"LANG": value}, clear=True),
                patch.object(locale, "getdefaultlocale", side_effect=AssertionError),
                patch.object(locale, "getlocale", side_effect=AssertionError),
                patch.object(locale, "setlocale", side_effect=AssertionError),
            ):
                self.language.load(create=False)
                self.assertEqual(self.language.current_lang, "ko")
                self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_lc_overrides_do_not_replace_the_product_lang_policy(self):
        """Only LANG supplies automatic selection; LC_* and LANGUAGE stay native."""
        for environment, expected, text in (
            (
                {"LANG": "ko_KR.UTF-8", "LC_ALL": "C", "LC_MESSAGES": "en_US"},
                "ko",
                "옵션",
            ),
            (
                {"LANG": "en_US.UTF-8", "LC_ALL": "ko_KR", "LANGUAGE": "ko"},
                "en",
                "options",
            ),
            ({"LC_MESSAGES": "ko_KR.UTF-8", "LANGUAGE": "ko"}, "en", "options"),
        ):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
            ):
                self.language.load(create=False)
                self.assertEqual(self.language.current_lang, expected)
                self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), text)

    def test_missing_c_and_malformed_lang_values_fall_back_to_english(self):
        """Bad automatic environment data is not a fatal user option error."""
        for value in (
            None,
            "",
            "C",
            "C.UTF-8",
            "POSIX",
            "POSIX.UTF-8",
            "../ko",
            "ko/../en",
            "ko_KR@",
            "ko..UTF-8",
            "ko.UTF-8/x",
            "ko\n",
            "한국어",
            "english",
        ):
            environment = {} if value is None else {"LANG": value}
            with (
                self.subTest(value=value),
                patch.dict(os.environ, environment, clear=True),
            ):
                self.language.load(create=False)
                self.assertEqual(self.language.current_lang, "en")
                self.assertEqual(
                    self.language.get(Message.CLI_OPTIONS_TITLE), "options"
                )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_each_automatic_load_detects_lang_again(self):
        """An earlier explicit or automatic choice cannot stick in the singleton."""
        self.language.load("ko", create=False)
        for value, expected in (
            ("en_US.UTF-8", "options"),
            ("ko_KR", "옵션"),
            ("", "options"),
        ):
            with patch.dict(os.environ, {"LANG": value}, clear=True):
                self.language.load(create=False)
                self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), expected)
        with patch.dict(os.environ, {"LANG": "ko_KR.UTF-8"}, clear=True):
            self.language.load("en", create=False)
            self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "options")

    def test_partial_user_language_keeps_english_for_missing_messages(self):
        """A local language pack need not duplicate the entire English catalog."""
        self.write("zz", {"cli_options_title": "LOCAL OPTIONS"})
        with patch.dict(os.environ, {"LANG": "zz_ZZ.UTF-8"}, clear=True):
            self.language.load(create=False)
        self.assertEqual(self.language.current_lang, "zz")
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "LOCAL OPTIONS")
        self.assertEqual(
            self.language.get(Message.STREAM_SHELL_ERROR, shell="missing"),
            "Shell executable not found: missing",
        )

    def test_terminal_native_failures_use_the_selected_language(self):
        """Construction errors translate without requiring a native library."""
        from types import SimpleNamespace

        from ish.terminal.vt import Terminal, TerminalError

        self.language.load("ko", create=False)
        library = SimpleNamespace(_ish_engine=None, ish_vt_new=lambda *_: 0)
        with (
            patch("ish.terminal.vt.i18n", self.language),
            patch("ish.terminal.vt._load", return_value=library),
        ):
            with self.assertRaisesRegex(
                ValueError, "터미널 크기가 262144셀 한도를 초과"
            ):
                Terminal(262145, 1, lambda _: None)
            with self.assertRaisesRegex(TerminalError, "터미널을 할당할 수 없습니다"):
                Terminal(10, 4, lambda _: None)

    def test_terminal_diagnostics_omit_host_and_decoder_error_text(self):
        """Normal logs use trusted translations, not child-controlled strings."""
        from types import SimpleNamespace

        from ish.terminal.diagnostics import report_session

        self.language.load("ko", create=False)
        application = SimpleNamespace(
            vt=SimpleNamespace(
                framing_drops=0, protocol_failures=(), image_failure_count=2
            ),
            app=SimpleNamespace(
                renderer=SimpleNamespace(
                    graphics_error="PRIVATE_HOST_REPLY", graphics_failure_count=3
                )
            ),
            host=SimpleNamespace(
                feature_status={
                    "hyperlinks": SimpleNamespace(
                        policy="off",
                        support="unqueried",
                        active=False,
                        reason="policy-off",
                    )
                },
                dropped_controls=4,
                graphics_reason="PRIVATE_HOST_REASON",
                profile=SimpleNamespace(image_protocol=None),
            ),
            clipboard=SimpleNamespace(failures=5),
        )
        logger = logging.getLogger("vt-diagnostics-test")
        with (
            patch("ish.lang.i18n", self.language),
            self.assertLogs(logger, level="INFO") as captured,
        ):
            report_session(application, logger)
        output = "\n".join(captured.output)
        self.assertIn("이미지 입력 거부: 오류 2개(본문 제외)", output)
        self.assertIn("이미지 출력 실패: 오류 3개(호스트 응답 및 본문 제외)", output)
        self.assertIn(
            "지원하는 호스트 이미지 출력 프로토콜을 확인하지 못했습니다", output
        )
        self.assertNotIn("PRIVATE_HOST_REPLY", output)
        self.assertNotIn("PRIVATE_HOST_REASON", output)

    def test_current_product_messages_have_complete_korean_contracts(self):
        """Consumer references require Korean entries with the same named fields."""
        catalog = json.loads(
            (ROOT / "src/ish/lang/locales/ko.json").read_text(encoding="utf-8")
        )
        required = set()
        for relative in PRODUCT_SOURCES:
            path = ROOT / relative
            self.assertTrue(path.exists(), relative)
            sources = sorted(path.rglob("*.py")) if path.is_dir() else (path,)
            for source in sources:
                tree = ast.parse(source.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "Message"
                    ):
                        required.add(node.attr)
        self.assertIn("STREAM_SHELL_ERROR", required)
        self.assertIn("I18N_LOAD_ERROR", required)
        self.assertGreater(len(required), 80)
        formatter = Formatter()
        for name in sorted(required):
            with self.subTest(message=name):
                key = name.lower()
                self.assertIn(key, catalog)
                self.assertIsInstance(catalog[key], str)
                self.assertTrue(catalog[key])
                fields = {
                    field
                    for _, field, _, _ in formatter.parse(catalog[key])
                    if field is not None
                }
                expected = {
                    field
                    for _, field, _, _ in formatter.parse(Message[name].english)
                    if field is not None
                }
                self.assertEqual(fields, expected)

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
            self.language.get(Message.STREAM_VERSION, version="2.0.0"),
            "ish 2.0.0",
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

    def test_load_failures_use_the_selected_language_not_the_previous_one(self):
        """Even an early English fallback-file failure is reported in the new locale."""
        path = self.write("en", {"cli_options_title": 7})
        for selected, expected in (
            (
                "ko",
                f"언어 파일 {path}을 불러올 수 없습니다: 언어 메시지는 문자열 키와 문자열 값으로 구성되어야 합니다.",
            ),
            (
                "en",
                f"Cannot load language file {path}: Language messages must map strings to strings",
            ),
        ):
            with (
                self.subTest(selected=selected),
                self.assertLogs("global", level="WARNING") as captured,
            ):
                self.language.load(selected, create=False)
            self.assertEqual(
                [record.getMessage() for record in captured.records], [expected]
            )

    def test_user_language_can_override_the_entire_failure_message(self):
        """Loader diagnostics are public translation keys, including their owned reason."""
        failed = self.write("en", {"cli_version": "missing field"})
        self.write(
            "ko",
            {
                "i18n_load_error": "LOCAL {path} :: {error}",
                "i18n_fields_error": "FIELD {name}",
            },
        )
        with self.assertLogs("global", level="WARNING") as captured:
            self.language.load("ko", create=False)
        self.assertEqual(
            captured.records[0].getMessage(), f"LOCAL {failed} :: FIELD cli_version"
        )

    def test_create_failure_uses_selected_language_and_supplied_logger(self):
        """A failed optional file creation preserves shipped text and the caller's log sink."""
        self.language.base_path.write_text("not a directory", encoding="utf-8")
        logger = logging.getLogger("language-test")
        with (
            self.assertLogs(logger, level="WARNING") as captured,
            self.assertNoLogs("global"),
        ):
            self.language.load("ko", create=True, logger=logger)
        self.assertEqual(len(captured.records), 1)
        self.assertTrue(
            captured.records[0]
            .getMessage()
            .startswith(
                f"언어 파일 {self.language.base_path / 'en.json'}을 만들 수 없습니다:"
            )
        )
        self.assertEqual(self.language.get(Message.CLI_OPTIONS_TITLE), "옵션")
        self.assertEqual(self.language.base_path.read_text(), "not a directory")

    def test_translation_validation_errors_use_the_active_language(self):
        """Public updates reject bad text atomically with translated user errors."""
        self.language.load("ko", create=False)
        with self.assertRaisesRegex(
            ValueError, "번역의 치환 항목이 cli_version과 일치하지 않습니다"
        ):
            self.language.update({"cli_version": "missing"})
        with self.assertRaisesRegex(
            ValueError, "언어 메시지는 문자열 키와 문자열 값으로 구성되어야 합니다"
        ):
            self.language.message = {"cli_options_title": 7}
        with self.assertRaisesRegex(
            ValueError, "번역의 치환 항목이 cli_version과 일치하지 않습니다"
        ):
            self.language.message["cli_version"] = "missing"
        with self.assertRaisesRegex(
            ValueError, "언어 메시지는 문자열 키와 문자열 값으로 구성되어야 합니다"
        ):
            self.language.message.update({"cli_options_title": "INVALID", "other": 7})
        with self.assertRaisesRegex(
            ValueError, "언어는 en 또는 ko와 같은 단일 언어 코드여야 합니다"
        ):
            self.language.load("../outside", create=False)
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
