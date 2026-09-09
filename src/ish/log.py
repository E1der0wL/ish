from __future__ import annotations

import logging
from typing import Optional, Iterable

from ish.config import config

__all__ = ['register_logger', 'get_logger']


def register_logger(
	name: str = "global",
	level: int = logging.ERROR,
	handler: Iterable[str] = ("console", )
) -> None:
	logger = logging.getLogger(name)
	if getattr(logger, "_is_configured", False):
		return None

	logger.setLevel(level)

	for h in handler:
		if h == "file":
			file_handler = logging.FileHandler(str(config.LOG_FILE), encoding="utf-8")
			file_handler.setFormatter(
				logging.Formatter("[%(asctime)s] %(levelname)-8s  | %(message)s")
			)
			logger.addHandler(file_handler)

		elif h == "console":
			console_handler = logging.StreamHandler()
			console_handler.setFormatter(
				logging.Formatter("[%(levelname)s] %(message)s")
			)
			logger.addHandler(console_handler)

		else:
			pass

	logger._is_configured = True
	return None


def get_logger(name: str = "global") -> Optional[logging.Logger]:
	logger = logging.getLogger(name)
	if not getattr(logger, "_is_configured", False):
		register_logger(name, logging.DEBUG)
	return logger
