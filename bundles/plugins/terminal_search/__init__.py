"""An explicitly selected Ctrl+F popup for native scrollback search."""

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from ish.lang import Message, i18n

PLUGIN_META = {"version": "1.0.0", "description": "Native scrollback search popup"}
TERMINAL_API = 1
POPUP_WIDTH = 64
POPUP_HEIGHT = 6


def setup(api):
    """Register a keyboard-only find UI without enabling host mouse capture."""
    popup = None

    def close():
        nonlocal popup
        api.clear_search()
        if popup is not None and popup in api.popups:
            api.close_popup(popup)
        popup = None

    @api.bind("c-f")
    def toggle(event):
        nonlocal popup
        if popup is not None and popup in api.popups:
            close()
            return
        status = ""
        editor = TextArea(multiline=False, wrap_lines=False)
        bindings = KeyBindings()

        async def find(previous=False):
            nonlocal status
            try:
                found = await api.search(editor.text)
                api.select_match(previous=previous)
                status = i18n.get(Message.VT_SEARCH_COUNT, count=found)
            except Exception as error:
                status = i18n.get(Message.VT_SEARCH_ERROR, error=error)
            api.invalidate()

        @bindings.add("enter")
        async def older(event):
            await find()

        @bindings.add("c-p")
        async def newer(event):
            await find(previous=True)

        @bindings.add("escape")
        @bindings.add("c-c")
        @bindings.add("c-d")
        def cancel(event):
            close()

        editor.control.key_bindings = bindings
        content = HSplit(
            [
                Window(
                    FormattedTextControl(lambda: i18n.get(Message.VT_SEARCH_TITLE)),
                    height=1,
                ),
                editor,
                Window(FormattedTextControl(lambda: status), height=1),
                Window(
                    FormattedTextControl(lambda: i18n.get(Message.VT_SEARCH_HINT)),
                    height=1,
                ),
            ]
        )
        popup = api.show_popup(content, width=POPUP_WIDTH, height=POPUP_HEIGHT)

    return close
