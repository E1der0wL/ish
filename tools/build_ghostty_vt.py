"""Build pinned Linux libghostty-vt with an isolated cache and local installation."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ish.terminal.engine import (  # noqa: E402
    BRIDGE_ABI,
    ENGINE_VERSION,
    check_library,
)

CACHE = ROOT / ".cache" / "ghostty-vt"
NATIVE_STAMP = "ish-native.json"
ZIG_VERSION = "0.17.1"
BUILD_FLAGS = ("-Doptimize=ReleaseFast", "-Dcpu=baseline", "-Dsimd=false")
SOURCE_BUILD_DIRS = frozenset((".zig-cache", "zig-out"))
RELEASE = f"https://release.files.ghostty.org/libghostty-vt/{ENGINE_VERSION}"
DOWNLOADS = (
    (
        f"{RELEASE}/libghostty-vt-{ENGINE_VERSION}.tar.gz",
        "333dfce97641ef9b536bb9e08907ad86abaaada50e8f7417b70d9284adef37aa",
    ),
    (
        f"{RELEASE}/libghostty-vt-{ENGINE_VERSION}-deps.tar.gz",
        "91a4e10960a52bf5bf4b15aa2a2cddaff8a89d6cd903d3055bda4fd17de3356d",
    ),
    (
        f"https://ziglang.org/download/{ZIG_VERSION}/zig-x86_64-linux-{ZIG_VERSION}.tar.xz",
        "3581863e3cbcfd0fce0efc682ce7e9e93da96668a977791d356a26d2a7e9b17a",
    ),
)


def digest(path: Path) -> str:
    """Hash the downloaded archive without loading it into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(item: tuple[str, str], *, cache: Path) -> Path:
    """Verify every archive against the pinned official release checksum."""
    url, expected = item
    path = cache / url.rsplit("/", 1)[1]
    if not path.exists() or digest(path) != expected:
        temporary = path.with_suffix(path.suffix + ".part")
        print(f"Downloading {path.name}", flush=True)
        subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--retry",
                "2",
                "--max-time",
                "180",
                "--output",
                str(temporary),
                url,
            ],
            check=True,
        )
        if digest(temporary) != expected:
            raise RuntimeError(f"Checksum mismatch: {path.name}")
        temporary.replace(path)
    return path


def unpack(path: Path, destination: Path | None = None) -> None:
    """Extract the checksum-pinned official archives inside the local cache."""
    destination = destination or path.parent
    marker = destination / (path.name + ".unpacked")
    expected = digest(path)
    if marker.is_file() and marker.read_text() == expected:
        return
    destination.mkdir(parents=True, exist_ok=True)
    print(f"Extracting {path.name}", flush=True)
    subprocess.run(
        [
            "tar",
            "xf",
            str(path),
            "--no-same-owner",
            "--no-same-permissions",
            "-C",
            str(destination),
        ],
        check=True,
    )
    marker.write_text(expected, encoding="ascii")


def verify_source(archive: Path, source: Path) -> None:
    """Reject cached source edits instead of patching or repairing upstream files."""
    expected = set()
    with tarfile.open(archive) as stream:
        for member in stream:
            name = Path(member.name)
            if not name.parts or name.parts[0] != source.name or ".." in name.parts:
                raise RuntimeError(f"Unexpected upstream archive path: {member.name}")
            relative = Path(*name.parts[1:])
            path = source / relative
            expected.add(relative)
            expected.update(relative.parents)
            if member.isfile():
                if not path.is_file() or path.is_symlink():
                    raise RuntimeError(
                        f"Upstream source is missing or modified: {relative}"
                    )
                with stream.extractfile(member) as content:
                    matches = (
                        digest(path)
                        == hashlib.file_digest(content, "sha256").hexdigest()
                    )
                if not matches:
                    raise RuntimeError(f"Upstream source is modified: {relative}")
            elif member.issym():
                if not path.is_symlink() or os.readlink(path) != member.linkname:
                    raise RuntimeError(f"Upstream source link is modified: {relative}")
            elif member.isdir():
                if not path.is_dir() or path.is_symlink():
                    raise RuntimeError(
                        f"Upstream source directory is modified: {relative}"
                    )
            else:
                raise RuntimeError(f"Unexpected upstream archive entry: {member.name}")
    for directory, directories, files in source.walk():
        if directory == source:
            directories[:] = [
                name for name in directories if name not in SOURCE_BUILD_DIRS
            ]
        for name in (*directories, *files):
            relative = (directory / name).relative_to(source)
            if relative not in expected:
                raise RuntimeError(f"Unexpected file in upstream source: {relative}")


