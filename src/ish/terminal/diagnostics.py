"""Count native diagnostics without collecting terminal bytes or native messages."""

from __future__ import annotations

import ctypes
from threading import Lock

LOG_LEVELS = ("error", "warning", "info", "debug")
LogCallback = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_size_t,
)
_lock = Lock()
_counts = [0] * len(LOG_LEVELS)


@LogCallback
def capture(_userdata, level, _scope, _scope_len, _message, _message_len):
    """Keep severity counts only; native log text can contain child output."""
    try:
        if 0 <= level < len(LOG_LEVELS):
            with _lock:
                _counts[level] += 1
    except Exception:
        # No Python exception may cross a native callback boundary.
        pass


def snapshot():
    """Read process-wide counts; do not attribute them to an individual session."""
    with _lock:
        return dict(zip(LOG_LEVELS, _counts, strict=True))


def report():
    """Report counts at the caller's safe logging boundary, never native payloads."""
    from ish.lang import Message, i18n
    from ish.log import get_logger

    counts = snapshot()
    if any(counts.values()):
        get_logger().info(i18n.get(Message.VT_NATIVE_LOGS, **counts))


def report_session(app, logger):
    """Report only fixed host reasons and counts after the terminal is restored.

    Host replies, decoder exceptions and terminal payloads are deliberately not
    interpolated. This does not claim to detect every rejected native control.
    Image capability information is opt-in INFO; actual adapter/render failures
    retain WARNING visibility without adding UI to an ordinary session.
    """
    from ish.lang import Message, i18n

    from .host import (
        GRAPHICS_CANCELLED,
        GRAPHICS_DISABLED,
        GRAPHICS_NO_SIZE,
        GRAPHICS_PENDING,
        GRAPHICS_RESIZED,
    )

    for name, state in app.host.feature_status.items():
        logger.info(
            i18n.get(
                Message.VT_FEATURE_STATUS,
                name=name,
                policy=state.policy,
                support=state.support,
                active=state.active,
                reason=state.reason,
            )
        )
    if app.vt.framing_drops:
        logger.warning(i18n.get(Message.VT_FRAMING_DROPS, count=app.vt.framing_drops))
    if app.host.dropped_controls:
        logger.info(i18n.get(Message.VT_INPUT_DROPS, count=app.host.dropped_controls))
    if app.clipboard.failures:
        logger.warning(
            i18n.get(Message.VT_CLIPBOARD_FAILURES, count=app.clipboard.failures)
        )
    for name, count in app.vt.protocol_failures:
        logger.warning(i18n.get(Message.VT_PROTOCOL_FAILURE, name=name, count=count))
    if app.vt.image_failure_count:
        logger.warning(
            i18n.get(Message.VT_IMAGE_FAILURES, count=app.vt.image_failure_count)
        )
    if app.app.renderer.graphics_failure_count:
        logger.warning(
            i18n.get(
                Message.VT_IMAGE_OUTPUT_ERROR,
                count=app.app.renderer.graphics_failure_count,
            )
        )
    reason = app.host.graphics_reason
    if app.host.profile.image_protocol:
        message = Message.VT_GRAPHICS_AVAILABLE
    elif reason == GRAPHICS_DISABLED:
        message = Message.VT_GRAPHICS_DISABLED
    elif reason in (GRAPHICS_NO_SIZE, GRAPHICS_RESIZED):
        message = Message.VT_GRAPHICS_GEOMETRY
    elif reason in (GRAPHICS_PENDING, GRAPHICS_CANCELLED):
        message = Message.VT_GRAPHICS_PROBE
    else:
        message = Message.VT_GRAPHICS_UNAVAILABLE
    logger.info(i18n.get(Message.VT_GRAPHICS_STATUS, reason=i18n.get(message)))
