"""Compare direct colors with ish output using the actual host's color readback."""

from __future__ import annotations

import argparse
import math
import os
import re
import select
import shlex
import sys
import termios
import time
import traceback
import tty
from pathlib import Path

from terminal_host import HostTerminal, record
from terminal_restore import ATTRIBUTES, DEADLINE, write
from test_terminal_cli import STARTUP_DEADLINE

CSI = b"\x1b["
ST = b"\x1b\\"
SGR = re.compile(rb"\x1b\[[0-9:;]*m")
SAMPLES = {
    b"SWATCH_RGB_FG": b"38;2;123;45;67",
    b"SWATCH_RGB_BG": b"48;2;20;60;120",
    b"SWATCH_THEME_RED": b"31",
    b"SWATCH_BOLD_RED": b"1;31",
    b"SWATCH_BOLD_BLUE": b"1;34",
    b"SWATCH_INDEX_BG": b"48;5;166",
    b"SWATCH_DEFAULT": b"0",
}
# One unique grapheme identifies each swatch even when PTK emits decoration
# escapes between every two characters of its ASCII label.
TAGS = {marker: chr(0x2460 + i).encode() for i, marker in enumerate(SAMPLES)}
PAYLOAD = b"".join(
    CSI + b"0;" + code + b"m" + TAGS[marker] + marker + CSI + b"0m\r\n"
    for marker, code in SAMPLES.items()
)


def query(control, pattern):
    """Wait for an actual response on the dedicated test terminal."""
    write(control)
    received = bytearray()
    deadline = time.monotonic() + DEADLINE
    while (remaining := deadline - time.monotonic()) > 0:
        if select.select([0], [], [], remaining)[0]:
            received.extend(os.read(0, 65536))
            match = pattern.search(received)
            if match:
                return match
    raise TimeoutError((control, bytes(received)))


def host_rgb(key, cache):
    """Resolve default and indexed colors from this host's actual OSC answers."""
    if key not in cache:
        report = re.compile(
            rb"\x1b\]"
            + re.escape(key)
            + rb";rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)"
            + rb"(?:\x07|\x1b\\)"
        )
        answer = query(b"\x1b]" + key + b";?" + ST, report)
        cache[key] = [
            round(int(channel, 16) * 255 / (16 ** len(channel) - 1))
            for channel in answer.groups()
        ]
    return cache[key]


def colors(attributes, palette):
    """Decode Windows Terminal's canonical SGR report, preserving defaults."""
    values = {"fg": b"10", "bg": b"11"}
    for field in attributes.split(b";"):
        if b":" in field:
            layer, mode, *parts = field.split(b":")
            if layer in (b"38", b"48"):
                if mode == b"2" and len(parts) >= 3:
                    value = [int(channel) for channel in parts[-3:]]
                elif mode == b"5" and len(parts) == 1:
                    value = b"4;" + parts[0]
                else:
                    raise AssertionError(("Unrecognized host color", field))
                values["fg" if layer == b"38" else "bg"] = value
        elif field:
            code = int(field)
            if code in (38, 48):
                raise AssertionError(("Unexpected host color syntax", attributes))
            for start, layer, first in (
                (30, "fg", 0),
                (40, "bg", 0),
                (90, "fg", 8),
                (100, "bg", 8),
            ):
                if start <= code < start + 8:
                    values[layer] = f"4;{first + code - start}".encode()
    return {
        layer: {
            "rgb": host_rgb(value, palette) if isinstance(value, bytes) else value,
            "default": value in (b"10", b"11"),
        }
        for layer, value in values.items()
    }