def build_bridge(prefix: Path) -> None:
    """Compile the selected ABI and atomically replace only a successful build."""
    verify_install(prefix)
    output = prefix / "lib" / "libish_vt.so"
    with tempfile.NamedTemporaryFile(
        prefix="libish_vt-", suffix=".so", dir=output.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
    try:
        subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                "-shared",
                f"-DISH_BRIDGE_ABI={BRIDGE_ABI}",
                "-I",
                str(prefix / "include"),
                str(ROOT / "src/ish/terminal/native/bridge.c"),
                "-L",
                str(prefix / "lib"),
                "-Wl,-rpath,$ORIGIN",
                "-lghostty-vt",
                "-o",
                str(temporary),
            ],
            check=True,
        )
        check_library(ctypes.CDLL(str(temporary)))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Built {output}", flush=True)


def install_identity(prefix: Path) -> dict:
    """Bind the artifact to the official archives and unmodified build recipe."""
    library = prefix / "lib" / f"libghostty-vt.so.{ENGINE_VERSION}"
    if not library.is_file() or library.is_symlink():
        raise ValueError("Native versioned library is missing or replaced")
    for suffix in ("", f".{ENGINE_VERSION.split('.')[0]}"):
        alias = library.parent / f"libghostty-vt.so{suffix}"
        if not alias.is_symlink() or alias.resolve() != library.resolve():
            raise ValueError(
                f"Native library alias does not match verified artifact: {alias.name}"
            )
    return {
        "engine": ENGINE_VERSION,
        "source": "upstream",
        "archives": {url.rsplit("/", 1)[1]: checksum for url, checksum in DOWNLOADS},
        "zig_version": ZIG_VERSION,
        "build_flags": list(BUILD_FLAGS),
        "library_sha256": digest(library),
    }


def record_install(prefix: Path) -> None:
    """Publish provenance only after the pinned engine build succeeds."""
    stamp = prefix / NATIVE_STAMP
    identity = install_identity(prefix)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=prefix, prefix=NATIVE_STAMP, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(identity, stream, sort_keys=True, indent=2)
            stream.write("\n")
        temporary.replace(stamp)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_install(prefix: Path) -> None:
    """Reject old patched builds and modified or differently built libraries."""
    try:
        recorded = json.loads((prefix / NATIVE_STAMP).read_text())
        matches = recorded == install_identity(prefix)
    except (OSError, ValueError):
        matches = False
    if not matches:
        raise RuntimeError("Native artifact provenance mismatch; rebuild libghostty-vt")


def main() -> None:
    """Install the library and build the narrow C bridge without global installs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-only", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--bridge-only", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
    args = parser.parse_args()
    cache = args.cache_dir.resolve()
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        parser.error(
            "This prototype build currently targets Linux x86_64 (including WSL)."
        )
    prefix = CACHE / "install"
    if args.bridge_only:
        if args.library_only or args.prepare:
            parser.error(
                "--bridge-only cannot be combined with --library-only or --prepare"
            )
        build_bridge(prefix)
        return
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        archives = list(pool.map(partial(download, cache=cache), DOWNLOADS))
    # Former experimental source trees are left untouched. Builds use a
    # separate official-source directory and reject edits on every reuse.
    source_cache = cache / "upstream"
    unpack(archives[0], source_cache)
    source = source_cache / f"libghostty-vt-{ENGINE_VERSION}"
    verify_source(archives[0], source)
    for archive in archives[1:]:
        unpack(archive)
    if args.prepare:
        return
    dependencies = cache / f"libghostty-vt-{ENGINE_VERSION}-deps"
    zig = cache / f"zig-x86_64-linux-{ZIG_VERSION}" / "zig"
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(cache / "zig-cache"))
    print(
        "Building libghostty-vt (ReleaseFast, baseline CPU, SIMD disabled)", flush=True
    )
    subprocess.run(
        [
            str(zig),
            "build",
            *BUILD_FLAGS,
            "--system",
            str(dependencies),
            "--prefix",
            str(prefix),
            "-j2",
        ],
        cwd=source,
        env=env,
        check=True,
    )
    record_install(prefix)
    if args.library_only:
        return
    build_bridge(prefix)


if __name__ == "__main__":
    main()
