"""Manually verify Windows Terminal mouse ownership with actual PTK and PTYs.

This is an opt-in host check, not an automated unit test. Mouse and keyboard
actions come only from the user. Clipboard content never leaves PowerShell:
only whether it contains this case's fresh sentinel is returned and recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import traceback
import tty
import uuid
from pathlib import Path

from terminal_host import HostTerminal, record
from test_terminal_cli import ROOT, Terminal

HUMAN_SECONDS = 600
CLIPBOARD_SECONDS = 15
EXIT_SECONDS = 30
MAX_INPUT_BYTES = 4096
MOUSE_MODES = frozenset((9, 1000, 1001, 1002, 1003))
MODE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
SGR = re.compile(rb"\x1b\[<([0-9]+);([0-9]+);([0-9]+)([Mm])")
CASES = (
    ("auto-native-off", "auto", False, False),
    ("auto-native-on", "auto", True, False),
    ("off-native-on", "off", True, False),
)
SEARCH_CASE = ("optional-search", "auto", False, True)

CHILD = r"""import json
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text())
saved = termios.tcgetattr(0)
received = bytearray()
deadline = time.monotonic() + settings['timeout']
try:
    tty.setraw(0)
    if settings['native_mouse']:
        os.write(1, b'\x1b[?1000h\x1b[?1006h')
    os.write(1, ('\x1b[2J\x1b[H' + settings['instructions'] + '\r\n\r\n'
                 + settings['sentinel'] + '\r\n').encode())
    Path('native-ready').write_text('ready')
    while b'\r' not in received and b'\n' not in received:
        if time.monotonic() >= deadline:
            raise TimeoutError('physical input deadline exceeded')
        if select.select([0], [], [], 0.1)[0]:
            block = os.read(0, 4096)
            if not block:
                raise EOFError('native input ended before confirmation')
            received.extend(block)
            if b'\x03' in block or b'\x04' in block:
                Path('native-cancel.bin').write_bytes(received)
                raise KeyboardInterrupt('physical check cancelled')
            if len(received) > settings['max_input_bytes']:
                raise ValueError('physical input exceeded test limit')
    Path('native-result.bin').write_bytes(received)
    Path('native-confirmed').write_text('confirmed')
    # An explicit file acknowledgement, not silence, releases the native child.
    while not Path('release-native').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('controller acknowledgement deadline exceeded')
        time.sleep(0.01)
finally:
    os.write(1, b'\x1b[?1000l\x1b[?1006l')
    termios.tcsetattr(0, termios.TCSANOW, saved)
"""


def clipboard_contains(executable, sentinel):
    """Read inside PowerShell and return only a comparison boolean, never content."""
    # Generated sentinels contain ASCII letters, digits and underscores only.
    if not re.fullmatch(r"[A-Z0-9_]+", sentinel):
        raise ValueError("invalid test sentinel")
    command = (
        "$ErrorActionPreference='Stop'; "
        "$interactionClipboard=Get-Clipboard -Raw; "
        f"if ($null -ne $interactionClipboard -and $interactionClipboard.Contains('{sentinel}')) "
        "{[Console]::Write('true')} else {[Console]::Write('false')}"
    )
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=CLIPBOARD_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "failed", "contains_sentinel": None}
    value = result.stdout.strip()
    if result.returncode != 0 or value not in (b"true", b"false"):
        return {"status": "failed", "contains_sentinel": None}
    return {"status": "checked", "contains_sentinel": value == b"true"}


def capture_modes(output):
    """Independently inspect actual host-directed DECSET/DECRST wire requests."""
    states = {}
    for match in MODE.finditer(output):
        for value in match[1].split(b";"):
            mode = int(value)
            if mode in MOUSE_MODES:
                states[str(mode)] = match[2] == b"h"
    return states


def mouse_events(data):
    """Decode final native bytes independently of the product's mouse parser."""
    return [
        {
            "button": int(match[1]),
            "column": int(match[2]),
            "row": int(match[3]),
            "action": "press" if match[4] == b"M" else "release",
        }
        for match in SGR.finditer(data)
    ]


