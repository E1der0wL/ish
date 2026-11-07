"""Provide the shared translation message manager for this process."""

from .i18n import I18N, Message

i18n = I18N()

__all__ = ["Message", "i18n"]
