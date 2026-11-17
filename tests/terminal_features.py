"""Verify feature policies with actual host responses and independent output bytes.

This check observes protocol output, native input files and restoration. It does
not infer pixels, link activation or physical keyboard behavior from those bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import termios
import traceback
from pathlib import Path

from terminal_host import HostTerminal, record
from terminal_scenarios import SHELLS
from test_terminal_cli import STARTUP_DEADLINE

POLICIES = ("on", "off")
SELECTED_FEATURES = ("hyperlinks", "extended_styles", "overline", "cursor_color")
AUTO_FEATURES = (
    "kitty_graphics",
    "synchronized_output",
    "enhanced_keyboard",
    "pixel_mouse",
    "clipboard_protocol",
    "scheme_notifications",
)
PAYLOAD = (
    b"\x1b]8;;https://example.invalid/ish-feature-check\x1b\\FEATURE_LINK\x1b]8;;\x1b\\\r\n"
    b"\x1b[4:3m\x1b[58:2::17:34:51mFEATURE_UNDERLINE\x1b[0m\r\n"
    b"\x1b[53mFEATURE_OVERLINE\x1b[0m\r\n"
    b"\x1b]12;rgb:11/22/33\x1b\\"
)
EXPECTED_OUTPUT = {
    "hyperlinks": b"\x1b]8;;https://example.invalid/ish-feature-check\x1b\\",
    "extended_styles": b"\x1b[4:3m",
    "underline_color": b"\x1b[58:2::17:34:51m",
    "overline": b"\x1b[53m",
    "cursor_color": b"\x1b]12;rgb:11/22/33\x1b\\",
}
INPUT = "feature input 한글 |done|".encode()
READER = r"""import os
import termios
import tty
from pathlib import Path

saved = termios.tcgetattr(0)
received = bytearray()
try:
    tty.setraw(0)
    payload = PAYLOAD
    os.write(1, payload)
    Path('feature-source.bin').write_bytes(payload)
    Path('feature-ready').touch()
    while not received.endswith(b'|done|'):
        block = os.read(0, 4096)
        if not block:
            raise EOFError('Native input ended before the sentinel')
        received.extend(block)
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
Path('feature-input.bin').write_bytes(received)
"""


def run_case(directory, shell, policy):
    """Check selected protocols without manufacturing any host capability reply."""
    directory.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "running",
        "shell": shell,
        "policy": policy,
        "plugins": False,
        "scope": "actual host replies, emitted feature protocols, native stdin file",
        "pixel_verification": "not performed",
        "physical_input_verification": "not performed",
        "cursor_color_readback": "not performed; startup restoration prerequisite and output bytes only",
    }
    record(directory, "result.json", result)
    terminal = None
    try:
        arguments = ["--no-rc"]
        for name in SELECTED_FEATURES:
            arguments.extend(("--feature", f"{name}={policy}"))
        terminal = HostTerminal(
            directory, shell=shell, plugins=False, extra_args=arguments
        )
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        script = terminal.root / "feature-reader.py"
        script.write_text(
            READER.replace("payload = PAYLOAD", f"payload = {PAYLOAD!r}"),
            encoding="utf-8",
        )
        before = len(terminal.output)
        terminal.submit(shlex.quote(sys.executable) + " " + shlex.quote(str(script)))
        terminal.result("feature-source.bin", PAYLOAD)
        terminal.require(lambda: (terminal.root / "feature-ready").exists())
        terminal.require(
            lambda: all(
                terminal.frame_has(marker)
                for marker in ("FEATURE_LINK", "FEATURE_UNDERLINE", "FEATURE_OVERLINE")
            )
        )
        state = terminal.state()
        features = state["features"]
        if policy == "on":
            expected_wire = [
                wire
                for name, wire in EXPECTED_OUTPUT.items()
                if name != "cursor_color" or features[name]["active"]
            ]
            # An outer PTY read may split the last cursor OSC from the visible
            # text. Only a complete observed control satisfies the assertion.
            terminal.require(
                lambda: all(wire in terminal.output[before:] for wire in expected_wire)
            )
        observed = {
            name: wire in terminal.output[before:]
            for name, wire in EXPECTED_OUTPUT.items()
        }
        result.update(
            features=features,
            host=state.get("host"),
            host_adapter=state.get("host_adapter"),
            literal_controls=observed,
            clipboard_policy=state.get("clipboard_policy"),
            no_added_ui=not state.get("popups") and state["rows"] == terminal.rows,
        )
        for name in SELECTED_FEATURES:
            if features[name]["policy"] != policy:
                raise AssertionError(
                    ("Requested policy was lost", name, features[name])
                )
            expected = policy == "on"
            if name == "cursor_color" and policy == "on":
                # Explicit on retains the independently observed restoration
                # prerequisite; unsupported hosts must not be falsely enabled.
                expected = features[name]["active"]
            elif features[name]["active"] != expected:
                raise AssertionError(("Selected formatting gate differs", name))
            if observed[name] != expected:
                raise AssertionError(("Actual feature bytes differ", name, observed))
        if observed["underline_color"] != (policy == "on"):
            raise AssertionError("Underline RGB was not gated with extended styles")
        for name in AUTO_FEATURES:
            feature = features[name]
            if feature["policy"] != "auto" or (
                feature["active"] and feature["support"] != "supported"
            ):
                raise AssertionError(("Automatic support was inferred", name, feature))
        if result["clipboard_policy"] != {"read": False, "write": True}:
            raise AssertionError(
                "Host transport selection changed clipboard permission"
            )
        if not result["no_added_ui"]:
            raise AssertionError("Unselected UI reserved part of the native screen")
        terminal.send(INPUT)
        terminal.result("feature-input.bin", INPUT)
        terminal.require(terminal.at_prompt)
        terminal.finish()
        result.update(
            status="passed",
            returncode=terminal.status,
            native_input_exact=True,
            host_response_leaked_to_stdin=False,
            outer_termios_restored=termios.tcgetattr(terminal.fd) == terminal.attrs,
        )
    except (Exception, KeyboardInterrupt):
        result.update(status="failed", error=traceback.format_exc())
    finally:
        if terminal is not None:
            try:
                terminal.close()
                if "literal_controls" in result:
                    final_controls = {
                        name: wire in terminal.output[before:]
                        for name, wire in EXPECTED_OUTPUT.items()
                    }
                    result["literal_controls_after_exit"] = final_controls
                    if policy == "off" and any(final_controls.values()):
                        raise AssertionError(
                            "A disabled feature was emitted after the initial frame"
                        )
                restored = termios.tcgetattr(0) == terminal.saved_host
                result["host_termios_restored"] = restored
                if not restored:
                    raise AssertionError("Actual host termios was not restored")
            except Exception:
                result.update(status="failed", cleanup_error=traceback.format_exc())
        record(directory, "result.json", result)
    return result


def main():
    """Run each native shell with formatting enabled and disabled, retaining failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("This check requires an actual interactive host terminal")
    directory = args.directory.resolve()
    if directory.exists():
        parser.error("Choose a new result directory; existing evidence is preserved")
    directory.mkdir(parents=True)
    results = []
    for shell in SHELLS:
        for policy in POLICIES:
            results.append(run_case(directory / f"{shell}-{policy}", shell, policy))
            record(directory, "summary.json", {"status": "running", "cases": results})
    passed = all(case["status"] == "passed" for case in results)
    summary = {"status": "passed" if passed else "failed", "cases": results}
    record(directory, "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
