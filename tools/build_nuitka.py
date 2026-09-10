"""Build Linux distributions with a bundled helper and private extraction in user cache.

Run with `uv run --group build python tools/build_nuitka.py`. Build on the oldest
Linux/glibc target you support; Nuitka does not make newer glibc backward compatible.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from ish.shell.constants import BUNDLED_FORWARD_DIRECTORY, FORWARD_BINARY
from ish.shell.integration import build_binary


def main() -> None:
    """Compile into a Linux build cache and copy only distributable files to dist."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("standalone", "onefile"), default="onefile")
    parser.add_argument(
        "--build-id", help="Unique release ID; never reuse for different binaries"
    )
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 4))
    parser.add_argument("--include-package", action="append", default=[])
    parser.add_argument(
        "--all-lexers",
        action="store_true",
        help="Bundle Pygments lexers for languages beyond shells",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux" or sys.version_info[:3] != (3, 12, 14):
        parser.error("Build under Linux/WSL with Python 3.12.14")

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")) + [
        root / "uv.lock",
        Path(__file__),
    ]:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    build_id = (
        args.build_id
        or f"{project['project']['version']}-{timestamp}-{digest.hexdigest()[:8]}"
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", build_id) or build_id in {".", ".."}:
        parser.error(
            "The build ID must contain only letters, digits, underscores, dots, and hyphens"
        )
    cache = Path.home() / ".cache" / "ish-build" / build_id / args.mode
    cache.mkdir(parents=True, exist_ok=True)
    destination = (
        args.output_dir or root / "dist" / "nuitka" / build_id / args.mode
    ).resolve()
    if destination.exists():
        parser.error(f"Refusing to overwrite a release directory: {destination}")
    helper = cache / "helper"
    if not build_binary(directory=helper):
        raise SystemExit("Could not compile ish_forward; check GCC installation")

    command = [
        sys.executable,
        "-m",
        "nuitka",
        f"--mode={args.mode}",
        f"--output-dir={cache}",
        "--output-filename=ish",
        f"--jobs={args.jobs}",
        "--lto=no",
        "--assume-yes-for-downloads",
        # Dependency installation uses an external interpreter. Bundling ensurepip
        # would pull hundreds of unused pip modules into this non-interpreter binary.
        "--nofollow-import-to=pip,ensurepip",
        "--include-package=ish",
        "--include-module=pygments.lexers.shell",
        "--include-distribution-metadata=prompt_toolkit",
        "--include-distribution-metadata=Pygments",
        "--include-distribution-metadata=packaging",
        f"--include-data-files={helper / FORWARD_BINARY}={BUNDLED_FORWARD_DIRECTORY}/{FORWARD_BINARY}",
        f"--report={cache / 'compilation-report.xml'}",
    ]
    if args.mode == "onefile":
        command += [
            # A private directory avoids concurrent cold-start writes to shared
            # libraries. "temporary" means cleanup on exit, not placement in /tmp.
            f"--onefile-tempdir-spec={{HOME}}/ish/.cache/nuitka/{build_id}/launch-{{PID}}-{{TIME_US}}-{{RANDOM}}",
            "--onefile-cache-mode=temporary",
        ]
    if not args.all_lexers and not any(
        name == "pygments" or name.startswith("pygments.lexers")
        for name in args.include_package
    ):
        # Nuitka includes every Pygments lexer by default. The shipped shell
        # adapters only use shell.py; keep mappings for get_lexer_by_name().
        lexer_dir = Path(importlib.util.find_spec("pygments.lexers").origin).parent
        omitted = [
            f"pygments.lexers.{path.stem}"
            for path in sorted(lexer_dir.glob("*.py"))
            if path.stem not in {"__init__", "_mapping", "shell"}
        ]
        command.append("--nofollow-import-to=" + ",".join(omitted))
    command += [f"--include-package={name}" for name in args.include_package]
    command.append(str(root / "src" / "ish" / "main.py"))
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(root / "src")
    print(f"Building {args.mode}: {build_id}\nLog: {cache / 'build.log'}", flush=True)
    with (cache / "build.log").open("w") as log:
        result = subprocess.run(
            command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    if result.returncode:
        print((cache / "build.log").read_text()[-12000:], file=sys.stderr)
        raise SystemExit(result.returncode)

    destination.mkdir(parents=True)
    if args.mode == "onefile":
        shutil.copy2(cache / "ish", destination / "ish")
    else:
        shutil.copytree(cache / "main.dist", destination / "ish.dist")
    shutil.copy2(
        cache / "compilation-report.xml", destination / "compilation-report.xml"
    )
    manifest = {
        "build_id": build_id,
        "mode": args.mode,
        "python": sys.version,
        "platform": platform.platform(),
        "libc": platform.libc_ver(),
        "source_sha256": digest.hexdigest(),
        "command": command,
        "created_utc": timestamp,
    }
    (destination / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Distribution: {destination}", flush=True)


if __name__ == "__main__":
    main()
