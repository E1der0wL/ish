"""Verify explicit RGB output through the actual host without claiming pixel accuracy."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import termios
import traceback
from pathlib import Path

from terminal_host import HostTerminal, record

PAYLOAD = (
    b"\x1b[38;2;17;34;51mRGB_FOREGROUND_112233\x1b[0m\r\n"
    b"\x1b[38;2;123;45;67mRGB_FOREGROUND_7B2D43\x1b[0m\r\n"
    b"\x1b[48;2;20;60;120mRGB_BACKGROUND_143C78\x1b[0m\r\n"
)
EXPECTED_SGR = (b"38;2;17;34;51", b"38;2;123;45;67", b"48;2;20;60;120")
SGR = re.compile(rb"\x1b\[([0-9;]*)m")


def run(directory):
    """Relay actual host replies; compare emitted RGB with independent literals."""
    directory.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "running",
        "color_depth_policy": "24",
        "scope": "native RGB bytes -> actual PTK renderer -> actual host output",
        "pixel_verification": "not performed",
        "physical_input_verification": "not performed",
        "other_host_capabilities": "unchanged; explicitly selecting RGB grants no other feature",
    }
    record(directory, "result.json", result)
    terminal = None
    try:
        terminal = HostTerminal(
            directory,
            shell="tcsh",
            plugins=False,
            extra_args=("--no-rc", "--color-depth", "24"),
        )
        terminal.require(terminal.at_prompt)
        script = terminal.root / "color-producer.py"
        script.write_text(
            "import os\nfrom pathlib import Path\n"
            f"payload = {PAYLOAD!r}\n"
            "os.write(1, payload)\n"
            "Path('color-source.bin').write_bytes(payload)\n",
            encoding="utf-8",
        )
        before = len(terminal.output)
        terminal.submit(shlex.quote(sys.executable) + " " + shlex.quote(str(script)))
        terminal.result("color-source.bin", PAYLOAD)
        terminal.require(
            lambda: all(
                terminal.frame_has(marker)
                for marker in (
                    "RGB_FOREGROUND_112233",
                    "RGB_FOREGROUND_7B2D43",
                    "RGB_BACKGROUND_143C78",
                )
            )
        )
        controls = SGR.findall(bytes(terminal.output[before:]))
        verified = {
            expected.decode("ascii"): any(
                b";" + expected + b";" in b";" + control + b";" for control in controls
            )
            for expected in EXPECTED_SGR
        }
        state = terminal.state()
        result.update(
            literal_rgb_controls=verified,
            source_bytes=len(PAYLOAD),
            native_result_exact=(terminal.root / "color-source.bin").read_bytes()
            == PAYLOAD,
            resolved_color_depth=state.get("color_depth"),
            host=state.get("host"),
            host_adapter=state.get("host_adapter"),
        )
        if (
            not all(verified.values())
            or result["resolved_color_depth"] != "DEPTH_24_BIT"
        ):
            raise AssertionError(
                "Explicit RGB channels were not preserved in actual host-directed SGR"
            )
        terminal.finish()
        result.update(
            status="passed",
            returncode=terminal.status,
            outer_termios_restored=termios.tcgetattr(terminal.fd) == terminal.attrs,
        )
    except (Exception, KeyboardInterrupt):
        result.update(status="failed", error=traceback.format_exc())
    finally:
        if terminal is not None:
            try:
                terminal.close()
                result["host_termios_restored"] = (
                    termios.tcgetattr(0) == terminal.saved_host
                )
                if not result["host_termios_restored"]:
                    raise AssertionError("Actual host termios was not restored")
            except Exception:
                result.update(status="failed", cleanup_error=traceback.format_exc())
        record(directory, "result.json", result)
    return result


def main():
    """Run an automatic, separately recorded RGB check in an existing host window."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("This check requires an actual interactive host terminal")
    directory = args.directory.resolve()
    if directory.exists():
        parser.error("Choose a new result directory; existing evidence is preserved")
    result = run(directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
