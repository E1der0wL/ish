"""Build pinned Linux libghostty-vt with an isolated cache and local installation."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "ghostty-vt"
VERSION = "1.0.1"
ZIG_VERSION = "0.17.1"
RELEASE = f"https://release.files.ghostty.org/libghostty-vt/{VERSION}"
DOWNLOADS = (
    (
        f"{RELEASE}/libghostty-vt-{VERSION}.tar.gz",
        "333dfce97641ef9b536bb9e08907ad86abaaada50e8f7417b70d9284adef37aa",
    ),
    (
        f"{RELEASE}/libghostty-vt-{VERSION}-deps.tar.gz",
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


def unpack(path: Path) -> None:
    """Extract the checksum-pinned official archives inside the local cache."""
    marker = path.with_name(path.name + ".unpacked")
    if marker.exists():
        return
    print(f"Extracting {path.name}", flush=True)
    subprocess.run(
        [
            "tar",
            "xf",
            str(path),
            "--no-same-owner",
            "--no-same-permissions",
            "-C",
            str(path.parent),
        ],
        check=True,
    )
    marker.write_text(digest(path), encoding="ascii")


def main() -> None:
    """Install the library and build the narrow C bridge without global installs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-only", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
    args = parser.parse_args()
    cache = args.cache_dir.resolve()
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        parser.error(
            "This prototype build currently targets Linux x86_64 (including WSL)."
        )
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        archives = list(pool.map(partial(download, cache=cache), DOWNLOADS))
    for archive in archives:
        unpack(archive)
    if args.prepare:
        return
    source = cache / f"libghostty-vt-{VERSION}"
    dependencies = cache / f"libghostty-vt-{VERSION}-deps"
    zig = cache / f"zig-x86_64-linux-{ZIG_VERSION}" / "zig"
    prefix = CACHE / "install"
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(cache / "zig-cache"))
    print(
        "Building libghostty-vt (ReleaseFast, baseline CPU, SIMD disabled)", flush=True
    )
    subprocess.run(
        [
            str(zig),
            "build",
            "-Doptimize=ReleaseFast",
            "-Dcpu=baseline",
            "-Dsimd=false",
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
    if args.library_only:
        return
    output = prefix / "lib" / "libish_vt.so"
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
            "-I",
            str(prefix / "include"),
            str(ROOT / "src/ish/terminal/native/bridge.c"),
            "-L",
            str(prefix / "lib"),
            "-Wl,-rpath,$ORIGIN",
            "-lghostty-vt",
            "-o",
            str(output),
        ],
        check=True,
    )
    print(f"Built {output}", flush=True)


if __name__ == "__main__":
    main()
