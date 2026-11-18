"""Shared logger configuration that avoids duplicate console and file handlers."""

from __future__ import annotations

import errno
import logging
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterable, TextIO

from ish.config import config

__all__ = ["register_logger", "get_logger", "session_logging", "LOG_LEVELS"]

DEFAULT_LOGGER_NAME = "global"
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_CONFIGURED = "_is_configured"
_CONSOLE = "console"
_FILE = "file"
_DEFAULT_HANDLERS = (_CONSOLE,)
_FILE_ENCODING = "utf-8"
_CONSOLE_FORMAT = "[%(levelname)s] %(message)s"
_FILE_FORMAT = "[%(asctime)s] %(levelname)-8s  | %(message)s"
_REGISTRATION_LOCK = RLock()
_BUFFER_MEMORY_BYTES = 64 * 1024


def register_logger(
    name: str = DEFAULT_LOGGER_NAME,
    level: int = logging.ERROR,
    handler: Iterable[str] = _DEFAULT_HANDLERS,
) -> None:
    """Register each requested console or file handler only once."""
    logger = logging.getLogger(name)
    with _REGISTRATION_LOCK:
        if getattr(logger, _CONFIGURED, False):
            return
        pending = []
        try:
            for target in dict.fromkeys(handler):
                sink: logging.Handler
                if target == _FILE:
                    sink = logging.FileHandler(config.LOG_FILE, encoding=_FILE_ENCODING)
                    pattern = _FILE_FORMAT
                elif target == _CONSOLE:
                    sink = logging.StreamHandler()
                    pattern = _CONSOLE_FORMAT
                else:
                    continue
                pending.append(sink)
                sink.setFormatter(logging.Formatter(pattern))
            logger.setLevel(level)
            for sink in pending:
                logger.addHandler(sink)
            setattr(logger, _CONFIGURED, True)
        except BaseException:
            for sink in pending:
                logger.removeHandler(sink)
                sink.close()
            raise


def get_logger(name: str = DEFAULT_LOGGER_NAME) -> logging.Logger:
    """Return the named logger and create default handlers if needed."""
    logger = logging.getLogger(name)
    if not getattr(logger, _CONFIGURED, False):
        register_logger(name, logging.DEBUG)
    return logger


class _SessionErrors:
    """Defer logging I/O failures instead of stdlib's direct stderr traceback."""

    def handleError(self, record):
        """Report only the failure; never echo a record into the live terminal."""
        self.on_error(sys.exc_info()[1])


class _DeferredHandler(_SessionErrors, logging.StreamHandler):
    """Buffer diagnostics without allowing handler failures to write stderr."""

    def __init__(self, stream, on_error):
        """Install a separate failure sink before accepting records."""
        self.on_error = on_error
        super().__init__(stream)


class _SessionFileHandler(_SessionErrors, logging.FileHandler):
    """Append to the selected file while deferring runtime write errors."""

    PERMISSIONS = 0o666
    OPEN_FLAGS = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NONBLOCK", 0)

    def __init__(self, path, on_error):
        """Use the existing UTF-8 file format and a deferred failure sink."""
        self.on_error = on_error
        super().__init__(path, encoding=_FILE_ENCODING)

    def _open(self):
        """Reject terminal and pipe sinks using the opened descriptor, without waiting.

        Checking the path before opening it would race with symlink replacement,
        and opening a FIFO normally could block before its type is checked.
        """
        from ish.lang import Message, i18n

        descriptor = os.open(self.baseFilename, self.OPEN_FLAGS, self.PERMISSIONS)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(
                    errno.EINVAL,
                    i18n.get(Message.LOG_DESTINATION),
                    self.baseFilename,
                )
            return os.fdopen(
                descriptor, self.mode, encoding=self.encoding, errors=self.errors
            )
        except BaseException:
            os.close(descriptor)
            raise


