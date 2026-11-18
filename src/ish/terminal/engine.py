"""Validate the native boundary before assigning reviewed input ownership.

This profile describes the engine, not the outer terminal. New native input
support must pass the same observable contract as its adapter before it is
added here. A newer library or a new symbol alone never enables a feature.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

from ish.lang import Message, i18n

ENGINE_VERSION = "1.0.1"
# Increment whenever the private C/Python signatures or snapshots change.
# The build script passes this value to C; there is no independent C default.
BRIDGE_ABI = 7
NATIVE_INPUTS = ("kitty",)
# Reviewed public extension identities include their exact wire namespaces.
# Image routing above remains independent; a name alone never promotes an OSC.
NATIVE_PROTOCOLS: tuple[tuple[str, str, tuple[bytes, ...]], ...] = ()


class EngineError(RuntimeError):
    """The loaded native artifact does not implement the reviewed boundary."""


def _error(message, **values):
    return EngineError(
        i18n.get(message, rebuild=i18n.get(Message.VT_ENGINE_REBUILD), **values)
    )


@dataclass(frozen=True, slots=True)
class EngineInfo:
    """Verified engine facts, independent of host output support and policy."""

    version: str
    header_version: str
    bridge_abi: int
    native_inputs: tuple[str, ...]
    native_protocols: tuple[tuple[str, str, tuple[bytes, ...]], ...] = ()


def _function(library, name, result):
    try:
        function = getattr(library, name)
    except AttributeError as error:
        raise _error(Message.VT_ENGINE_ENTRY, name=name) from error
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
        raise _error(Message.VT_ENGINE_VERSION, name=name) from error


def check_library(library) -> EngineInfo:
    """Reject incompatible artifacts before binding stateful or snapshot calls."""
    abi = _function(library, "ish_vt_abi", ctypes.c_uint32)()
    if abi != BRIDGE_ABI:
        raise _error(Message.VT_ENGINE_ABI, actual=abi, expected=BRIDGE_ABI)
    headers = _version(library, "ish_vt_headers")
    version = _version(library, "ish_vt_version")
    if headers != ENGINE_VERSION or version != ENGINE_VERSION:
        raise _error(
            Message.VT_ENGINE_UNVERIFIED,
            headers=headers,
            version=version,
            expected=ENGINE_VERSION,
        )
    missing = _function(library, "ish_vt_missing_feature", ctypes.c_char_p)()
    if missing:
        name = missing.decode("ascii", errors="replace")
        raise _error(Message.VT_ENGINE_FEATURE, name=name)
    return EngineInfo(version, headers, abi, NATIVE_INPUTS, NATIVE_PROTOCOLS)
