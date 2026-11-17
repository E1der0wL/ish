"""Opt-in one-row status bar, independent of shell prompts and execution."""

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl

from ish.lang import Message, i18n

PLUGIN_META = {"version": "1.0.0", "description": "One-row terminal status bar"}
TERMINAL_API = 1
BAR_ROWS = 1


def setup(api):
    """Reserve one UI row only when this plugin was explicitly selected."""
    api.set_bar(
        Window(FormattedTextControl(lambda: i18n.get(Message.VT_SAMPLE_BAR))),
        rows=BAR_ROWS,
    )
