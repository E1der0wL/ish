"""Example primary-prompt bar, editable independently of the stream host."""

PLUGIN_META = {"version": "1.0.0", "description": "Native shell demonstration bar"}
STREAM_API = 1
BAR_TEXT = "ish stream | native shell | F2 popup"
BAR_STYLE = "fg:ansicyan"


def setup(api):
    """Register content without writing to the terminal or reserving input."""
    api.set_bar([(BAR_STYLE, BAR_TEXT)])
