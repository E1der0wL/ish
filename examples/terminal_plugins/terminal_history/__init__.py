"""Opt-in page keys for scrolling the primary terminal screen."""

PLUGIN_META = {"version": "1.0.0", "description": "Scroll history by page"}
TERMINAL_API = 1


def setup(api):
    """Leave both keys available to alternate-screen programs and popup widgets."""

    @api.bind("s-pageup", primary_only=True)
    def previous(event):
        api.scroll_pages(-1)

    @api.bind("s-pagedown", primary_only=True)
    def following(event):
        api.scroll_pages(1)