class InteractionTerminal(HostTerminal):
    """Reuse real host relay while launching an isolated raw Python consumer."""

    def __init__(self, directory, native_home, settings, *, mouse, search):
        """Never execute a user's shell rc or mutate the Windows Terminal profile."""
        self.directory = directory
        self.saved_host = termios.tcgetattr(0)
        self.resize_handler = signal.getsignal(signal.SIGWINCH)
        self.resize_pending = False
        self.resize_gate = None
        self.host_input = bytearray()
        rows, cols = termios.tcgetwinsize(0)
        self.sizes = [[cols, rows]]
        self.popup_seen = False
        environment = {
            name: os.environ[name]
            for name in ("TERM", "COLORTERM", "WT_SESSION", "WSL_DISTRO_NAME")
            if name in os.environ
        }
        script = native_home / "native.py"
        script.write_text(CHILD, encoding="utf-8")
        request = native_home / "request.json"
        request.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        arguments = [
            "--home",
            str(native_home / "settings"),
            "--no-rc",
            "--mouse",
            mouse,
            "--shell-arg=" + str(script),
            "--shell-arg=" + str(request),
        ]
        if search:
            arguments.extend(
                (
                    "--plugin-dir",
                    str(ROOT / "examples/terminal_plugins"),
                    "--plugin",
                    "terminal_search",
                )
            )
        else:
            arguments.append("--no-plugins")
        Terminal.__init__(
            self,
            native_home,
            rows=rows,
            cols=cols,
            shell=sys.executable,
            extra_args=arguments,
            environment=environment,
            configure=False,
        )
        try:
            tty.setraw(0)
            signal.signal(signal.SIGWINCH, self._resized)
            record(
                directory,
                "environment.json",
                {
                    **environment,
                    "python": sys.version,
                    "initial_size": [cols, rows],
                    "input_method": "physical user keyboard/mouse; actual host replies relayed",
                    "temporary_home": True,
                    "mouse_policy": mouse,
                    "native_mouse_requested": settings["native_mouse"],
                    "plugins": ["terminal_search"] if search else [],
                },
            )
        except BaseException:
            self.close()
            raise

    def receive(self):
        """Never synthesize mouse, keyboard or query-response bytes."""
        super().receive()
        if self.state().get("popups", 0):
            self.popup_seen = True


