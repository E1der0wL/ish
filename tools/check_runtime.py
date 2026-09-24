"""Check a shipped interpreter's stdlib, native modules, pip, and spawn after relocation.

Run with the distribution's python/bin/python3 -I, outside the source tree. This
probe needs neither the project's development dependencies nor network access.
"""

from __future__ import annotations

import bz2
import concurrent.futures
import ctypes
import dbm.ndbm
import importlib
import importlib.util
import json
import lzma
import multiprocessing
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import tkinter
import venv
from pathlib import Path


def square(value: int) -> int:
    """Expose a pickleable function for a fresh-interpreter process pool."""
    return value * value


def main() -> None:
    """Exercise representative stdlib families and report the actual interpreter."""
    from ish.runtime.distribution import bundled_forward_binary
    from ish.shell.adapter import preload_libraries

    assert sys.version_info[:3] == (3, 12, 14), sys.version
    helper = bundled_forward_binary()
    assert helper is not None and helper.is_file(), helper
    for library in preload_libraries():
        native = library.bundled_path()
        assert native is not None and native.is_file(), native
    # sys.stdlib_module_names also lists modules for other operating systems.
    foreign = {
        "_msi",
        "_overlapped",
        "_scproxy",
        "_winapi",
        "_wmi",
        "msilib",
        "msvcrt",
        "nt",
        "winreg",
        "winsound",
    }
    missing = {
        name
        for name in sys.stdlib_module_names
        if importlib.util.find_spec(name) is None
    } - foreign
    assert missing == {"_gdbm", "nis"}, missing
    imported = 0
    for name in sorted(
        sys.stdlib_module_names - foreign - missing - {"antigravity", "this"}
    ):
        # antigravity opens a browser; this prints the Zen of Python. Neither is
        # needed to validate extension loading on a target host.
        importlib.import_module(name)
        imported += 1
    for name in (
        "asyncio",
        "ctypes.util",
        "curses",
        "email.mime.text",
        "ensurepip",
        "http.server",
        "idlelib",
        "multiprocessing.shared_memory",
        "pdb",
        "unittest.mock",
        "xml.dom.minidom",
        "xml.etree.ElementTree",
        "zoneinfo",
        "pip",
        "psutil",
        "pygments.lexers.python",
        "prompt_toolkit",
    ):
        importlib.import_module(name)
    assert bz2.decompress(bz2.compress(b"stdlib")) == b"stdlib"
    assert lzma.decompress(lzma.compress(b"stdlib")) == b"stdlib"
    assert sqlite3.connect(":memory:").execute("select 42").fetchone() == (42,)
    assert ctypes.CDLL(None).getpid() > 0
    assert tkinter.Tcl().eval("expr {6 * 7}") == "42"
    ssl.create_default_context()
    with tempfile.TemporaryDirectory(prefix="ish-runtime-") as directory:
        root = Path(directory)
        for index, backend in enumerate((dbm.ndbm,)):
            with backend.open(str(root / f"db-{index}"), "c") as database:
                database[b"key"] = b"value"
                assert database[b"key"] == b"value"
        # Users can also create a venv with the bundled CPython and seed pip.
        target = root / "venv"
        venv.EnvBuilder(with_pip=True).create(target)
        subprocess.run(
            [str(target / "bin/python"), "-I", "-m", "pip", "--version"], check=True
        )
    with concurrent.futures.ProcessPoolExecutor(
        mp_context=multiprocessing.get_context("spawn"), max_workers=1
    ) as executor:
        assert executor.submit(square, 7).result(timeout=30) == 49
    subprocess.run([sys.executable, "-I", "-m", "pip", "check"], check=True)
    print(
        json.dumps(
            {
                "runtime": sys.executable,
                "stdlib_and_spawn": "passed",
                "stdlib_modules_imported": imported,
                "upstream_stdlib_exclusions": sorted(missing),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
