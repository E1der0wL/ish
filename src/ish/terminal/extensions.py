"""Exclusive, session-fixed protocol supplements around the native VT owner.

An adapter returns native input and optional PTY replies, never host output.
It owns only transfer state; the native engine still owns the screen. Native
promotion is a reviewed engine capability, not a runtime retry after failure.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

from ish.lang import Message, i18n

from .protocol import MAX_STRING_BYTES, Control, ProtocolStream

MAX_PREFIX_BYTES = 256


def _discard_awaitable(value):
    """Cancel already scheduled results as well as closing unstarted coroutines."""
    if inspect.iscoroutine(value):
        value.close()
    elif isinstance(value, asyncio.Future):
        value.cancel()

        def collected(future):
            if not future.cancelled():
                future.exception()

        if value.done():
            collected(value)
        else:
            value.add_done_callback(collected)


@dataclass(frozen=True, slots=True)
class ProtocolResult:
    """Apply native input first, then enqueue a reply through the session writer."""

    native: bytes = b""
    reply: bytes = b""


@dataclass
class _Route:
    name: str
    kind: str
    prefixes: tuple[bytes, ...]
    provider: str
    adapter: object = None
    failed: bool = False
    closed: bool = False
    errors: int = 0


class ProtocolRegistry:
    """Assign a complete control to one owner, without fallthrough on errors."""

    KINDS = frozenset(("osc", "apc", "dcs", "pm", "sos"))
    MAX_PREFIXES = 16
    MAX_PROTOCOLS = 64
    MAX_REPLY_BYTES = 1024 * 1024
    MAX_ERRORS = 16

    def __init__(self, *, native=(), write, reply=None):
        """Keep reviewed native facts and the sole output sinks for this session."""
        self._native = frozenset(native)
        self._write, self._reply = write, reply
        self._routes: list[_Route] = []
        self._started = self._closed = False
        self._errors: list[str] = []

    @property
    def errors(self):
        """Return bounded diagnostics, without returning mutable adapter state."""
        return tuple(self._errors)

    @property
    def routes(self):
        """Read immutable implementation choices, independent of host support."""
        return tuple((route.name, route.provider) for route in self._routes)

    @property
    def failures(self):
        """Expose diagnostic counts without exception strings or child payloads."""
        return tuple(
            (route.name, route.errors) for route in self._routes if route.errors
        )

    def register(self, name, *, kind, prefixes, factory=None, provider=None):
        """Reject ambiguous selectors before any bytes can start a session.

        Factories take no writer or engine handle. Their object implements
        handle(Control) -> ProtocolResult, cancel() and close(), synchronously.
        Native routes never construct their supplemental adapter.
        """
        if self._started or self._closed:
            raise RuntimeError(i18n.get(Message.VT_PROTOCOL_LATE))
        if (
            not isinstance(name, str)
            or not name
            or len(name) > MAX_PREFIX_BYTES
            or not name.isascii()
            or any(not (char.isalnum() or char in "-_.") for char in name)
        ):
            raise ValueError(i18n.get(Message.VT_PROTOCOL_NAME))
        if kind not in self.KINDS or not isinstance(prefixes, tuple):
            raise ValueError(i18n.get(Message.VT_PROTOCOL_SELECTORS))
        if not 1 <= len(prefixes) <= self.MAX_PREFIXES or any(
            not isinstance(prefix, bytes) or not 1 <= len(prefix) <= MAX_PREFIX_BYTES
            for prefix in prefixes
        ):
            raise ValueError(i18n.get(Message.VT_PROTOCOL_PREFIXES))
        for index, prefix in enumerate(prefixes):
            if any(
                prefix.startswith(other) or other.startswith(prefix)
                for other in prefixes[:index]
            ):
                raise ValueError(i18n.get(Message.VT_PROTOCOL_OVERLAP))
        if len(self._routes) >= self.MAX_PROTOCOLS:
            raise ValueError(i18n.get(Message.VT_PROTOCOL_LIMIT))
        for route in self._routes:
            if route.name == name or (
                route.kind == kind
                and any(
                    first.startswith(second) or second.startswith(first)
                    for first in prefixes
                    for second in route.prefixes
                )
            ):
                raise ValueError(
                    i18n.get(Message.VT_PROTOCOL_CONFLICT, name=route.name)
                )
        selected = provider or ("native" if name in self._native else "adapter")
        if selected not in ("native", "adapter", "unsupported", "reserved"):
            raise ValueError(i18n.get(Message.VT_PROTOCOL_PROVIDER))
        # Reserve first so a factory cannot recursively claim the same selector.
        route = _Route(name, kind, prefixes, selected)
        self._routes.append(route)
        try:
            if selected == "adapter":
                if not callable(factory):
                    raise TypeError(i18n.get(Message.VT_PROTOCOL_FACTORY))
                route.adapter = factory()
                if not all(
                    callable(getattr(route.adapter, method, None))
                    for method in ("handle", "cancel", "close")
                ):
                    raise TypeError(i18n.get(Message.VT_PROTOCOL_METHODS))
        except BaseException:
            self._routes.remove(route)
            if inspect.isawaitable(route.adapter):
                _discard_awaitable(route.adapter)
            elif callable(getattr(route.adapter, "close", None)):
                self._close(route)
            raise
        return selected

    def start(self):
        """Seal ownership before the first byte, including an incomplete record."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PROTOCOL_CLOSED))
        self._started = True

    def _record(self, route, error):
        route.errors += 1
        self._errors.append(f"{route.name}: {type(error).__name__}: {error}")
        del self._errors[: -self.MAX_ERRORS]

    def _invoke(self, route, method):
        try:
            result = getattr(route.adapter, method)()
            if inspect.isawaitable(result):
                _discard_awaitable(result)
                raise TypeError(i18n.get(Message.VT_PROTOCOL_SYNC))
        except Exception as error:
            self._record(route, error)
            return False
        return True

    def _close(self, route):
        if route.adapter is not None and not route.closed:
            route.closed = True
            self._invoke(route, "close")

    @staticmethod
    def _complete(data):
        discarded = False

        def cancel():
            nonlocal discarded
            discarded = True

        framing = ProtocolStream(lambda _: None, lambda _: False, cancel=cancel)
        framing.feed(data)
        return framing.complete and not discarded

    def handle(self, control: Control):
        """Return ownership, not success; failed transfers are never replayed."""
        if self._closed:
            raise RuntimeError(i18n.get(Message.VT_PROTOCOL_CLOSED))
        for route in self._routes:
            if route.kind != control.kind or not control.body.startswith(
                route.prefixes
            ):
                continue
            if route.provider in ("native", "reserved"):
                return False
            if route.provider == "unsupported" or route.failed:
                return True
            try:
                result = route.adapter.handle(control)
                if not isinstance(result, ProtocolResult):
                    _discard_awaitable(result)
                    raise TypeError(i18n.get(Message.VT_PROTOCOL_RESULT_TYPE))
                if (
                    not isinstance(result.native, bytes)
                    or not isinstance(result.reply, bytes)
                    or len(result.native) > MAX_STRING_BYTES
                    or len(result.reply) > self.MAX_REPLY_BYTES
                    or not self._complete(result.native)
                ):
                    raise ValueError(i18n.get(Message.VT_PROTOCOL_RESULT))
                if result.reply and self._reply is None:
                    raise RuntimeError(i18n.get(Message.VT_PROTOCOL_REPLY))
            except Exception as error:
                route.failed = True
                self._record(route, error)
                self._invoke(route, "cancel")
                self._close(route)
                return True
            # Writer errors are terminal/session failures, not adapter failures.
            # Do not retry any portion after a partially completed native write.
            if result.native:
                self._write(result.native)
            if result.reply:
                self._reply(result.reply)
            return True
        return False

    def cancel(self):
        """Cancel transfers at CAN/SUB/RIS/EOF without changing route ownership."""
        for route in self._routes:
            if route.adapter is not None and not route.closed:
                if not self._invoke(route, "cancel"):
                    route.failed = True
                    self._close(route)

    def close(self):
        """Release every adapter once, even when an earlier cleanup fails."""
        if self._closed:
            return
        self._closed = True
        for route in reversed(self._routes):
            self._close(route)
