"""Provide the plugin manager instance shared by the CLI."""

from .manager import PluginManager

plugin_manager = PluginManager()

__all__ = ["plugin_manager"]
