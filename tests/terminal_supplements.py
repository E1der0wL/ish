"""Shared PTY/host evidence for supplemental input and sustained output."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from terminal_scenarios import Scenario

CHUNKS = 8
CHUNK_ROWS = 160
SNAPSHOT_KEY = b"\x1b[20~"
PROBE = r"""import json
from pathlib import Path

PLUGIN_META = {"version": "1"}
TERMINAL_API = 1

def setup(api):
    @api.bind("f9")
    def snapshot(event):
        terminal = api.owner.vt
        previous = terminal.frame()
        rows = []
        terminal.scroll_top()
        try:
            while True:
                frame = terminal.frame()
                start = max(0, len(rows) - frame.viewport_offset)
                rows.extend("".join(text for _, text in line).rstrip()
                            for line in frame.lines[start:])
                if frame.viewport_active:
                    break
                terminal.scroll(len(frame.lines))
            Path("history-snapshot.json").write_text(json.dumps(rows))
        finally:
            if previous.viewport_active:
                terminal.scroll_bottom()
            else:
                terminal.scroll_top()
                terminal.scroll(previous.viewport_offset)
            api.owner.app.invalidate()
"""
WRITER = r"""import os
import time
from pathlib import Path

for chunk in range(8):
    gate = Path(f"stress-release-{chunk}")
    while not gate.exists():
        time.sleep(0.01)
    for index in range(chunk * 160, (chunk + 1) * 160):
        os.write(1, f"STRESS:{index:06d}: 한글 🙂\n".encode())
    Path(f"stress-chunk-{chunk}").write_text("done")
Path("stress-finished").write_text("done")
"""


def prepare(directory: Path):
    """Install one observation-only fixture in the ordinary shared plugin directory."""
    directory.mkdir(parents=True, exist_ok=True)
    package = directory / "plugins" / "stress_probe"
    package.mkdir(parents=True, exist_ok=True)
    (package / "stress_probe.py").write_text(PROBE, encoding="utf-8")
    (directory / "stress-writer.py").write_text(WRITER, encoding="utf-8")
    return ["--plugin-dir", str(package.parent), "--plugin", "stress_probe"]


def exercise(terminal, shell, *, resize=None):
    """Compare every retained numbered row, including output produced under popups."""
    scenario = Scenario(terminal, shell, True)
    terminal.require(terminal.at_prompt)
    terminal.submit(f"{shlex.quote(sys.executable)} stress-writer.py")
    for chunk in range(CHUNKS):
        if chunk % 2 == 0:
            scenario.open_popup()
        if resize is None:
            terminal.resize(23 + chunk % 3, 76 + chunk % 4 * 3)
        else:
            resize(chunk)
        (terminal.root / f"stress-release-{chunk}").touch()
        terminal.result(f"stress-chunk-{chunk}", b"done")
        marker = f"STRESS:{(chunk + 1) * CHUNK_ROWS - 1:06d}:"
        terminal.require(
            lambda marker=marker: any(
                marker in line for line in terminal.state().get("lines", [])
            )
        )
        if chunk % 2 == 0:
            scenario.cancel_popup()
    terminal.result("stress-finished", b"done")
    terminal.require(terminal.at_prompt)
    terminal.send(SNAPSHOT_KEY)
    snapshot = terminal.root / "history-snapshot.json"
    terminal.require(lambda: snapshot.exists() and snapshot.read_text().endswith("]"))
    observed = [
        row for row in json.loads(snapshot.read_text()) if row.startswith("STRESS:")
    ]
    expected = [f"STRESS:{index:06d}: 한글 🙂" for index in range(CHUNKS * CHUNK_ROWS)]
    if observed != expected:
        raise AssertionError(("Retained output differs", len(observed), len(expected)))
    terminal.finish()
    return {"rows": len(observed), "chunks": CHUNKS, "popups": CHUNKS // 2}
