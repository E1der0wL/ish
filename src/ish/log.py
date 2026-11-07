"""Shared logger configuration that avoids duplicate console and file handlers."""

from __future__ import annotations

import logging
from threading import RLock
from typing import Iterable

from ish.config import config

__all__ = ["register_logger", "get_logger"]

DEFAULT_LOGGER_NAME = "global"
_CONFIGURED = "_is_configured"
_CONSOLE = "console"
_FILE = "file"
_DEFAULT_HANDLERS = (_CONSOLE,)
_FILE_ENCODING = "utf-8"
_CONSOLE_FORMAT = "[%(levelname)s] %(message)s"
_FILE_FORMAT = "[%(asctime)s] %(levelname)-8s  | %(message)s"
_REGISTRATION_LOCK = RLock()


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
