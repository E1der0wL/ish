"""Observe physical Korean IME and Alt/Escape in the actual Windows host."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import traceback
from pathlib import Path

from terminal_host import HostTerminal, record
from terminal_scenarios import Scenario
from test_terminal_cli import STARTUP_DEADLINE

EXPECTED = "한글 입력 확인"
DEADLINE = 900
CHILD = r"""import os,sys,termios,tty
from pathlib import Path
print("팝업과 그 다음 입력 줄에 한글 IME로 '한글 입력 확인'을 직접 입력하고 Enter를 누르세요.", flush=True)
Path('keyboard-ready').touch()
if '--keys-only' not in sys.argv:
    line = sys.stdin.readline()
    Path('keyboard-ime.txt').write_text(line, encoding='utf-8')
saved = termios.tcgetattr(0)
try:
    tty.setraw(0)
    os.write(1, "\r\n이제 영문 입력으로 전환한 뒤 Alt+x, Esc, Enter를 차례로 누르세요.\r\n".encode())
    Path('keyboard-raw-ready').touch()
    data = bytearray()
    while not data.endswith(b'\r'):
        data.extend(os.read(0, 128))
        if len(data) > 4096:
            raise ValueError('test input limit exceeded')
    Path('keyboard-keys.bin').write_bytes(data)
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
"""


def main():
    """Check final native files and popup return values, not key simulations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--keys-only", action="store_true")
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("An actual interactive terminal is required")
    directory = args.directory.resolve()
    directory.mkdir(parents=True)
    result = {"status": "running", "input_method": "physical keyboard and host IME"}
    terminal = None
    try:
        terminal = HostTerminal(directory, shell="bash", plugins=not args.keys_only)
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        (terminal.root / "keyboard.py").write_text(CHILD, encoding="utf-8")
        suffix = " --keys-only" if args.keys_only else ""
        terminal.submit(f"{shlex.quote(sys.executable)} keyboard.py{suffix}")
        terminal.require((terminal.root / "keyboard-ready").exists)
        if not args.keys_only:
            scenario = Scenario(terminal, "bash", True)
            scenario.open_popup()
            terminal.require(lambda: bool(terminal.state().get("popups")))
            record(directory, "stage.json", {"stage": "popup-ime"})
            popup = terminal.root / "popup-value-1"
            terminal.require(popup.exists, DEADLINE)
            if popup.read_text() != EXPECTED:
                raise AssertionError("Physical IME popup value did not match")
            result["popup_ime"] = True
            record(directory, "stage.json", {"stage": "native-ime"})
            line = terminal.root / "keyboard-ime.txt"
            terminal.require(line.exists, DEADLINE)
            if line.read_text() != EXPECTED + "\n":
                raise AssertionError("Physical IME native input did not match")
            result["native_ime"] = True
        terminal.require((terminal.root / "keyboard-raw-ready").exists)
        record(directory, "stage.json", {"stage": "alt-escape"})
        keys = terminal.root / "keyboard-keys.bin"
        terminal.require(keys.exists, DEADLINE)
        result["keys_hex"] = keys.read_bytes().hex()
        if keys.read_bytes() != b"\x1bx\x1b\r":
            raise AssertionError(
                ("Physical Alt/Escape bytes differ", result["keys_hex"])
            )
        terminal.require(terminal.at_prompt)
        terminal.finish()
        result.update(status="passed", alt_escape=True)
    except Exception:
        result.update(status="failed", error=traceback.format_exc())
    finally:
        if terminal is not None:
            terminal.close()
        record(directory, "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
