"""Independent native consumers shared by PTY and actual-host VT checks."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import sys
from pathlib import Path

from test_terminal_cli import (
    F2,
    F10,
    PASTE_END,
    PASTE_START,
    STARTUP_DEADLINE,
    Terminal,
)

SHELLS = ("tcsh", "bash", "zsh")
BAR = "HOOKLESS TEST BAR"
TITLE = "HOOKLESS TEST POPUP"
RC_NAMES = {"bash": ".bashrc", "zsh": ".zshrc", "tcsh": ".tcshrc"}
STARTUP = {
    "bash": "PS1='READY> '\nPS2='CONT> '\nPROMPT_COMMAND=':'\ndeclare -p PROMPT_COMMAND > initial-hooks\n",
    "zsh": "PROMPT='READY> '\nPS2='CONT> '\nprecmd() { :; }\nfunctions precmd > initial-hooks\n",
    "tcsh": "set prompt = 'READY> '\nset prompt2 = 'CONT> '\nset history = 100\nunset autologout\nalias precmd 'true'\nalias precmd > initial-hooks\n",
}
HOOK_QUERY = {
    "bash": "declare -p PROMPT_COMMAND > observed-hooks",
    "zsh": "functions precmd > observed-hooks",
    "tcsh": "alias precmd > observed-hooks",
}
RELOAD = {
    "bash": "PROMPT_COMMAND=\"printf 'reloaded\\n' >> reload-hooks\"\n",
    "zsh": "precmd_functions=()\npreexec_functions=()\nprecmd() { print -r -- reloaded >> reload-hooks; }\n",
    "tcsh": "alias precmd 'echo reloaded >> reload-hooks'\nalias postcmd 'true'\n",
}
LOOP = {
    "tcsh": ("foreach item (one two)", "printf '%s\\n' $item >> loop-result\rend\r"),
    "bash": (
        "for item in one two; do",
        'printf "%s\\n" "$item" >> loop-result\rdone\r',
    ),
    "zsh": ("for item in one two; do", 'printf "%s\\n" "$item" >> loop-result\rdone\r'),
}
POPUP_PLUGIN = '''"""Explicit test UI loaded through the public PluginManager contract."""
from pathlib import Path
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

PLUGIN_META = {
    "version": "1",
    "settings": {
        "label": {"title": "Bar text", "description": "Text in the test bar.", "default": "HOOKLESS TEST BAR"},
    },
}
TERMINAL_API = 1

def setup(api):
    root = Path.home()
    api.set_bar(Window(FormattedTextControl(api.settings_for("terminal_check")["label"])), rows=1)
    sequence = 0
    popup = None

    def finish(value):
        nonlocal popup
        if api.close_popup(popup, value):
            popup = None
            kind = "cancel" if value is None else "value"
            (root / f"popup-{kind}-{sequence}").write_text("cancelled" if value is None else value)

    @api.bind("f2")
    def toggle(event):
        nonlocal sequence, popup
        if popup is not None:
            finish(None)
            return
        sequence += 1
        keys = KeyBindings()
        @keys.add("escape", eager=True)
        @keys.add("c-c", eager=True)
        @keys.add("c-d", eager=True)
        def cancel(event):
            finish(None)
        def accept(buffer):
            finish(buffer.text)
        field = TextArea(multiline=False, accept_handler=accept)
        title = Window(FormattedTextControl("HOOKLESS TEST POPUP"), height=1)
        content = HSplit([title, field], key_bindings=keys)
        popup = api.show_popup(content, width=42, height=4)
        (root / "popup-open").write_text(str(sequence))
'''
RAW_READER = r"""import json
import os
import select
import signal
import termios
import tty
from pathlib import Path

saved = termios.tcgetattr(0)
received = bytearray()
sizes = []
resized = True
def changed(*_):
    global resized
    resized = True
