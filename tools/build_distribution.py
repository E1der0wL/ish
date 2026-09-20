"""Package a relocatable Linux x86_64 CPython installation and the locked ish wheel.

Run with tools/build.sh. Build on the oldest glibc host you support: compiling the
native helpers on a newer system does not make them backward compatible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from ish.runtime.distribution import RUNTIME_MARKER
from ish.shell.adapters import preload_libraries
from ish.shell.integration import build_binary


def sha256(path: Path) -> str:
    """Hash large runtime archives without retaining them in memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_archive(spec: dict, supplied: Path | None, cache: Path) -> Path:
    """Download or reuse only an archive matching the repository's pinned checksum."""
    archive = supplied or cache / spec["sha256"]
    if not archive.exists() and supplied is None:
        print("Downloading " + spec["url"], flush=True)
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
            partial = Path(temporary.name)
            try:
                with urllib.request.urlopen(spec["url"], timeout=60) as response:
                    shutil.copyfileobj(response, temporary)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        try:
            if sha256(partial) != spec["sha256"]:
                raise RuntimeError("CPython archive checksum mismatch")
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if sha256(archive) != spec["sha256"]:
        raise RuntimeError(f"CPython archive checksum mismatch: {archive}")
    return archive


def extract_runtime(archive: Path, bundle: Path) -> None:
    """Extract the install-only archive without modifying its native binaries."""
    with tarfile.open(archive, mode="r|gz") as tar:
        for member in tar:
            if not member.name.startswith("python/"):
                raise RuntimeError(f"Unexpected runtime archive member: {member.name}")
            tar.extract(member, bundle, filter="data")


def extract_licenses(archive: Path, bundle: Path) -> None:
    """Retain licenses and metadata from the full archive without shipping build objects."""
    with archive.open("rb") as source:
        with zstandard.ZstdDecompressor().stream_reader(source) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                for member in tar:
                    if not (
                        member.name.startswith("python/licenses/")
                        or member.name == "python/PYTHON.json"
                    ):
                        continue
                    tar.extract(member, bundle, filter="data")
    if not (bundle / "python/licenses/LICENSE.cpython.txt").is_file():
        raise RuntimeError("The runtime archive does not contain dependency licenses")


def relocate_python_scripts(python: Path) -> None:
    """Replace build-time Python shebangs with relative shell/Python launchers."""
    for script in (python / "bin").iterdir():
        if script.is_symlink() or not script.is_file():
            continue
        data = script.read_bytes()
        first, separator, body = data.partition(b"\n")
        if not first.startswith(b"#!") or b"python" not in first:
            continue
        # Valid both as a shell exec and as a Python triple-quoted string. The
        # remaining entry-point source is preserved, including command arguments.
        prefix = (
            b"#!/bin/sh\n"
            b'\'\'\'exec\' "$(dirname -- "$(readlink -f -- "$0")")/python3" "$0" "$@"\n'
            b"' '''\n"
        )
        script.write_bytes(prefix + body if separator else prefix)


def require_case_sensitive_output(destination: Path) -> None:
    """Reject Windows-mounted output before building case-sensitive runtime data."""
    parent = destination.parent
    while not parent.exists():
        parent = parent.parent
    with tempfile.TemporaryDirectory(prefix="ish-fs-check-", dir=parent) as directory:
        probe = Path(directory) / "case-sensitive"
        probe.touch()
        if probe.with_name("CASE-SENSITIVE").exists():
            raise ValueError(
                'The output filesystem is case-insensitive. In WSL, use --output-dir "$HOME/ish-release" and transfer the result as a tar.gz archive.'
            )


