"""Explicit GUI-test plugin; production installs none of these bindings."""

import os
from pathlib import Path

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.widgets import TextArea

PLUGIN_META = {"version": "1", "requirements": [], "dependencies": []}
TERMINAL_API = 1


def plugin_options(directory):
    """Install this opt-in fixture through the public PluginManager file layout."""
    scripts = directory / "plugins"
    package = scripts / "terminal_test"
    package.mkdir(parents=True, exist_ok=True)
    (package / "terminal_test.py").write_bytes(Path(__file__).read_bytes())
    return [
        "--plugin-dir",
        str(scripts),
        "--library-dir",
        str(directory / "libraries"),
        "--plugin",
        "terminal_test",
    ]


def setup(plugins):
    """Install a movable opaque popup only when the test requests this plugin."""
    directory = Path(os.environ["ISH_GUI_DIR"])
    popup = None
    area = None

    def close():
        nonlocal popup
        if popup is not None:
            plugins.close_popup(popup)
            popup = None

    @plugins.bind("f2")
    def toggle(event):
        nonlocal popup, area
        if popup is not None:
            close()
            return
        bindings = KeyBindings()

        @bindings.add("escape")
        def escape(event):
            close()

        area = TextArea(
            text="ISH_POPUP\n",
            multiline=True,
            wrap_lines=False,
            style="bg:#30a050 fg:#ffffff",
        )
        area.control.key_bindings = bindings
        area.buffer.on_text_changed += lambda _: (directory / "popup-input").write_text(
            area.text
        )
        popup = plugins.show_popup(area, width=12, height=4, left=8, top=5)

    @plugins.bind("f3")
    def move(event):
        if popup is not None:
            popup.floating.left = 30
            popup.floating.top = 13
            plugins.owner.app.invalidate()

    return close