def run_case(directory, case, powershell, timeout):
    """Require real host selection or real native consumption before passing."""
    name, policy, native_mouse, search = case
    directory.mkdir(parents=True, exist_ok=False)
    sentinel = "HOST_SELECTION_CHECK_" + uuid.uuid4().hex.upper()
    clicking = native_mouse and policy == "auto"
    if search:
        sentinel = "SEARCH_TARGET_" + uuid.uuid4().hex.upper()
        instructions = (
            "[선택 검색 검사] Ctrl+F를 누르고 SEARCH_TARGET을 입력한 뒤 Enter를 누르세요.\r\n"
            "검색 결과를 확인하고 Esc로 닫은 다음 Enter를 한 번 더 누르세요."
        )
    elif clicking:
        instructions = (
            "[2/3 프로그램 마우스 검사] 아래 문구를 마우스 왼쪽 버튼으로 한 번 클릭하세요.\r\n"
            "버튼을 놓은 다음 Enter를 누르세요. Shift는 누르지 마세요."
        )
    else:
        number = 1 if policy == "auto" else 3
        instructions = (
            f"[{number}/3 호스트 선택 검사] Shift를 누르지 않고 아래 HOST_SELECTION_CHECK 문구 전체를 드래그하세요.\r\n"
            "선택 영역이 강조된 상태에서 터미널에 설정된 복사 단축키로 복사하세요.\r\n"
            "모든 키를 놓은 뒤 Enter를 누르세요. 붙여넣지는 마세요."
        )
    result = {
        "name": name,
        "status": "running",
        "mouse_policy": policy,
        "native_mouse_requested": native_mouse,
        "clipboard_content_recorded": False,
        "pixel_verification": "not performed",
        "physical_input": "required; no synthetic input",
        "expected_sentinel": sentinel,
        "stages": [],
        "returncode": None,
        "outer_termios_restored": None,
        "host_termios_restored": None,
    }
    record(directory, "result.json", result)
    terminal = None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ish-vt-physical-") as temporary:
        native_home = Path(temporary)
        try:
            terminal = InteractionTerminal(
                directory,
                native_home,
                {
                    "native_mouse": native_mouse,
                    "sentinel": sentinel,
                    "instructions": instructions,
                    "timeout": timeout,
                    "max_input_bytes": MAX_INPUT_BYTES,
                },
                mouse=policy,
                search=search,
            )
            terminal.require(
                (native_home / "native-ready").exists, timeout=EXIT_SECONDS
            )
            result["stages"].append("native-consumer-ready")
            record(directory, "result.json", result)
            terminal.require((native_home / "native-confirmed").exists, timeout=timeout)
            result["stages"].append("physical-enter-confirmed")
            data = (native_home / "native-result.bin").read_bytes()
            (directory / "native-result.bin").write_bytes(data)
            events = mouse_events(data)
            modes = capture_modes(terminal.output)
            result.update(
                consumer_hex=data.hex(),
                consumer_bytes=len(data),
                sgr_events=events,
                host_mouse_mode_requests_at_confirmation=modes,
                host_capture_requested=any(modes.values()),
                diagnostic_mouse_capture=terminal.state().get("mouse_capture"),
                popup_seen=terminal.popup_seen,
            )
            if clicking:
                if not any(
                    event["action"] == "press" and event["button"] == 0
                    for event in events
                ):
                    raise AssertionError(
                        "일반 왼쪽 클릭의 SGR 바이트가 native PTY 소비자에 도착하지 않았습니다"
                    )
                if not any(modes.values()):
                    raise AssertionError(
                        "실제 호스트에 마우스 캡처 요청을 보낸 증거가 없습니다"
                    )
                if SGR.sub(b"", data) not in (b"\r", b"\n", b"\r\n"):
                    raise AssertionError(
                        "클릭과 Enter 이외의 예상하지 않은 native 입력이 있습니다"
                    )
                result["stages"].append("native-sgr-consumer-confirmed")
            elif search:
                if not terminal.popup_seen or terminal.state().get("popups", 0):
                    raise AssertionError(
                        "검색 팝업의 실제 열림과 닫힘을 모두 관찰하지 못했습니다"
                    )
                if data not in (b"\r", b"\n", b"\r\n"):
                    raise AssertionError(
                        "검색 단축키 또는 입력이 native 프로그램에 유출됐습니다"
                    )
                result["stages"].append("physical-search-open-close-confirmed")
            else:
                result["clipboard"] = clipboard_contains(powershell, sentinel)
                if result["clipboard"]["contains_sentinel"] is not True:
                    raise AssertionError(
                        "Windows 클립보드에 이번 단계의 고유 선택 문구가 없습니다"
                    )
                if any(modes.values()):
                    raise AssertionError(
                        "호스트 선택 단계에서 마우스 캡처 요청이 켜져 있습니다"
                    )
                if data not in (b"\r", b"\n", b"\r\n"):
                    raise AssertionError(
                        "호스트 선택 마우스가 native 프로그램에 전달됐습니다"
                    )
                result["stages"].append("host-selection-and-clipboard-confirmed")
            (native_home / "release-native").touch()
            terminal.require(terminal.exited, timeout=EXIT_SECONDS)
            terminal.receive()
            result.update(
                returncode=terminal.status,
                outer_termios_restored=termios.tcgetattr(terminal.fd) == terminal.attrs,
                sizes=terminal.sizes,
            )
            if terminal.status != 0 or not result["outer_termios_restored"]:
                raise AssertionError("검사 프로그램 종료 또는 PTY 복원이 실패했습니다")
            result["status"] = "passed"
            result["stages"].append("normal-exit-and-outer-restore")
        except (Exception, KeyboardInterrupt):
            result.update(status="failed", error=traceback.format_exc())
            cancelled = native_home / "native-cancel.bin"
            if cancelled.exists():
                data = cancelled.read_bytes()
                (directory / "native-cancel.bin").write_bytes(data)
                result["cancelled_consumer_hex"] = data.hex()
        finally:
            (native_home / "release-native").touch()
            if terminal is not None:
                try:
                    if terminal.status is not None:
                        result["outer_termios_restored"] = (
                            termios.tcgetattr(terminal.fd) == terminal.attrs
                        )
                    terminal.close()
                    result["returncode"] = terminal.status
                    result["host_termios_restored"] = (
                        termios.tcgetattr(0) == terminal.saved_host
                    )
                    if not result["host_termios_restored"]:
                        raise AssertionError("실제 호스트 termios 복원이 실패했습니다")
                except Exception:
                    result.update(status="failed", cleanup_error=traceback.format_exc())
            result["seconds"] = round(time.monotonic() - started, 3)
            record(directory, "result.json", result)
    return result


