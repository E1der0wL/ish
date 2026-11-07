"""Apply the selected shell's default word motion to recovered Emacs editing keys."""

from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.filters import emacs_insert_mode, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from ish.shell.adapter import ShellAdapter

_PRIMARY_EMACS = emacs_insert_mode & has_focus(DEFAULT_BUFFER)
_BACKWARD_WORD = (Keys.Escape, "b")
_FORWARD_WORD = (Keys.Escape, "f")


def native_word_bindings(adapter: ShellAdapter) -> KeyBindings:
    """Register the adapter's word motions only in the foreground replay registry."""
    bindings = KeyBindings()
    policy = adapter.word_motion
    if policy is None:
        return bindings

    def move(event, count):
        buffer = event.current_buffer
        buffer.cursor_position = policy.position(
            buffer.text, buffer.cursor_position, count
        )

    @bindings.add(*_BACKWARD_WORD, filter=_PRIMARY_EMACS)
    def backward(event):
        move(event, -event.arg)

    @bindings.add(*_FORWARD_WORD, filter=_PRIMARY_EMACS)
    def forward(event):
        move(event, event.arg)

    if policy.control_arrows:
        bindings.add(Keys.ControlLeft, filter=_PRIMARY_EMACS)(backward)
        bindings.add(Keys.ControlRight, filter=_PRIMARY_EMACS)(forward)
    return bindings
