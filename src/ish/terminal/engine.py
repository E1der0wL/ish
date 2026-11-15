"""Validate the native boundary before assigning reviewed input ownership.

This profile describes the engine, not the outer terminal. New native input
support must pass the same observable contract as its adapter before it is
added here. A newer library or a new symbol alone never enables a feature.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

ENGINE_VERSION = "1.0.1"
# Increment whenever the private C/Python signatures or snapshots change.
# The build script passes this value to C; there is no independent C default.
BRIDGE_ABI = 1
NATIVE_INPUTS = ("kitty",)
REBUILD = "Rebuild with tools/build_ghostty_vt.py."


class EngineError(RuntimeError):
    """The loaded native artifact does not implement the reviewed boundary."""


@dataclass(frozen=True, slots=True)
class EngineInfo:
    """Verified engine facts, independent of host output support and policy."""

    version: str
    header_version: str
    bridge_abi: int
    native_inputs: tuple[str, ...]


def _function(library, name, result):
    try:
        function = getattr(library, name)
    except AttributeError as error:
        raise EngineError(
            f"Missing native metadata entry point {name}. {REBUILD}"
        ) from error
    function.argtypes = []
    function.restype = result
    return function


def _version(library, name):
    value = _function(library, name, ctypes.c_char_p)()
    try:
        if not value:
            raise ValueError("empty version")
        return value.decode("ascii")
    except (AttributeError, UnicodeError, ValueError) as error:
        raise EngineError(f"Invalid native version from {name}. {REBUILD}") from error


def check_library(library) -> EngineInfo:
    """Reject incompatible artifacts before binding stateful or snapshot calls."""
    abi = _function(library, "ish_vt_abi", ctypes.c_uint32)()
    if abi != BRIDGE_ABI:
        raise EngineError(
            f"Native bridge ABI {abi!r} does not match required ABI {BRIDGE_ABI}. {REBUILD}"
        )
    headers = _version(library, "ish_vt_headers")
    version = _version(library, "ish_vt_version")
    if headers != ENGINE_VERSION or version != ENGINE_VERSION:
        raise EngineError(
            f"Unverified libghostty-vt artifact: headers={headers}, runtime={version}; "
            f"this build requires {ENGINE_VERSION}. {REBUILD}"
        )
    missing = _function(library, "ish_vt_missing_feature", ctypes.c_char_p)()
    if missing:
        name = missing.decode("ascii", errors="replace")
        raise EngineError(f"Native build lacks required feature {name}. {REBUILD}")
    return EngineInfo(version, headers, abi, NATIVE_INPUTS)
