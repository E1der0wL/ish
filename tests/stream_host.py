"""Drive ish in an actual host terminal, with screenshot gates and real PTY input.

This is deliberately separate from pyte checks. Output reaches the real host,
host replies flow back to ish, and test inputs enter ish's outer PTY. Windows
keyboard encoding and GUI pixels require the separate observer's verification.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import select
import shlex
import signal
import sys
import termios
import time
import traceback
import tty
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = b"STREAM_PRIMARY> "
TEST_SECONDS = 45
GATE_SECONDS = 600
READ_BYTES = 65536


class HostRun:
    """Keep the real host as renderer and response source throughout each stage."""

    def __init__(self, directory: Path, demo: bool):
        """Prepare isolated shell state and evidence without starting another GUI."""
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.home = self.directory / "home"
        self.home.mkdir(exist_ok=True)
        self.output = bytearray()
        self.demo = demo
        self.fd = self.pid = None
        self.stage = 0
        self.sizes = []
        self.exiting = False
        self.exited = False
        self.result = {
            "status": "running",
            "scope": "native files and real host protocol responses",
            "demo": demo,
            "stages": [],
            "pixel_verification": "not performed",
            "gui_keyboard_verification": "not performed",
        }
        (self.home / ".tcshrc").write_text(
            "set prompt = 'STREAM_PRIMARY> '\nset prompt2 = 'STREAM_MORE> '\n"
        )
        self.saved = termios.tcgetattr(0)
        self.original_resize = signal.getsignal(signal.SIGWINCH)

    def record(self, name: str, value: dict):
        """Publish complete JSON files so an external observer never sees half a record."""
        temporary = self.directory / (name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temporary.replace(self.directory / name)

    def resize(self, *_):
        """Propagate an actual Windows Terminal/WSL resize to ish's outer PTY."""
        rows, cols = termios.tcgetwinsize(0)
        self.sizes.append([cols, rows])
        if self.fd is not None:
            termios.tcsetwinsize(self.fd, (rows, cols))
        self.record("host-size.json", {"sizes": self.sizes})

    def start(self):
        """Launch the public CLI in a controlling PTY and preserve the host's identity."""
        argv = [
            sys.executable,
            "-m",
            "ish.stream",
            "--shell",
            "tcsh",
            "--cwd",
            str(self.home),
        ]
        if self.demo:
            argv.append("--demo")
        rows, cols = termios.tcgetwinsize(0)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            termios.tcsetwinsize(0, (rows, cols))
            environment = dict(os.environ)
            environment.update(
                HOME=str(self.home),
                PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests"),
                LANG="C.UTF-8",
            )
            os.execvpe(argv[0], argv, environment)
        os.set_blocking(self.fd, False)
        tty.setraw(0)
        signal.signal(signal.SIGWINCH, self.resize)
        self.resize()
        self.record(
            "environment.json",
            {
                "TERM": os.environ.get("TERM"),
                "WT_SESSION": os.environ.get("WT_SESSION"),
                "WSL_DISTRO_NAME": os.environ.get("WSL_DISTRO_NAME"),
                "python": sys.version,
                "pid": self.pid,
                "input_method": "scripted bytes into outer PTY; host responses relayed",
            },
        )

    def send(self, data: bytes):
        """Enter bytes through the same outer PTY consumed by the ish application."""
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.fd, view) :]
            except BlockingIOError:
                self.pump()

    def pump(self):
        """Relay output unchanged to the host and actual host input back to ish."""
        ready, _, _ = select.select([0, self.fd], [], [], 0.05)
        if self.fd in ready:
            try:
                data = os.read(self.fd, READ_BYTES)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""
            if not data:
                if not self.exiting:
                    raise EOFError("ish CLI closed its terminal")
                self.exited = True
                return
            self.output.extend(data)
            view = memoryview(data)
            while view:
                view = view[os.write(1, view) :]
        if 0 in ready:
            data = os.read(0, READ_BYTES)
            if data:
                self.send(data)

    def wait(self, predicate, description, timeout=TEST_SECONDS):
        """Wait for concrete observations; time is only a test failure limit."""
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(description)
            self.pump()

    def expect(self, marker: bytes, offset=0):
        """Wait for a deterministic fixture or actual native terminal marker."""
        self.wait(lambda: marker in self.output[offset:], repr(marker))

    def gate(self, name: str, **observations):
        """Wait for an external screenshot observer to create this stage's continue file."""
        self.stage += 1
        entry = {"stage": self.stage, "name": name, **observations}
        self.result["stages"].append(entry)
        self.record("stage.json", entry)
        self.record("result.json", self.result)
        (self.directory / "output.bin").write_bytes(self.output)
        gate = self.directory / f"continue-{self.stage}"
        self.wait(gate.exists, f"screenshot gate {self.stage}: {name}", GATE_SECONDS)

    def command(self, command: str):
        """Submit through the real CLI and wait for the next native primary prompt."""
        offset = len(self.output)
        self.send(command.encode() + b"\r")
        self.expect(PROMPT, offset)

    def scenario(self):
        """Expose each screen for independent GUI inspection and assert actual files."""
        self.expect(PROMPT)
        if self.demo:
            self.expect(b"ish stream | native shell | F2 popup")
        self.gate("primary-bar" if self.demo else "primary-without-ui")
        if self.demo:
            offset = len(self.output)
            self.send(b"\x1bOQ")
            self.expect(b"Plugin popup", offset)
            self.send("팝업 값".encode())
            self.gate("popup")
            close_offset = len(self.output)
            self.send(b"\x1b")
            self.expect(PROMPT, close_offset)
        offset = len(self.output)
        self.send(b"foreach stream_item (one two)\r")
        self.expect(b"STREAM_MORE", offset)
        self.gate("native-continuation-without-ui")
        self.send(b"echo $stream_item >> loop-result\rend\r")
        self.wait((self.home / "loop-result").exists, "foreach file")
        self.expect(PROMPT, offset)
        assert (self.home / "loop-result").read_text() == "one\ntwo\n"
        self.command(
            "cat > heredoc-result << END\n  spaced 한글\nish_exit\npython\nEND"
        )
        assert (self.home / "heredoc-result").read_text() == (
            "  spaced 한글\nish_exit\npython\n"
        )
        offset = len(self.output)
        self.send(b"vim -Nu NONE -n vim-result\r")
        self.expect(b"\x1b[?1049h", offset)
        self.expect(b"vim-result", offset)
        self.gate("vim-without-ui")
        self.send(b"iSTREAM_VIM_RESULT\x1b:wq\r")
        self.wait((self.home / "vim-result").exists, "Vim file")
        self.expect(PROMPT, offset)
        assert (self.home / "vim-result").read_text() == "STREAM_VIM_RESULT\n"
        offset = len(self.output)
        image_ready = self.home / "image-ready"
        command = shlex.join(
            [
                sys.executable,
                str(ROOT / "tests/stream_fixture.py"),
                "image",
                str(image_ready),
            ]
        )
        self.send(command.encode() + b"\r")
        self.wait(image_ready.exists, "native SIXEL fixture")
        self.expect(b"Native image program", offset)
        replies = json.loads((self.home / "host-replies.json").read_text())
        assert replies["cpr"] == [15, 1], replies
        assert replies["dsr"], replies
        assert replies["margins"] == [1, self.sizes[-1][1]], replies
        assert replies["size"] == self.sizes[-1], replies
        self.result["host_protocol"] = replies
        self.gate("sixel-native-program-without-ui")
        self.send(b"q")
        self.expect(PROMPT, offset)
        self.gate("resize-and-primary-return", initial_size=self.sizes[0])
        self.command("echo finished > host-result")
        self.wait((self.home / "host-result").exists, "final native file")
        assert (self.home / "host-result").read_text() == "finished\n"
        self.exiting = True
        self.send(b"exit\r")
        self.wait(lambda: self.exited, "normal ish exit")
        _, exit_status = os.waitpid(self.pid, 0)
        self.pid = None
        returncode = os.waitstatus_to_exitcode(exit_status)
        assert returncode == 0, returncode
        self.result.update(
            status="passed",
            returncode=returncode,
            native_files=["loop-result", "heredoc-result", "vim-result", "host-result"],
            sizes=self.sizes,
            resized=any(size != self.sizes[0] for size in self.sizes),
        )

    def close(self):
        """Restore the actual host's tty settings and reap only the test CLI."""
        signal.signal(signal.SIGWINCH, self.original_resize)
        termios.tcsetattr(0, termios.TCSANOW, self.saved)
        if self.fd is not None:
            os.close(self.fd)
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    break
                time.sleep(0.02)
            else:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        (self.directory / "output.bin").write_bytes(self.output)
        self.result["parent_termios_restored"] = termios.tcgetattr(0) == self.saved
        self.record("result.json", self.result)


def main():
    """Run only inside a real host terminal launched explicitly by the test owner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    runner = HostRun(args.directory, args.demo)
    try:
        runner.start()
        runner.scenario()
    except Exception:
        runner.result.update(status="failed", error=traceback.format_exc())
        return 1
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