signal.signal(signal.SIGWINCH, changed)
try:
    tty.setraw(0)
    Path('raw-ready').write_text('ready')
    os.write(1, b'HOOKLESS_NATIVE_READY\r\n')
    while not received.endswith(b'|done|'):
        if resized:
            resized = False
            size = os.get_terminal_size(0)
            sizes.append([size.columns, size.lines])
            temporary = Path('raw-sizes.next')
            temporary.write_text(json.dumps(sizes))
            temporary.replace('raw-sizes')
        if select.select([0], [], [], 0.05)[0]:
            data = os.read(0, 4096)
            if not data:
                break
            received.extend(data)
            Path('raw-progress').write_bytes(received)
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
Path('raw-result').write_bytes(received)
"""
JOB_READER = r"""import json
import os
import signal
import sys
from pathlib import Path

def continued(*_):
    Path('job-resumed').write_text(str(os.getpid()))

signal.signal(signal.SIGCONT, continued)
Path('job-ready').write_text(json.dumps({
    'pid': os.getpid(),
    'foreground': os.tcgetpgrp(0) == os.getpgrp(),
}))
Path('job-result').write_text(sys.stdin.readline())
Path('job-final').write_text(
    'foreground\n' if os.tcgetpgrp(0) == os.getpgrp() else 'background\n'
)
"""

LOAD_READER = r"""import hashlib
import os
import threading
import time
import termios
import tty
from pathlib import Path

def output():
    while not Path('release-output').exists():
        time.sleep(0.01)
    for number in range(1200):
        data = (f'LOAD:{number:04d} ' + 'wide 한글 ' * 8 + '\r\n').encode()
        while data:
            data = data[os.write(1, data):]
    os.write(1, b'LOAD_DONE\r\n')
    Path('output-done').write_text('done')

