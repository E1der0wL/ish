"""Example editable PTK popup using the host's confirmed native handoff."""

from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from ish.lang import Message, i18n
from ish.stream.ui import PopupView

PLUGIN_META = {"version": "1.0.0", "description": "Native handoff demonstration popup"}
STREAM_API = 1
POPUP_MESSAGE = Message.STREAM_SAMPLE_POPUP
TITLE_STYLE = "bold"


def create(accept):
    """Create a new editor whose result stays inside the popup contract."""

    def submitted(buffer):
        accept(buffer.text)
        return True

    field = TextArea(
        height=1,
        multiline=False,
        wrap_lines=False,
        accept_handler=submitted,
    )
    content = HSplit(
        [
            Window(
                FormattedTextControl(i18n.get(POPUP_MESSAGE)),
                height=1,
                style=TITLE_STYLE,
            ),
            field,
        ]
    )
    return PopupView(content, field)


def setup(api):
    """Supply popup content while the host keeps focus and input ownership."""
    api.set_popup(create)
