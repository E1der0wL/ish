"""An explicitly reserved F2 popup whose text never becomes a shell command."""

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from ish.lang import Message, i18n

PLUGIN_META = {"version": "1.0.0", "description": "Global F2 terminal popup"}
TERMINAL_API = 1
POPUP_WIDTH = 58
POPUP_HEIGHT = 5


def setup(api):
    """Keep one optional popup usable during primary, continuation and programs."""
    popup = None

    def close(result=None):
        nonlocal popup
        if popup is not None:
            api.close_popup(popup, result)
            popup = None

    @api.bind("f2")
    def toggle(event):
        nonlocal popup
        if popup is not None and popup in api.popups:
            close()
            return
        bindings = KeyBindings()
        editor = TextArea(multiline=False, wrap_lines=False)

        @bindings.add("enter")
        def accept(event):
            close(editor.text)

        @bindings.add("escape")
        @bindings.add("c-c")
        @bindings.add("c-d")
        def cancel(event):
            close()

        editor.control.key_bindings = bindings
        content = HSplit(
            [
                Window(
                    FormattedTextControl(
                        lambda: i18n.get(Message.STARTUP_SAMPLE_POPUP)
                    ),
                    height=1,
                ),
                editor,
            ]
        )
        popup = api.show_popup(content, width=POPUP_WIDTH, height=POPUP_HEIGHT)

    return close
