"""Verify an installed ish distribution with the maintained native PTY scenarios.

Run with a development Python or an isolated test-support directory. The checkout supplies only
independent test fixtures; the child imports the installed product and native engine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    """Check installation identity, read-only queries and native shell/UI consumers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--tests", type=Path, default=Path(__file__).resolve().parents[1] / "tests"
    )
    parser.add_argument("--shell", action="append", choices=("bash", "zsh", "tcsh"))
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--support",
        type=Path,
        help="Isolated dev dependency directory for the parent test driver",
    )
    args = parser.parse_args()
    root = args.directory.resolve()
    runtime = root / "bin/python"
    probe = r"""
import importlib.util
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
runtime = root / 'bin/python'
import ish
from ish.runtime.distribution import bundled_vt_library
from ish.terminal.vt import Terminal
assert Path(ish.__file__).resolve().is_relative_to(runtime), ish.__file__
bridge = bundled_vt_library()
assert bridge is not None and bridge.is_file() and bridge.resolve().is_relative_to(root), bridge
with Terminal(24, 4, lambda _: None) as terminal:
    terminal.feed('PACKAGED 한글'.encode())
    assert ''.join(value for _, value in terminal.frame().lines[0]).rstrip() == 'PACKAGED 한글'
for removed in ('ish.main', 'ish.stream', 'ish.shell', 'ish.parser', 'ish.ui'):
    assert importlib.util.find_spec(removed) is None, removed
for removed in ('ish-vt', 'ish-stream', 'ish2', 'ish-legacy'):
    assert not (root / removed).exists() and not (runtime / 'bin' / removed).exists(), removed
print(json.dumps({'installed_package': ish.__file__, 'native': str(bridge), 'version': ish.__version__}))
"""
    result = subprocess.run(
        [str(runtime / "bin/python3"), "-I", "-c", probe, str(root)],
        env={
            name: value
            for name, value in os.environ.items()
            if not name.startswith("ISH_GHOSTTY_")
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    installed = json.loads(result.stdout)
    if args.support:
        sys.path.insert(0, str(args.support.resolve()))
    sys.path.insert(0, str(args.tests.resolve()))
    from terminal_scenarios import HooklessTerminal, Scenario

    executable = root / "ish"
    results = []
    with tempfile.TemporaryDirectory(prefix="ish-package-smoke-") as temporary:
        work = Path(temporary)
        settings = work / "settings"
        settings.mkdir()
        trap = work / "rc-executed"
        (settings / ".ishrc.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(trap)!r}).touch()\n"
            "raise RuntimeError('information queries ran configuration')\n"
        )
        env = dict(
            os.environ,
            PYTHONHOME=str(work / "absent-python"),
            PYTHONPATH=str(work / "unrelated-source"),
        )
        for name in tuple(env):
            if name.startswith("ISH_GHOSTTY_"):
                env.pop(name)
        for option in ("--help", "--version", "--diagnose"):
            result = subprocess.run(
                [str(executable), "--home", str(settings), "--lang", "en", option],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            if trap.exists() or result.stderr or not result.stdout:
                raise AssertionError(
                    ("Installed read-only query failed", option, result)
                )
            if (
                option == "--version"
                and result.stdout.strip() != "ish " + installed["version"]
            ):
                raise AssertionError(("Unexpected public version", result.stdout))
        if sorted(path.name for path in settings.iterdir()) != [".ishrc.py"]:
            raise AssertionError("Information queries created session files")
        blocked = work / "blocked"
        blocked.mkdir()
        for name in ("python", "python3", "gcc", "cc"):
            path = blocked / name
            path.write_text(
                f"#!/bin/sh\nprintf invoked >> {str(work / 'forbidden-tool')!r}\nexit 99\n"
            )
            path.chmod(0o755)
        for shell in args.shell or ("bash", "zsh", "tcsh"):
            for plugins in (False, True):
                name = f"{shell}-{'plugins' if plugins else 'plain'}"
                terminal = HooklessTerminal(
                    work / name,
                    shell=shell,
                    plugins=plugins,
                    executable=executable,
                    environment={"PATH": str(blocked) + ":/usr/bin:/bin"},
                )
                try:
                    Scenario(terminal, shell, plugins).run()
                    results.append({"name": name, "status": "passed"})
                    print(json.dumps(results[-1]), flush=True)
                finally:
                    terminal.close()
        if (work / "forbidden-tool").exists():
            raise AssertionError("The distribution invoked host Python or a compiler")
    report = {
        "status": "passed",
        **installed,
        "cases": results,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
