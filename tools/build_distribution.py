"""Package a relocatable Linux x86_64 CPython installation and the locked ish wheel.

Run with tools/build.sh. The pinned runtime determines platform and libc
requirements; dependencies come from locked wheels and the pinned libghostty build.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
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

DOCUMENTATION_PATHS = ("README.md", "AGENTS.md", "docs", "bundles")
LAUNCHERS = ("ish",)
SOURCE_PATHS = (
    "src",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "tools/build.sh",
    "tools/python-runtime.toml",
    "tools/ish-launcher.sh",
    "tools/build_distribution.py",
    "tools/build_ghostty_vt.py",
    *DOCUMENTATION_PATHS,
)
SOURCE_CACHE_DIRS = frozenset(("__pycache__", ".pytest_cache", ".ruff_cache"))
SOURCE_BINARY_SUFFIXES = frozenset(
    (".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib", ".o", ".a")
)
NATIVE_LICENSE_NAMES = frozenset(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE"))


def source_digest(root: Path) -> str:
    """Identify source and packaged resources, excluding generated caches and binaries."""
    paths = []
    for name in SOURCE_PATHS:
        path = root / name
        candidates = path.rglob("*") if path.is_dir() else (path,)
        for candidate in candidates:
            relative = candidate.relative_to(root)
            if (
                any(
                    part in SOURCE_CACHE_DIRS
                    or part.endswith((".egg-info", ".dist-info"))
                    for part in relative.parts
                )
                or candidate.suffix in SOURCE_BINARY_SUFFIXES
            ):
                continue
            if candidate.is_file():
                paths.append(candidate)
            elif candidate == path and not path.exists():
                raise FileNotFoundError(path)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def sha256(path: Path) -> str:
    """Hash large runtime archives without retaining them in memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_documentation(root: Path, destination: Path) -> None:
    """Keep the user guide and its local links usable in a relocated installation."""
    for name in DOCUMENTATION_PATHS:
        source = root / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns(
                    *SOURCE_CACHE_DIRS,
                    *(f"*{suffix}" for suffix in SOURCE_BINARY_SUFFIXES),
                ),
            )
        else:
            shutil.copy2(source, target)


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
    import zstandard

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


def copy_native(source: Path, bundle: Path, licenses: tuple[Path, ...]) -> dict:
    """Install the verified bridge, its relative engine links and source licenses."""
    from tools.build_ghostty_vt import NATIVE_STAMP, verify_install

    from ish.runtime.distribution import VT_LIBRARY_PATH
    from ish.terminal.engine import ENGINE_VERSION, check_library

    source = source.resolve()
    verify_install(source.parent)
    check_library(ctypes.CDLL(str(source / VT_LIBRARY_PATH.name)))
    destination = bundle / VT_LIBRARY_PATH.parent
    destination.mkdir(parents=True)
    names = (
        VT_LIBRARY_PATH.name,
        "libghostty-vt.so",
        "libghostty-vt.so.1",
        f"libghostty-vt.so.{ENGINE_VERSION}",
    )
    hashes = {}
    for name in names:
        path = source / name
        if not path.is_file() or not path.resolve().is_relative_to(source):
            raise RuntimeError(f"Missing or escaping native library: {path}")
        if path.is_symlink() and Path(os.readlink(path)).is_absolute():
            raise RuntimeError(f"Native library link is not relocatable: {path}")
        shutil.copy2(path, destination / name, follow_symlinks=False)
        hashes[name] = sha256(path)
    license_count = 0
    for directory in licenses:
        for path in directory.rglob("*"):
            if (
                path.is_file()
                and re.split(r"[._-]", path.name.upper(), maxsplit=1)[0]
                in NATIVE_LICENSE_NAMES
            ):
                target = (
                    bundle / "licenses" / directory.name / path.relative_to(directory)
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                license_count += 1
    if not license_count:
        raise RuntimeError("The pinned native source did not provide license files")
    provenance_dir = bundle / "licenses/ish-native-build"
    provenance_dir.mkdir(parents=True)
    shutil.copy2(source.parent / NATIVE_STAMP, provenance_dir / NATIVE_STAMP)
    return {
        "engine": ENGINE_VERSION,
        "source": "upstream",
        "provenance_sha256": sha256(source.parent / NATIVE_STAMP),
        "sha256": hashes,
        "license_files": license_count,
    }


def prepare_native(root: Path, cache: Path) -> None:
    """Reuse a verified engine and rebuild only the narrow bridge for current source."""
    from ish.runtime.distribution import VT_LIBRARY_PATH

    native = root / ".cache/ghostty-vt/install/lib" / VT_LIBRARY_PATH.name
    header = native.parent.parent / "include/ghostty/vt.h"
    reusable = False
    if native.is_file() and header.is_file():
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import ctypes, sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[2]); "
                "from ish.terminal.engine import check_library; "
                "from tools.build_ghostty_vt import verify_install; "
                "verify_install(Path(sys.argv[1]).parents[1]); "
                "check_library(ctypes.CDLL(sys.argv[1]))",
                str(native),
                str(Path(__file__).resolve().parents[1]),
            ],
            cwd=root,
            capture_output=True,
            check=False,
        )
        reusable = probe.returncode == 0
    command = [sys.executable, str(root / "tools/build_ghostty_vt.py")]
    subprocess.run(
        [*command, "--cache-dir", str(cache), *(["--prepare"] if reusable else [])],
        cwd=root,
        check=True,
    )
    if reusable:
        subprocess.run([*command, "--bridge-only"], cwd=root, check=True)


