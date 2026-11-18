"""Explicit shortcuts for native OSC 133 prompt and output metadata."""

PLUGIN_META = {"version": "1.0.0", "description": "Native semantic command navigation"}
TERMINAL_API = 1


def setup(api):
    """Install no shell hooks; absent semantic markers leave navigation inactive."""

    @api.bind("f6")
    def previous(event):
        api.scroll_prompts(-1)

    @api.bind("f7")
    def following(event):
        api.scroll_prompts(1)

    @api.bind("f8")
    def select(event):
        api.select_last_output()

    @api.bind("f9")
    def copy(event):
        api.copy_last_output()
