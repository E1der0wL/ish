"""Run repeatable stream scenarios inside an actual host terminal.

Reuse HostRun for PTY input, native file results and real terminal responses.
Stages advance automatically; --require-resize requests a window resize and Enter.
This command does not claim screenshot or GUI keyboard verification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

from stream_host import HostRun
from stream_plugins import SHELL_PLUGINS, copy_plugins

SHELLS = ("tcsh", "bash", "zsh")
SCENARIOS = ("plugins", "plain", "async-error", "factory-task")
F2 = b"\x1bOQ"
FAILURE = "STREAM_ASYNC_FAILURE"
ESCAPE_RETURN_SECONDS = 1.0
SETTINGS_PLUGIN = """
from pathlib import Path
PLUGIN_META = {"version": "1", "requirements": ["stream_popup"], "dependencies": []}
STREAM_API = 1

def setup(api):
    sample = api.get("stream_popup")
    def create(accept):
        def record(value):
            accepted = accept(value)
            if accepted and value is not None:
                Path.home().joinpath("popup-value").write_text(value)
            return accepted
        return sample.create(record)
    api.set_popup(create)
"""
ASYNC_PLUGIN = """
import asyncio
from pathlib import Path
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea
from ish.stream.ui import PopupView
PLUGIN_META = {"version": "1", "requirements": [], "dependencies": []}
STREAM_API = 1

def create(accept):
    bindings = KeyBindings()
    @bindings.add("tab", eager=True)
    async def fail(event):
        await asyncio.sleep(0)
        Path.home().joinpath("async-triggered").write_text("failed")
        raise ValueError("STREAM_ASYNC_FAILURE")
    field = TextArea(height=1, multiline=False)
    title = Window(FormattedTextControl("ASYNC TEST POPUP"), height=1)
    return PopupView(HSplit([title, field], key_bindings=bindings), field)

def setup(api):
    api.set_popup(create)
"""
FACTORY_PLUGIN = """
import asyncio
from pathlib import Path
from prompt_toolkit.application import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea
from ish.stream.ui import PopupView
PLUGIN_META = {"version": "1", "requirements": [], "dependencies": []}
STREAM_API = 1
calls = 0
release = asyncio.Event()

async def lookup():
    await release.wait()
    raise ValueError("RETIRED_FACTORY_FAILURE")

def create(accept):
    global calls
    calls += 1
    if calls == 1:
        task = get_app().create_background_task(lookup())
        task.add_done_callback(lambda _: Path.home().joinpath("factory-settled").write_text("settled"))
    bindings = KeyBindings()
    @bindings.add("tab", eager=True)
    def finish_old(event):
        release.set()
    def submit(buffer):
        if accept(buffer.text):
            Path.home().joinpath("popup-value").write_text(buffer.text)
    field = TextArea(height=1, multiline=False, accept_handler=submit)
    title = Window(FormattedTextControl("FACTORY POPUP " + str(calls)), height=1)
    return PopupView(HSplit([title, field], key_bindings=bindings), field)

def setup(api):
    api.set_popup(create)
"""


class AutomaticRun(HostRun):
    """Publish the existing stage evidence without a separate gate controller."""

    def __init__(self, *args, **kwargs):
        """Reserve confirmation input only during the explicit resize test gate."""
        super().__init__(*args, **kwargs)
        self.confirm_resize = False

    def gate(self, name, **observations):
        """Confirm the user's drag ended before allowing the next scenario."""
        self.confirm_resize = name == "resize-native-program" and self.require_resize
        if self.confirm_resize:
            print(
                "\r\nResize the window, finish dragging, then press Enter.\r\n",
                flush=True,
            )
        else:
            (self.directory / f"continue-{self.stage + 1}").touch()
        try:
            super().gate(name, **observations)
            if self.confirm_resize:
                # The final SIGWINCH may arrive after pump's initial size check.
                # Read the actual size once the user confirms dragging is over.
                self.resize()
                self._apply_resize()
        finally:
            self.confirm_resize = False

    def host_input(self, data):
        """Consume only the explicit resize confirmation; relay host replies."""
        if self.confirm_resize and (b"\r" in data or b"\n" in data):
            (self.directory / f"continue-{self.stage}").touch()
            data = data.replace(b"\r", b"").replace(b"\n", b"")
        if data:
            super().host_input(data)

    def primary_after(self, offset):
        """Observe a new semantic P even when ZLE reuses existing prompt cells."""
        self.wait(
            lambda: (
                "셸 이벤트 kind=P"
                in (self.directory / "session.log").read_text()[offset:]
            ),
            "primary event after popup return",
        )

    def finish(self, native_file):
        """Exit normally and retain the actual shell result and restored terminal."""
        self.exiting = True
        self.send(b"exit\r")
        self.wait(lambda: self.exited, "normal exit")
        _, status = os.waitpid(self.pid, 0)
        self.pid = None
        code = os.waitstatus_to_exitcode(status)
        assert code == 0, code
        assert b"Traceback" not in self.output
        self.result.update(
            status="passed",
            returncode=code,
            native_files=[native_file, "lang-result"],
            sizes=self.sizes,
        )


