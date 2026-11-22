"""Opt-in one-row status bar, independent of shell prompts and execution."""

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl

from ish.lang import Message, i18n

PLUGIN_META = {
    "version": "1.0.0",
    "description": "One-row terminal status bar",
    "settings": {
        "label": {
            "title": {"en": "Bar text", "ko": "하단 바 문구"},
            "description": {
                "en": "Leave empty to use the translated status text.",
                "ko": "비워 두면 현재 언어의 기본 문구를 표시합니다.",
            },
            "default": "",
        },
    },
}
TERMINAL_API = 1
BAR_ROWS = 1


def setup(api):
    """Reserve one UI row only when this plugin was explicitly selected."""
    label = api.settings_for("terminal_bar")["label"]
    api.set_bar(
        Window(FormattedTextControl(lambda: label or i18n.get(Message.VT_SAMPLE_BAR))),
        rows=BAR_ROWS,
    )