class SessionLog:
    """Keep diagnostic console output away from a live terminal byte relay.

    File records are flushed as they arrive. Console records are retained until
    the owner calls flush at a safe boundary or leaves session_logging. Large
    sessions spill to a temporary file instead of dropping records or retaining
    an unbounded in-memory queue. No terminal or shell input is captured here.
    """

    FLUSH_CHARACTERS = 64 * 1024

    def __init__(self, logger: logging.Logger, buffer: TextIO):
        """Bind the shared logger to the private deferred console stream."""
        self.logger = logger
        self._buffer = buffer
        self._error = None
        self._console = _DeferredHandler(buffer, self._failed)
        self._console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))

    def _failed(self, error):
        if self._error is None:
            self._error = error

    def flush(self, stream: TextIO | None = None) -> None:
        """Write deferred diagnostics once, only when the caller owns the terminal."""
        destination = sys.stderr if stream is None else stream
        # Serialize the rewind/copy/truncate operation with logging emissions.
        with self._console.lock:
            try:
                self._console.flush()
                self._buffer.seek(0)
                while chunk := self._buffer.read(self.FLUSH_CHARACTERS):
                    destination.write(chunk)
                self._buffer.seek(0)
                self._buffer.truncate()
            except (OSError, ValueError) as error:
                self._failed(error)
            if self._error is not None:
                from ish.lang import Message, i18n

                message = i18n.get(Message.LOG_OUTPUT_ERROR, error=self._error)
                destination.write(f"[ERROR] {message}\n")
                self._error = None
            destination.flush()


def _level(value: str | int) -> int:
    """Normalize supported public levels without configuring a logger."""
    from ish.lang import Message, i18n

    if isinstance(value, str):
        try:
            return LOG_LEVELS[value.upper()]
        except KeyError as error:
            raise ValueError(i18n.get(Message.LOG_LEVEL_ERROR, value=value)) from error
    if type(value) is int and value in LOG_LEVELS.values():
        return value
    raise ValueError(i18n.get(Message.LOG_LEVEL_ERROR, value=value))


@contextmanager
def session_logging(
    *,
    level: str | int = logging.WARNING,
    path: str | Path | None = None,
    name: str = DEFAULT_LOGGER_NAME,
) -> Iterator[SessionLog]:
    """Temporarily configure the existing logger for one terminal session.

    Enter before loading plugins and leave only after terminal restoration.
    Existing handlers are detached, not closed, and restored on every exit.
    This also applies an explicit level when get_logger initialized the logger
    earlier. Configure one application at a time; nested scopes must be LIFO.
    """
    selected_level = _level(level)
    logger = logging.getLogger(name)
    sinks = []
    with tempfile.SpooledTemporaryFile(
        max_size=_BUFFER_MEMORY_BYTES, mode="w+", encoding=_FILE_ENCODING
    ) as buffer:
        session = SessionLog(logger, buffer)
        sinks.append(session._console)
        try:
            if path is not None:
                destination = Path(path).expanduser()
                destination.parent.mkdir(parents=True, exist_ok=True)
                sink = _SessionFileHandler(destination, session._failed)
                sink.setFormatter(logging.Formatter(_FILE_FORMAT))
                sinks.append(sink)
            with _REGISTRATION_LOCK:
                previous_handlers = list(logger.handlers)
                previous_level = logger.level
                previous_propagation = logger.propagate
                previous_disabled = logger.disabled
                was_configured = hasattr(logger, _CONFIGURED)
                previous_configured = getattr(logger, _CONFIGURED, None)
                logger.handlers = list(sinks)
                logger.setLevel(selected_level)
                logger.propagate = False
                logger.disabled = False
                setattr(logger, _CONFIGURED, True)
            try:
                yield session
            finally:
                with _REGISTRATION_LOCK:
                    logger.handlers = previous_handlers
                    logger.setLevel(previous_level)
                    logger.propagate = previous_propagation
                    logger.disabled = previous_disabled
                    if was_configured:
                        setattr(logger, _CONFIGURED, previous_configured)
                    else:
                        delattr(logger, _CONFIGURED)
                session.flush()
        finally:
            for sink in sinks:
                sink.close()