class SettingsRun(AutomaticRun):
    """Exercise persisted plugin selection and popup keys through the host path."""

    def scenario(self):
        """Keep the native scenario and include its new ESC recovery file in evidence."""
        super().scenario()
        self.result["native_files"].append("escape-result")

    def popup(self):
        """Record Alt editing, bounded ESC return and the final native consumer."""
        offset = len(self.output)
        self.send(F2)
        self.expect(self.popup_title, offset)
        log_offset = len((self.directory / "session.log").read_text())
        self.send("팝업 값 one two".encode() + b"\x1bbX\r")
        value = self.home / "popup-value"
        self.wait(value.exists, "rc-selected popup accepted its edited value")
        assert value.read_text() == "팝업 값 one Xtwo", value.read_text()
        self.primary_after(log_offset)
        offset = len(self.output)
        self.send(F2)
        self.expect(self.popup_title, offset)
        self.send(b"touch escape-leak")
        log_offset = len((self.directory / "session.log").read_text())
        started = time.monotonic()
        self.send(b"\x1b")
        self.primary_after(log_offset)
        elapsed = time.monotonic() - started
        assert elapsed < ESCAPE_RETURN_SECONDS, elapsed
        self.command("echo restored > escape-result")
        assert (self.home / "escape-result").read_text() == "restored\n"
        assert value.read_text() == "팝업 값 one Xtwo"
        assert not (self.home / "escape-leak").exists()
        self.result["popup_keys"] = {
            "value": value.read_text(),
            "escape_return_seconds": elapsed,
            "native_file": "escape-result",
        }
        self.gate("rc-popup-alt-and-escape", **self.result["popup_keys"])


def write_settings(directory: Path, plugins: Path) -> Path:
    """Save the same public Python settings reused by every selected host run."""
    path = directory / "settings/.ishrc.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    selected = ["stream_bar", "recorded_popup", *SHELL_PLUGINS]
    path.write_text(
        'config.lang = "ko"\n'
        f"config.plugins = {selected!r}\n"
        f"config.plugin_dir = {os.path.relpath(plugins, path.parent)!r}\n"
        'config.log_file = "../session.log"\n'
        'config.log_level = "DEBUG"\n'
        "config.escape_delay = 0.05\n"
    )
    return path


class AsyncFailureRun(AutomaticRun):
    """Keep the native shell usable after a current popup binding fails."""

    def scenario(self):
        """Prove native recovery, discarded popup input and deferred diagnostics."""
        self.expect(self.bar)
        self.check_language()
        self.gate("primary-before-async-failure")
        offset = len(self.output)
        self.send(F2)
        self.expect(b"ASYNC TEST POPUP", offset)
        self.gate("async-popup")
        self.send(b"\ttouch async-leak\r")
        self.wait((self.home / "async-triggered").exists, "async binding failure")

        def returned():
            records = (self.directory / "session.log").read_text()
            _, failed, following = records.partition(FAILURE)
            return bool(failed and "셸 이벤트 kind=P" in following)

        self.wait(returned, "primary event after async failure")
        self.demo = False
        self.command("echo recovered > async-result")
        assert (self.home / "async-result").read_text() == "recovered\n"
        assert not (self.home / "async-leak").exists()
        assert FAILURE.encode() not in self.output
        self.gate("native-recovered-after-async-failure")
        self.finish("async-result")
        assert self.output.count(FAILURE.encode()) == 1
        assert (self.directory / "session.log").read_text().count(FAILURE) == 1