def main() -> None:
    """Build in a private staging directory and publish only a verified installation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-id", help="Release identifier for the output and manifest"
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--runtime-archive",
        type=Path,
        help="Use a local copy of the pinned CPython archive",
    )
    parser.add_argument(
        "--licenses-archive",
        type=Path,
        help="Use a local copy of the matching full archive for licenses",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    spec = tomllib.loads((root / "tools/python-runtime.toml").read_text())
    version = tuple(map(int, spec["version"].split(".")))
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or sys.version_info[:3] != version
    ):
        parser.error(f"Build on Linux x86_64 with Python {spec['version']}")
    if (root / ".python-version").read_text().strip() != spec["version"]:
        parser.error(".python-version and tools/python-runtime.toml must agree")
    for tool in ("uv", "gcc"):
        if not shutil.which(tool):
            parser.error(f"Missing build tool: {tool}")
    project = tomllib.loads((root / "pyproject.toml").read_text())
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")) + [
        root / "pyproject.toml",
        root / "uv.lock",
        root / ".python-version",
        root / "tools/python-runtime.toml",
        root / "tools/ish-launcher.sh",
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
            "Use only letters, digits, dots, underscores, and hyphens in the build ID"
        )
    destination = (args.output_dir or root / "dist/cpython" / build_id).resolve()
    if destination.exists():
        parser.error(f"Refusing to overwrite a release directory: {destination}")
    try:
        require_case_sensitive_output(destination)
    except ValueError as exc:
        parser.error(str(exc))
    cache = Path.home() / ".cache/ish-build/cpython"
    cache.mkdir(parents=True, exist_ok=True)
    archive = runtime_archive(spec, args.runtime_archive, cache)
    licenses = runtime_archive(
        {"url": spec["licenses_url"], "sha256": spec["licenses_sha256"]},
        args.licenses_archive,
        cache,
    )
    with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as staging:
        work = Path(staging)
        bundle = work / "bin"
        bundle.mkdir()
        extract_runtime(archive, bundle)
        extract_licenses(licenses, bundle)
        python = bundle / "python/bin/python3"
        actual = subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import platform; print(platform.python_version())",
            ],
            text=True,
        ).strip()
        if actual != spec["version"]:
            raise RuntimeError(f"Unexpected runtime version: {actual}")
        requirements = work / "requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements),
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # Copy wheel files rather than hard-linking the build cache. The runtime
        # must be independent of the builder and its cache after relocation.
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--link-mode",
                "copy",
                "--require-hashes",
                "--no-deps",
                "--only-binary",
                ":all:",
                "--reinstall",
                "-r",
                str(requirements),
            ],
            cwd=root,
            check=True,
        )
        wheels = work / "wheels"
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--no-build-isolation",
                "--python",
                sys.executable,
                "--out-dir",
                str(wheels),
            ],
            cwd=root,
            check=True,
        )
        (wheel,) = wheels.glob("ish-*.whl")
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--link-mode",
                "copy",
                "--no-deps",
                str(wheel),
            ],
            cwd=root,
            check=True,
        )
        if not build_binary(directory=bundle / "libexec"):
            raise RuntimeError("Could not compile ish_forward")
        for library in preload_libraries():
            if not build_binary(directory=bundle / "libexec", library=library):
                raise RuntimeError(f"Could not compile {library.binary_name}")
        relocate_python_scripts(bundle / "python")
        shutil.copy2(root / "tools/ish-launcher.sh", bundle / "ish")
        (bundle / "ish").chmod(0o755)
        (bundle / "python" / RUNTIME_MARKER).write_text(
            json.dumps(spec, indent=2) + "\n"
        )
        shutil.copy2(root / "README.md", bundle / "README.md")
        for pattern in ("LICENSE", "LICENSE.*", "COPYING", "COPYING.*"):
            for license_path in root.glob(pattern):
                shutil.copy2(license_path, bundle / license_path.name)
        shutil.copy2(requirements, bundle / "requirements.txt")
        subprocess.run([str(bundle / "ish"), "--version"], cwd=work, check=True)
        # Capture the actual installed inventory instead of importing packages.
        packages = json.loads(
            subprocess.check_output(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))",
                ],
                text=True,
            )
        )
        manifest = {
            "build_id": build_id,
            "mode": "bundled-cpython",
            "runtime": spec,
            "python": actual,
            "packages": packages,
            "platform": platform.platform(),
            "libc": platform.libc_ver(),
            "source_sha256": digest.hexdigest(),
            "wheel_sha256": sha256(wheel),
            "created_utc": timestamp,
        }
        destination.mkdir(parents=True)
        shutil.copytree(bundle, destination / "bin", symlinks=True)
        (destination / "ish").symlink_to("./bin/ish")
        shutil.copy2(root / "README.md", destination / "README.md")
        for pattern in ("LICENSE", "LICENSE.*", "COPYING", "COPYING.*"):
            for license_path in root.glob(pattern):
                shutil.copy2(license_path, destination / license_path.name)
        (destination / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
        subprocess.run([str(destination / "ish"), "--version"], cwd=work, check=True)
    print(f"Distribution: {destination}", flush=True)


if __name__ == "__main__":
    main()
