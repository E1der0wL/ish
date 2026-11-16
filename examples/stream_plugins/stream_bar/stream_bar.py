"""Example primary-prompt bar, editable independently of the stream host."""

from ish.lang import Message, i18n

PLUGIN_META = {"version": "1.0.0", "description": "Native shell demonstration bar"}
STREAM_API = 1
BAR_MESSAGE = Message.STREAM_SAMPLE_BAR
BAR_STYLE = "fg:ansicyan"


def setup(api):
    """Register content without writing to the terminal or reserving input."""
    api.set_bar([(BAR_STYLE, i18n.get(BAR_MESSAGE))])
