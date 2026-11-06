"""Apply the selected shell's default word motion to recovered Emacs editing keys."""

from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.filters import emacs_insert_mode, has_focus
from prompt_toolkit.key_binding import KeyBindings

from ish.shell.adapter import ShellAdapter


def native_word_bindings(adapter: ShellAdapter) -> KeyBindings:
    """Register the adapter's word motions only in the foreground replay registry."""
    bindings = KeyBindings()
    policy = adapter.word_motion
    if policy is None:
        return bindings

    primary = emacs_insert_mode & has_focus(DEFAULT_BUFFER)

    def move(event, count):
        buffer = event.current_buffer
        buffer.cursor_position = policy.position(
            buffer.text, buffer.cursor_position, count
        )

    @bindings.add("escape", "b", filter=primary)
    def backward(event):
        move(event, -event.arg)

    @bindings.add("escape", "f", filter=primary)
    def forward(event):
        move(event, event.arg)

    if policy.control_arrows:
        bindings.add("c-left", filter=primary)(backward)
        bindings.add("c-right", filter=primary)(forward)
    return bindings