def main():
    """Run three manual mouse cases; optional search never changes the base product."""
    parser = argparse.ArgumentParser(
        description="실제 Windows Terminal/WSL에서 마우스 선택과 native 입력을 검사합니다."
    )
    parser.add_argument("directory", type=Path, help="새 결과 디렉터리")
    parser.add_argument(
        "--with-search",
        action="store_true",
        help="선택한 검색 플러그인의 물리 키 검사도 진행",
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=[case[0] for case in CASES],
        help="지정한 마우스 단계만 실행 (이전 결과는 보존)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=HUMAN_SECONDS,
        help="단계별 사람 입력 실패 제한(초)",
    )
    args = parser.parse_args()
    if (
        not os.isatty(0)
        or not os.isatty(1)
        or not os.environ.get("WT_SESSION")
        or not os.environ.get("WSL_DISTRO_NAME")
    ):
        parser.error("Windows Terminal의 실제 WSL 터미널에서 실행해야 합니다")
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        parser.error("클립보드 결과를 확인할 powershell.exe가 없습니다")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout은 1 이상 3600 이하의 초 단위 정수여야 합니다")
    directory = args.directory.resolve()
    if directory.exists():
        parser.error("기존 증거를 덮어쓰지 않도록 새 결과 디렉터리를 지정하세요")
    if (
        args.with_search
        and not (ROOT / "examples/terminal_plugins/terminal_search").is_dir()
    ):
        parser.error("선택 검색 플러그인이 준비되지 않았습니다")
    directory.mkdir(parents=True)
    cases = tuple(case for case in CASES if args.case is None or case[0] in args.case)
    cases += (SEARCH_CASE,) if args.with_search else ()
    summary = {
        "status": "running",
        "expected": len(cases),
        "cases": [],
        "clipboard_content_recorded": False,
    }
    record(directory, "summary.json", summary)
    print(
        "이 검사는 실제 마우스·키보드 조작이 필요합니다. 화면 안내에 따라 진행해 주세요.",
        flush=True,
    )
    for case in cases:
        result = run_case(directory / case[0], case, powershell, args.timeout)
        summary["cases"].append({"name": case[0], "status": result["status"]})
        record(directory, "summary.json", summary)
        if result["status"] != "passed":
            summary["status"] = "failed"
            record(directory, "summary.json", summary)
            print(
                f"검사 실패: {case[0]}. 기록: {directory / case[0] / 'result.json'}",
                flush=True,
            )
            return 1
    summary["status"] = "passed"
    record(directory, "summary.json", summary)
    print(
        f"실제 마우스·입력 검사 {len(cases)}개가 통과했습니다. 기록: {directory}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