class FactoryTaskRun(AutomaticRun):
    """Exercise factory task retirement through the real CLI and PTK popup."""

    def scenario(self):
        """Keep the second popup editable after the first factory task fails."""
        self.expect(self.bar)
        self.check_language()
        offset = len(self.output)
        self.send(F2)
        self.expect(b"FACTORY POPUP 1", offset)
        log_offset = len((self.directory / "session.log").read_text())
        self.send(b"old\r")
        self.primary_after(log_offset)
        offset = len(self.output)
        self.send(F2)
        self.expect(b"FACTORY POPUP 2", offset)
        self.send(b"\t")
        self.wait((self.home / "factory-settled").exists, "old factory task settlement")
        log_offset = len((self.directory / "session.log").read_text())
        self.send(b"touch factory-leak\r")
        self.primary_after(log_offset)
        self.wait(
            lambda: (self.home / "popup-value").read_text() == "touch factory-leak",
            "second popup accepted its own input",
        )
        assert not (self.home / "factory-leak").exists()
        self.command("echo recovered > factory-result")
        assert (self.home / "factory-result").read_text() == "recovered\n"
        self.gate("second-popup-survived-retired-factory")
        self.finish("factory-result")
        assert b"RETIRED_FACTORY_FAILURE" not in self.output
        assert (
            "RETIRED_FACTORY_FAILURE"
            not in (self.directory / "session.log").read_text()
        )


def main(argv=None):
    """Run selected cases once, preserving every failed attempt's evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--shell", action="append", choices=SHELLS)
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    parser.add_argument("--require-resize", action="store_true")
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        parser.error(
            "Use a new evidence directory; previous results are never overwritten"
        )
    if not sys.platform.startswith("linux") or not all(os.isatty(fd) for fd in (0, 1)):
        parser.error("Run inside a real Linux/WSL host terminal")
    shells = tuple(dict.fromkeys(args.shell or SHELLS))
    scenarios = tuple(dict.fromkeys(args.scenario or SCENARIOS))
    if args.require_resize and not {"plugins", "plain"}.intersection(scenarios):
        parser.error("--require-resize needs a plugins or plain scenario")
    directory.mkdir(parents=True, exist_ok=True)
    plugins = copy_plugins(directory / "plugins")
    (plugins / "recorded_popup").mkdir()
    (plugins / "recorded_popup/recorded_popup.py").write_text(SETTINGS_PLUGIN)
    error_plugins = copy_plugins(directory / "async-plugins")
    (error_plugins / "stream_popup/stream_popup.py").write_text(ASYNC_PLUGIN)
    factory_plugins = copy_plugins(directory / "factory-plugins")
    (factory_plugins / "stream_popup/stream_popup.py").write_text(FACTORY_PLUGIN)
    providers = {"async-error": error_plugins, "factory-task": factory_plugins}
    runners = {
        "plugins": SettingsRun,
        "async-error": AsyncFailureRun,
        "factory-task": FactoryTaskRun,
    }
    results = []
    resize_pending = args.require_resize
    for shell in shells:
        for scenario in scenarios:
            name = f"{shell}-{scenario}"
            case = directory / name
            selected = scenario != "plain"
            config_file = (
                write_settings(case, plugins) if scenario == "plugins" else None
            )
            extra = () if config_file else ("--home", str(case / "settings"))
            if selected and config_file is None:
                extra += (
                    "--log-level",
                    "DEBUG",
                    "--log-file",
                    str(case / "session.log"),
                )
            resize = resize_pending and scenario in ("plugins", "plain")
            runner = runners.get(scenario, AutomaticRun)(
                case,
                selected,
                shell,
                plugin_dir=providers.get(scenario, plugins),
                config_file=config_file,
                extra_args=extra,
                require_resize=resize,
                language="ko",
                lang_env="C.UTF-8" if config_file else "ko_KR.UTF-8",
            )
            resize_pending = resize_pending and not resize
            try:
                runner.start()
                runner.scenario()
                if selected:
                    records = (case / "session.log").read_text()
                    assert "세션 종료 status=0" in records
                    assert "팝업 focus=True" in records
                    assert "STREAM_VIM_RESULT" not in records
                else:
                    assert not (case / "settings").exists()
            except Exception:
                runner.result.update(status="failed", error=traceback.format_exc())
            finally:
                runner.close()
            if not runner.result["parent_termios_restored"]:
                runner.result.update(
                    status="failed", error="Parent terminal was not restored"
                )
                runner.record("result.json", runner.result)
            results.append(
                {"name": name, "status": runner.result["status"], "evidence": name}
            )
            temporary = directory / "summary.tmp"
            temporary.write_text(json.dumps(results, indent=2))
            temporary.replace(directory / "summary.json")
            if runner.result["status"] != "passed":
                print(f"\r\nISH HOST VERIFICATION FAILED: {case}\r\n")
                return 1
    print("\r\nISH HOST VERIFICATION: " + json.dumps(results) + "\r\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