def main() -> None:
    """Build in a private staging directory and publish only a verified installation."""
    from ish.runtime.distribution import RUNTIME_MARKER

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
    sys.path.insert(0, str(root))
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
    for tool in ("uv",):
        if not shutil.which(tool):
            parser.error(f"Missing build tool: {tool}")
    project = tomllib.loads((root / "pyproject.toml").read_text())
    digest = source_digest(root)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    build_id = (
        args.build_id or f"{project['project']['version']}-{timestamp}-{digest[:8]}"
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
    native_cache = cache.parent / "ghostty-vt"
    prepare_native(root, native_cache)
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
        relocate_python_scripts(bundle / "python")
        for name in LAUNCHERS:
            shutil.copy2(root / f"tools/{name}-launcher.sh", bundle / name)
            (bundle / name).chmod(0o755)
        (bundle / "python" / RUNTIME_MARKER).write_text(
            json.dumps(spec, indent=2) + "\n"
        )
        from ish.terminal.engine import ENGINE_VERSION

        native = copy_native(
            root / ".cache/ghostty-vt/install/lib",
            bundle,
            (
                native_cache / "upstream" / f"libghostty-vt-{ENGINE_VERSION}",
                native_cache / f"libghostty-vt-{ENGINE_VERSION}-deps",
            ),
        )
        copy_documentation(root, bundle)
        for pattern in ("LICENSE", "LICENSE.*", "COPYING", "COPYING.*"):
            for license_path in root.glob(pattern):
                shutil.copy2(license_path, bundle / license_path.name)
        shutil.copy2(requirements, bundle / "requirements.txt")
        for name in LAUNCHERS:
            subprocess.run([str(bundle / name), "--version"], cwd=work, check=True)
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
            "source_sha256": digest,
            "wheel_sha256": sha256(wheel),
            "native": native,
            "created_utc": timestamp,
        }
        destination.mkdir(parents=True)
        shutil.copytree(bundle, destination / "bin", symlinks=True)
        for name in LAUNCHERS:
            (destination / name).symlink_to(f"./bin/{name}")
        copy_documentation(root, destination)
        for pattern in ("LICENSE", "LICENSE.*", "COPYING", "COPYING.*"):
            for license_path in root.glob(pattern):
                shutil.copy2(license_path, destination / license_path.name)
        (destination / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for name in LAUNCHERS:
            subprocess.run([str(destination / name), "--version"], cwd=work, check=True)
    print(f"Distribution: {destination}", flush=True)


if __name__ == "__main__":
    main()
