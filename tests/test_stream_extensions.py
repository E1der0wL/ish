"""Verify external Python plugins through the public CLI and native PTY results."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stream_fixture import RAW_OUTPUT
from stream_plugins import copy_plugins
from test_stream_cli import BAR, F2, PROMPT, StreamProcess

METADATA = "PLUGIN_META = {'version': '1.0.0'}\nSTREAM_API = 1\n"


@unittest.skipUnless(shutil.which("tcsh"), "tcsh is required")
class ExtensionTests(unittest.TestCase):
    """Observe files, terminal output and actual popup/native input consumers."""

    def setUp(self):
        """Copy editable Python sources into a separate user plugin directory."""
        self.temporary = tempfile.TemporaryDirectory(prefix="ish-plugin-cli-")
        self.root = Path(self.temporary.name)
        self.plugins = self.root / "plugins"
        copy_plugins(self.plugins)
        self.processes = []

    def tearDown(self):
        """Stop only the CLI sessions owned by this test."""
        for process in self.processes:
            process.stop()
        self.temporary.cleanup()

    def plugin(self, name, source):
        """Create a real module consumed by the existing PluginManager."""
        directory = self.plugins / name
        directory.mkdir(exist_ok=True)
        (directory / (name + ".py")).write_text(METADATA + source)

    def start(self, **options):
        """Wait for a native prompt; no private activation functions are called."""
        process = StreamProcess(self.root, plugin_dir=self.plugins, **options)
        self.processes.append(process)
        process.expect(PROMPT)
        return process

    def exit(self, process):
        """Wait for public process completion, including plugin resource cleanup."""
        process.send(b"exit\r")
        process.wait(lambda: process.poll() is not None, "CLI teardown")
        process.read()
        self.assertEqual(process.status, 0)

    def test_plain_mode_never_loads_available_plugins(self):
        """A plugin path alone cannot enable hooks, UI or Python execution."""
        self.plugin(
            "unused",
            "from pathlib import Path\n(Path(__file__).resolve().parents[2] / 'unwanted').touch()\n"
            "def setup(api): raise RuntimeError('should not run')\n",
        )
        process = self.start()
        process.command("echo plain > result")
        process.expect_file(self.root / "result", "plain\n")
        self.assertFalse((self.root / "unwanted").exists())
        self.assertNotIn(BAR, process.output)
        self.assertNotIn(b"\x1b]777;ish;", process.output)
        self.exit(process)

    def test_selected_bar_uses_existing_manager_without_loading_other_plugins(self):
        """A named external module supplies its own formatted text."""
        self.plugin(
            "custom",
            "def setup(api): api.set_bar(lambda: [('fg:ansigreen', 'EXTERNAL BAR')])\n",
        )
        self.plugin("unused", "raise RuntimeError('unselected module executed')\n")
        process = self.start(plugins=("custom", "stream_tcsh"))
        process.expect(b"EXTERNAL BAR")
        process.command("echo native > result")
        process.expect_file(self.root / "result", "native\n")
        self.assertNotIn(b"unselected module executed", process.output)
        self.exit(process)

    def test_edited_popup_consumes_input_and_returns_native_suffix(self):
        """The external view changes while its accepted text never becomes a command."""
        entry = self.plugins / "stream_popup/stream_popup.py"
        entry.write_text(
            entry.read_text().replace("Message.STREAM_SAMPLE_POPUP", "'EXTERNAL POPUP'")
        )
        process = self.start(demo=True)
        process.expect(BAR)
        process.send(F2)
        process.expect(b"EXTERNAL POPUP")
        process.send(b"touch popup-leak\recho once >> result\r")
        process.expect_file(self.root / "result", "once\n")
        process.command("echo complete > complete")
        process.expect_file(self.root / "complete", "complete\n")
        self.assertFalse((self.root / "popup-leak").exists())
        self.assertEqual((self.root / "result").read_text(), "once\n")
        self.exit(process)

    def test_korean_sample_and_logs_preserve_native_output_and_input(self):
        """Localized UI owns only popup input; native bytes and files stay exact."""
        log = self.root / "korean.log"
        process = self.start(
            demo=True,
            extra_args=("--lang", "ko", "--log-file", str(log), "--log-level", "DEBUG"),
        )
        # The fixture's demo flag only controls its English-bar wait heuristic.
        process.demo = False
        process.expect("ish | 기본 셸 | F2 팝업".encode())
        process.send(F2)
        process.expect("플러그인 팝업 | Enter 확인 | Esc 취소".encode())
        process.send(b"touch popup-leak\recho native >> result\r")
        process.expect_file(self.root / "result", "native\n")
        offset = process.fixture("output")
        process.expect(b"STREAM_RAW_DONE", offset)
        self.assertIn(RAW_OUTPUT, bytes(process.output[offset:]))
        process.command("echo complete > complete")
        process.expect_file(self.root / "complete", "complete\n")
        self.assertFalse((self.root / "popup-leak").exists())
        self.exit(process)
        diagnostic = log.read_text()
        for text in (
            "세션 시작 shell=",
            "셸 이벤트 kind=P",
            "팝업 focus=True",
            "팝업 focus=False",
            "세션 종료 status=0",
        ):
            self.assertIn(text, diagnostic)
        self.assertNotIn("Shell event kind=", diagnostic)

    def test_korean_plugin_error_falls_back_to_native_shell(self):
        """An incompatible selected plugin reports a translated startup error."""
        self.plugin("bad", "STREAM_API = 999\ndef setup(api): raise AssertionError()\n")
        process = self.start(
            plugins=("stream_bar", "stream_tcsh", "bad"),
            extra_args=("--lang", "ko"),
        )
        process.expect("지원하지 않는 플러그인 API입니다: bad".encode())
        process.expect("UI 없이 셸을 시작합니다.".encode())
        process.command("echo recovered > result")
        process.expect_file(self.root / "result", "recovered\n")
        self.assertNotIn("기본 셸 | F2 팝업".encode(), process.output)
        self.exit(process)

    def test_cleanup_runs_in_reverse_order_after_native_exit(self):
        """Selected plugins can release their own resources with sync or async cleanup."""
        for name, asynchronous in (("first", False), ("second", True)):
            prefix = "async " if asynchronous else ""
            self.plugin(
                name,
                "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n"
                f"{prefix}def cleanup():\n"
                f"    with (ROOT / 'cleanup').open('a') as file: file.write('{name}\\n')\n"
                "def setup(api): return cleanup\n",
            )
        process = self.start(plugins=("first", "second"))
        self.assertNotIn(BAR, process.output)
        self.exit(process)
        self.assertEqual((self.root / "cleanup").read_text(), "second\nfirst\n")

    def test_failed_setup_releases_prior_resources_and_starts_shell_once(self):
        """Preparation failure falls back before native startup, not after a command."""
        self.plugin(
            "first",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n"
            "def setup(api): return lambda: (ROOT / 'released').touch()\n",
        )
        self.plugin("bad", "def setup(api): raise RuntimeError('setup failed')\n")
        process = self.start(plugins=("first", "bad"), startup="echo once >> starts\n")
        process.expect(b"starting without UI")
        process.expect_file(self.root / "starts", "once\n")
        self.assertTrue((self.root / "released").exists())
        process.command("echo recovered > result")
        process.expect_file(self.root / "result", "recovered\n")
        self.exit(process)

    def test_incompatible_api_and_ambiguous_shell_fall_back(self):
        """Unusable registrations never install a speculative shell integration."""
        cases = {
            "wrong_api": "STREAM_API = 999\ndef setup(api): raise AssertionError()\n",
            "ambiguous": "def setup(api): api.add_shell(lambda argv: True, lambda *a, **k: None)\n",
        }
        for name, source in cases.items():
            with self.subTest(name=name):
                self.plugin(name, source)
                process = self.start(plugins=("stream_bar", "stream_tcsh", name))
                process.expect(b"starting without UI")
                self.assertNotIn(BAR, process.output)
                self.exit(process)

    def test_popup_factory_error_discards_pending_text_and_releases_shell(self):
        """A failing view cannot leave its native widget blocked or execute popup text."""
        self.plugin(
            "broken",
            "def create(accept): raise RuntimeError('view failed')\n"
            "def setup(api): api.set_popup(create)\n",
        )
        process = self.start(plugins=("stream_bar", "broken", "stream_tcsh"))
        process.expect(BAR)
        offset = len(process.output)
        process.send(F2 + b"touch failed-popup-leak\r")
        process.expect(PROMPT, offset)
        process.command("echo recovered > result")
        process.expect_file(self.root / "result", "recovered\n")
        self.assertFalse((self.root / "failed-popup-leak").exists())
        self.exit(process)
        self.assertIn(b"view failed", process.output)

    def test_bar_callback_error_preserves_native_commands(self):
        """A display callback failure retires decoration while the shell remains usable."""
        self.plugin(
            "broken_bar",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n"
            "def text():\n"
            "    if (ROOT / 'fail-bar').exists():\n"
            "        (ROOT / 'bar-failure-observed').touch()\n"
            "        raise RuntimeError('bar failed')\n"
            "    return 'WORKING BAR'\n"
            "def setup(api): api.set_bar(text)\n",
        )
        process = self.start(plugins=("broken_bar", "stream_tcsh"))
        process.expect(b"WORKING BAR")
        process.command("touch fail-bar")
        process.wait((self.root / "bar-failure-observed").exists, "callback failed")
        process.command("echo survived > result")
        process.expect_file(self.root / "result", "survived\n")
        self.exit(process)
        self.assertIn(b"bar failed", process.output)

    def test_async_popup_error_releases_native_input_without_console_traceback(self):
        """A real async plugin failure cannot keep focus or execute popup data."""
        self.plugin(
            "async_popup",
            "import asyncio\n"
            "from pathlib import Path\n"
            "from ish.stream.ui import PopupView\n"
            "from prompt_toolkit.key_binding import KeyBindings\n"
            "from prompt_toolkit.layout import HSplit, Window\n"
            "from prompt_toolkit.layout.controls import FormattedTextControl\n"
            "from prompt_toolkit.widgets import TextArea\n"
            "ROOT = Path(__file__).resolve().parents[2]\n"
            "def create(accept):\n"
            "    field = TextArea(height=1)\n"
            "    keys = KeyBindings()\n"
            "    @keys.add('tab', eager=True)\n"
            "    async def failed(event):\n"
            "        await asyncio.sleep(0)\n"
            "        (ROOT / 'async-failure').touch()\n"
            "        raise ValueError('async plugin failed')\n"
            "    title = Window(FormattedTextControl('ASYNC POPUP'), height=1)\n"
            "    return PopupView(HSplit([title, field], key_bindings=keys), field)\n"
            "def setup(api): api.set_popup(create)\n",
        )
        for shell in ("tcsh", "bash", "zsh"):
            with self.subTest(shell=shell):
                marker = self.root / "async-failure"
                marker.unlink(missing_ok=True)
                process = self.start(
                    shell=shell,
                    plugins=("stream_bar", "async_popup", f"stream_{shell}"),
                )
                process.expect(BAR)
                process.send(F2)
                process.expect(b"ASYNC POPUP")
                process.send(
                    b"touch async-popup-leak\t"
                    b"\x1b[200~touch async-paste-leak\n\x1b[201~"
                )
                process.wait(marker.exists, "async binding failure")
                result = self.root / f"async-result-{shell}"
                process.command(f"echo recovered > {result.name}")
                process.expect_file(result, "recovered\n")
                self.assertFalse((self.root / "async-popup-leak").exists())
                self.assertFalse((self.root / "async-paste-leak").exists())
                self.assertNotIn(b"async plugin failed", process.output)
                self.exit(process)
                self.assertIn(b"async plugin failed", process.output)
                self.assertNotIn(b"Traceback", process.output)