def compare_host(output, observed_palette):
    """Replay foreground/background attributes from the bytes actually sent to WT."""
    attributes_query = b"\x1bP$qm" + ST
    saved = termios.tcgetattr(0)
    tty.setraw(0)
    initial = None
    palette, samples = {}, {}
    try:
        initial = query(attributes_query, ATTRIBUTES)[1]
        if len(observed_palette) != 256:
            raise AssertionError("Incomplete virtual palette")
        for index, observed in enumerate(observed_palette):
            expected = host_rgb(f"4;{index}".encode(), palette)
            if observed != expected:
                raise AssertionError(
                    ("Host palette mismatch", index, expected, observed)
                )
        direct_colors = {
            b"SWATCH_RGB_FG": ([123, 45, 67], None),
            b"SWATCH_RGB_BG": (None, [20, 60, 120]),
            b"SWATCH_THEME_RED": (palette[b"4;1"], None),
            b"SWATCH_BOLD_RED": (palette[b"4;1"], None),
            b"SWATCH_BOLD_BLUE": (palette[b"4;4"], None),
            b"SWATCH_INDEX_BG": (None, palette[b"4;166"]),
            b"SWATCH_DEFAULT": (None, None),
        }
        for marker, code in SAMPLES.items():
            # Record actual PTK styles at the marker. Reapply their exact SGRs
            # without replaying unrelated terminal commands or moving the screen.
            position = output.index(TAGS[marker])
            emitted = b"".join(SGR.findall(output[:position]))
            pair = {}
            for label, style in (("direct", CSI + code + b"m"), ("ish", emitted)):
                write(CSI + b"0m" + style + marker)
                observed = query(attributes_query, ATTRIBUTES)[1]
                pair[label] = {
                    "host_sgr": observed.decode(),
                    **colors(observed, palette),
                }
                write(CSI + b"0m   ")
            write(b"\r\n")
            direct = [pair["direct"][side] for side in ("fg", "bg")]
            if (
                tuple(None if c["default"] else c["rgb"] for c in direct)
                != direct_colors[marker]
            ):
                raise AssertionError(
                    ("Unexpected direct host reference", marker, direct)
                )
            pair["equal"] = all(
                pair["direct"][k] == pair["ish"][k] for k in ("fg", "bg")
            )
            if marker in (b"SWATCH_THEME_RED", b"SWATCH_BOLD_RED", b"SWATCH_BOLD_BLUE"):
                if pair["direct"]["host_sgr"] != pair["ish"]["host_sgr"]:
                    raise AssertionError(
                        ("Host palette or intensity differs", marker, pair)
                    )
            samples[marker.decode()] = pair
    finally:
        if initial is not None:
            write(CSI + b"0m" + CSI + initial + b"m")
        termios.tcsetattr(0, termios.TCSANOW, saved)
    return samples


def run(directory, policy):
    """Draw the same test swatches through a real shell and on its outer host."""
    directory.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "running",
        "policy_argument": policy,
        "scope": "real Windows Terminal SGR readback, OSC palette and default colors",
        "pixel_verification": "not performed",
    }
    record(directory, "result.json", result)
    terminal = None
    try:
        args = (
            ("--no-rc",)
            if policy == "default"
            else ("--no-rc", "--color-depth", policy)
        )
        terminal = HostTerminal(directory, shell="tcsh", plugins=False, extra_args=args)
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        script = terminal.root / "color-producer.py"
        script.write_text(f"import os\nos.write(1, {PAYLOAD!r})\n", encoding="utf-8")
        before = len(terminal.output)
        terminal.submit(shlex.join([sys.executable, str(script)]))
        terminal.require(lambda: all(terminal.frame_has(m.decode()) for m in SAMPLES))
        output = bytes(terminal.output[before:])
        terminal.finish()
        state = terminal.state()
        result.update(
            resolved_color_depth=state.get("color_depth"),
            reported_policy=state.get("color_depth_policy"),
            host_observation=state["host"].get("color_depth"),
            palette_entries_observed=sum(
                color is not None for color in state["host"]["appearance"]["palette"]
            ),
        )
        terminal.close()
        result["host_termios_restored"] = termios.tcgetattr(0) == terminal.saved_host
        terminal = None
        samples = compare_host(output, state["host"]["appearance"]["palette"])
        result.update(samples=samples, palette_entries_compared=256)
        if not result["host_termios_restored"]:
            raise AssertionError("Host terminal settings changed")
        if policy == "8":
            palette = state["host"]["appearance"]["palette"]
            for key, side in (("SWATCH_RGB_FG", "fg"), ("SWATCH_RGB_BG", "bg")):
                pair = samples[key]
                source = pair["direct"][side]["rgb"]
                actual = pair["ish"][side]["rgb"]
                nearest = min(math.dist(source, color) for color in palette)
                if (
                    math.dist(source, actual) != nearest
                    or ":2:" in pair["ish"]["host_sgr"]
                ):
                    raise AssertionError(
                        ("Expected closest host palette color", key, actual)
                    )
            if result["resolved_color_depth"] != "DEPTH_8_BIT" or not all(
                samples[k]["equal"]
                for k in ("SWATCH_THEME_RED", "SWATCH_INDEX_BG", "SWATCH_DEFAULT")
            ):
                raise AssertionError("Explicit 256-color selection did not take effect")
        elif result["resolved_color_depth"] != "DEPTH_24_BIT" or not all(
            sample["equal"] for sample in samples.values()
        ):
            raise AssertionError("Truecolor output differs from direct host colors")
        result["status"] = "passed"
    except (Exception, KeyboardInterrupt):
        result.update(status="failed", error=traceback.format_exc())
    finally:
        if terminal is not None:
            terminal.close()
        record(directory, "result.json", result)
    return result


def main():
    """Run from Windows Terminal/WSL using a fresh evidence directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--policy", choices=("default", "8", "24"), default="default")
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("This check requires an actual interactive host terminal")
    directory = args.directory.resolve()
    if directory.exists():
        parser.error("Choose a new result directory; existing evidence is preserved")
    result = run(directory, args.policy)
    print(f"{result['status']}: {directory / 'result.json'}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
