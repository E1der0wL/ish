"""Provide the shared translation message manager for this process."""

from .i18n import I18N

i18n = I18N()

__all__ = ["i18n"]