saved = termios.tcgetattr(0)
received = bytearray()
worker = threading.Thread(target=output, daemon=True)
try:
    tty.setraw(0)
    Path('load-ready').write_text('ready')
    worker.start()
    while not received.endswith(b'|done|'):
        block = os.read(0, 4096)
        if not block:
            raise EOFError('stdin ended before the complete payload')
        received.extend(block)
    worker.join()
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
Path('load-result').write_text(str(len(received)) + '\n' + hashlib.sha256(received).hexdigest() + '\n')
"""


def install_ui_fixture(root: Path) -> Path:
    """Make the PTY and composition tests load the same external UI package."""
    directory = root / "plugins" / "terminal_check"
    directory.mkdir(parents=True)
    (directory / "terminal_check.py").write_text(POPUP_PLUGIN, encoding="utf-8")
    return directory.parent


def prepare(root: Path, shell: str, plugins: bool) -> list[str]:
    """Use isolated native rc files, never a production shell integration script."""
    if shutil.which(shell) is None:
        raise RuntimeError(f"Required shell is missing: {shell}")
    if shutil.which("vim") is None:
        raise RuntimeError("Required native Vim is missing")
    root.mkdir(parents=True, exist_ok=True)
    (root / RC_NAMES[shell]).write_text(STARTUP[shell], encoding="utf-8")
    (root / "raw-reader.py").write_text(RAW_READER, encoding="utf-8")
    (root / "job-reader.py").write_text(JOB_READER, encoding="utf-8")
    (root / "load-reader.py").write_text(LOAD_READER, encoding="utf-8")
    arguments = []
    if plugins:
        scripts = install_ui_fixture(root)
        arguments = [
            "--plugin-dir",
            str(scripts),
            "--plugin-lib-dir",
            str(root / "libraries"),
            "--plugin",
            "terminal_check",
        ]
    return arguments


class HooklessTerminal(Terminal):
    """Reuse the actual PTY driver while varying only the selected native shell/UI."""

    def __init__(
        self,
        root,
        *,
        shell,
        plugins,
        rows=24,
        cols=100,
        environment=None,
        extra_args=(),
        executable=None,
    ):
        """Select an ordinary interactive shell and explicit external test plugin."""
        arguments = prepare(root, shell, plugins)
        super().__init__(
            root,
            rows=rows,
            cols=cols,
            shell=shutil.which(shell),
            extra_args=[*arguments, *extra_args],
            environment=environment,
            configure=False,
            executable=executable,
        )


class Scenario:
    """Assert final native files and rendered cells at each public input boundary."""

    def __init__(self, terminal, shell, plugins, *, stage=None, resize=None):
        """Share outcomes between independent-screen and real-host execution."""
        self.terminal = terminal
        self.root = terminal.root
        self.shell = shell
        self.plugins = plugins
        self.stage = stage or (lambda name: None)
        self.resize = resize or self.resize_pty
        self.popup = 0
        self.files = []

    def resize_pty(self, kind):
        """Change the real outer PTY while native raw input and Vim are active."""
        rows, cols = (31, 108) if kind == "native-program" else (29, 104)
        self.terminal.resize(rows, cols)
        return True

    def result(self, name, value):
        """Require exact data from the independently executed final consumer."""
        self.terminal.result(name, value)
        self.files.append(name)

    def command(self, text, name, value):
        """Submit a shell command and wait for its file plus native prompt return."""
        self.terminal.submit(text)
        self.result(name, value)
        self.terminal.require(self.terminal.at_prompt)

    def layout(self):
        """An opt-in bar is always present; the base reserves no UI rows or keys."""
        terminal = self.terminal
        rows = terminal.rows - int(self.plugins)
        terminal.require(lambda: terminal.state().get("rows") == rows)
        if self.plugins:
            terminal.require(lambda: terminal.frame_has(BAR))
        elif terminal.frame_has(BAR) or terminal.state().get("popups"):
            raise AssertionError("An unselected plugin appeared in the base session")

    def open_popup(self):
        """Require rendered PTK content and a new externally recorded invocation."""
        self.popup += 1
        self.terminal.send(F2)
        self.result("popup-open", str(self.popup).encode())
        self.terminal.require(lambda: self.terminal.frame_has(TITLE))

    def cancel_popup(self, key=None, tail=b""):
        """Cancel popup input without sending that input to the native consumer."""
        if key is None:
            key = self.terminal.escape_key
        self.terminal.send(b"discarded popup" + key + tail)
        self.result(f"popup-cancel-{self.popup}", b"cancelled")
        self.terminal.require(lambda: not self.terminal.state().get("popups"))

    def job_control(self):
        """Stop and resume the same native foreground stdin consumer via the shell."""
        terminal = self.terminal
        terminal.submit(f"{shlex.quote(sys.executable)} job-reader.py")
        ready = self.root / "job-ready"
        terminal.require(lambda: ready.exists() and ready.read_text().endswith("}"))
        info = json.loads(ready.read_text())
        if info["foreground"] is not True:
            raise AssertionError("The native job did not start in the foreground")
        terminal.send(b"\x1a")
        terminal.require(terminal.at_prompt)
        terminal.submit("jobs -l > job-stopped")
        stopped = self.root / "job-stopped"
        terminal.require(
            lambda: stopped.exists() and stopped.read_bytes().endswith(b"\n")
        )
        observed = stopped.read_text().lower()
        if (
            str(info["pid"]) not in observed
            or "job-reader.py" not in observed
            or not any(word in observed for word in ("stopped", "suspended"))
        ):
            raise AssertionError(
                f"The shell did not report the stopped native job: {observed!r}"
            )
        self.files.append("job-stopped")
        terminal.require(terminal.at_prompt)
        terminal.submit("bg")
        self.result("job-resumed", str(info["pid"]).encode())
        terminal.require(terminal.at_prompt)
        terminal.submit("jobs -l > job-background")
        background = self.root / "job-background"
        terminal.require(
            lambda: background.exists() and background.read_bytes().endswith(b"\n")
        )
        terminal.require(terminal.at_prompt)
        if str(info["pid"]) not in background.read_text():
            raise AssertionError("The shell lost the native job after bg")
        self.files.append("job-background")
        terminal.submit("fg")
        self.result("job-resumed", str(info["pid"]).encode())
        terminal.send(b"resumed native stdin\r")
        self.result("job-result", b"resumed native stdin\n")
        self.result("job-final", b"foreground\n")
        terminal.require(terminal.at_prompt)
        self.layout()
        self.stage("native-job-stop-jobs-foreground-resume")

    def load(self):
        """Keep native output flowing under a popup and preserve a large paste exactly."""
        terminal = self.terminal
        terminal.submit(f"{shlex.quote(sys.executable)} load-reader.py")
        self.result("load-ready", b"ready")
        if self.plugins:
            self.open_popup()
        (self.root / "release-output").touch()
        self.result("output-done", b"done")
        if self.plugins:
            terminal.require(lambda: terminal.frame_has(TITLE))
            self.cancel_popup()
        terminal.require(lambda: terminal.frame_has("LOAD_DONE"))
        payload = ("large paste 한글\n" * 3000).encode()
        terminal.send(PASTE_START)
        for offset in range(0, len(payload), 4096):
            terminal.send(payload[offset : offset + 4096])
            terminal.receive()
        terminal.send(PASTE_END + b"|done|")
        # This raw consumer does not request bracketed paste. Native terminal
        # paste therefore sends Enter (CR) for line breaks, without wrappers.
        expected = ("large paste 한글\r" * 3000).encode() + b"|done|"
        self.result(
            "load-result",
            f"{len(expected)}\n{hashlib.sha256(expected).hexdigest()}\n".encode(),
        )
        terminal.require(terminal.at_prompt)
        self.layout()
        self.stage("large-output-popup-background-and-exact-paste")

    def run(self):
        """Exercise shell editing, raw stdin and alternate-screen UI without hooks."""
        terminal = self.terminal
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        self.layout()
        expected_hooks = (self.root / "initial-hooks").read_bytes()
        self.command(HOOK_QUERY[self.shell], "observed-hooks", expected_hooks)
        self.command(
            "stty size > initial-size",
            "initial-size",
            f"{terminal.rows - int(self.plugins)} {terminal.cols}\n".encode(),
        )
        terminal.send("printf first > simple\rprintf second > fast-input\r")
        self.result("simple", b"first")
        self.result("fast-input", b"second")
        terminal.require(terminal.at_prompt)
        (self.root / "unique-native-completion").write_text("native Tab\n")
        self.command(
            "cat unique-native-comp\t > completion", "completion", b"native Tab\n"
        )
        self.stage("native-primary-and-completion")

        terminal.submit("printf '%s\\n' one \\")
        terminal.require(lambda: terminal.at_prompt(continuation=True))
        self.layout()
        if self.plugins:
            self.open_popup()
            self.cancel_popup()
        terminal.submit("two > backslash-result")
        self.result("backslash-result", b"one\ntwo\n")
        terminal.require(terminal.at_prompt)
        head, body = LOOP[self.shell]
        terminal.submit(head)
        terminal.require(lambda: terminal.at_prompt(continuation=True))
        if (self.root / "loop-result").exists():
            raise AssertionError("A native loop executed before its body was submitted")
        terminal.send(body)
        self.result("loop-result", b"one\ntwo\n")
        terminal.require(terminal.at_prompt)
        payload = "  한 글 🙂  \nish_exit\nish_reload\nprobe\n"
        terminal.submit("cat << END > heredoc-result")
        terminal.require(lambda: terminal.at_prompt(continuation=True))
        self.layout()
        if self.plugins:
            self.open_popup()
            self.cancel_popup()
        terminal.send(payload.replace("\n", "\r") + "END\r")
        self.result("heredoc-result", payload.encode())
        terminal.require(terminal.at_prompt)
        pasted = b"printf paste-a > paste-a\nprintf paste-b > paste-b"
        terminal.send(PASTE_START + pasted + PASTE_END + b"\r")
        self.result("paste-a", b"paste-a")
        self.result("paste-b", b"paste-b")
        terminal.require(terminal.at_prompt)
        self.stage("native-continuation-heredoc-and-multiline-paste")

        # Re-source the real rc file after replacing its hook registration. The
        # UI must survive even though every original prompt hook is overwritten.
        (self.root / RC_NAMES[self.shell]).write_text(
            STARTUP[self.shell] + RELOAD[self.shell]
        )
        terminal.submit("source " + RC_NAMES[self.shell])
        terminal.require(lambda: (self.root / "reload-hooks").exists())
        terminal.require(terminal.at_prompt)
        self.layout()
        self.command("printf sourced > after-source", "after-source", b"sourced")
        self.stage("rc-source-and-hook-replacement")

        terminal.submit(f"{shlex.quote(sys.executable)} raw-reader.py")
        self.result("raw-ready", b"ready")
        terminal.require(lambda: terminal.frame_has("HOOKLESS_NATIVE_READY"))
        terminal.send(b"before|")
        expected = b"before|"
        if self.plugins:
            self.open_popup()
            terminal.send("accepted 한 글\rafter-accept|")
            self.result(f"popup-value-{self.popup}", "accepted 한 글".encode())
            expected += b"after-accept|"
            self.result("raw-progress", expected)
            self.open_popup()
            self.cancel_popup(b"\x03", b"after-cancel|")
            expected += b"after-cancel|"
            self.result("raw-progress", expected)
            self.open_popup()
            self.cancel_popup(b"\x04", b"after-eof|")
            expected += b"after-eof|"
            self.result("raw-progress", expected)
            self.open_popup()
            self.cancel_popup()
            terminal.send(b"after-escape|")
            expected += b"after-escape|"
        else:
            terminal.send(F2 + F10)
            expected += b"\x1bOQ\x1b[21~"
        self.result("raw-progress", expected)
        self.layout()
        self.stage("foreground-popup-and-exact-typeahead")
        terminal.require(lambda: (self.root / "raw-sizes").exists())
        before = json.loads((self.root / "raw-sizes").read_text())[-1]
        changed = self.resize("native-program")
        wanted = [terminal.cols, terminal.rows - int(self.plugins)]
        terminal.require(
            lambda: (
                json.loads((self.root / "raw-sizes").read_text())[-1] == wanted
                and (changed is False or wanted != before)
            )
        )
        self.layout()
        terminal.send(b"|done|")
        self.result("raw-result", expected + b"|done|")
        terminal.require(terminal.at_prompt)
        self.stage("native-foreground-resize")

        self.job_control()
        self.load()

        terminal.submit("vim -Nu NONE -n -i NONE vim-result")
        terminal.require(lambda: terminal.state().get("alternate") is True)
        terminal.require(lambda: terminal.frame_has("vim-result"))
        terminal.send(
            b"i"
            + PASTE_START
            + "Vim 한 글\nsecond line".encode()
            + PASTE_END
            + terminal.escape_key
            + b":w\r"
        )
        self.result("vim-result", "Vim 한 글\nsecond line\n".encode())
        self.layout()
        if self.plugins:
            self.open_popup()
            self.cancel_popup()
        before = [terminal.cols, terminal.rows]
        changed = self.resize("vim")
        if changed is not False and before == [terminal.cols, terminal.rows]:
            raise AssertionError("The active Vim resize did not change the outer PTY")
        self.layout()
        terminal.send(
            ":call writefile([string(&lines), string(&columns)], 'vim-size')\r"
        )
        self.result(
            "vim-size",
            f"{terminal.rows - int(self.plugins)}\n{terminal.cols}\n".encode(),
        )
        terminal.send(":q\r")
        terminal.require(terminal.at_prompt)
        self.layout()
        self.stage("vim-native-editing-and-popup")

        terminal.submit("cat > interrupted-result")
        terminal.require(lambda: (self.root / "interrupted-result").exists())
        terminal.send(b"\x03")
        terminal.require(terminal.at_prompt)
        terminal.submit("cat > eof-result")
        terminal.require(lambda: (self.root / "eof-result").exists())
        terminal.send(b"native EOF\r\x04")
        self.result("eof-result", b"native EOF\n")
        terminal.require(terminal.at_prompt)
        self.command("printf finished > final-result", "final-result", b"finished")
        terminal.finish()
        self.stage("interrupt-eof-and-restoration")
