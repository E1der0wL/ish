"""Track terminal mode state that may need cleanup after interrupted TUIs."""

from .sequencer import Sequencer


class TerminalState:
    """Track mode controls without emulating a screen or changing live output.

    Defaults describe the ordinary caller prompt. Only observed changes generate
    restoration bytes; this is not a query of arbitrary pre-existing terminal state.
    """

    DEFAULTS = {
        1: False,
        25: True,
        47: False,
        1047: False,
        1049: False,
        1000: False,
        1002: False,
        1003: False,
        1004: False,
        1005: False,
        1006: False,
        1015: False,
        2004: False,
    }

    def __init__(self):
        """Prepare bounded mode state and a parser for otherwise unparsed tool output."""
        self.modes = {}
        self.keypad = False
        self.styled = False
        self.parser = Sequencer(control_callback=self.observe)

    def observe(self, sequence: bytes) -> None:
        """Observe complete controls already parsed by the shell's sequencer."""
        if sequence == b"\x1bc":
            self.modes.clear()
            self.keypad = self.styled = False
        elif sequence.startswith(b"\x1b[?") and sequence[-1:] in (b"h", b"l"):
            # Avoid parsing huge or private extensions as ordinary DEC modes.
            if len(sequence) > 128:
                return
            for parameter in sequence[3:-1].split(b";"):
                if parameter.isdigit() and len(parameter) <= 4:
                    mode = int(parameter)
                    if mode in self.DEFAULTS:
                        enabled = sequence.endswith(b"h")
                        if enabled == self.DEFAULTS[mode]:
                            self.modes.pop(mode, None)
                        else:
                            self.modes[mode] = enabled
        elif sequence in (b"\x1b=", b"\x1b>"):
            self.keypad = sequence == b"\x1b="
        elif sequence.startswith(b"\x1b[") and sequence.endswith(b"m"):
            self.styled = sequence not in (b"\x1b[m", b"\x1b[0m")

    def feed(self, data: bytes) -> None:
        """Inspect raw tool output, skipping Python parsing for plain text chunks."""
        if self.parser.state != Sequencer.GROUND or b"\x1b" in data:
            self.parser.interpret(data)

    def restore(self) -> bytes:
        """Return minimal common-mode resets for a terminating session."""
        result = bytearray()
        for mode in sorted(self.modes, key=lambda value: value not in (47, 1047, 1049)):
            result.extend(f"\x1b[?{mode}{'h' if self.DEFAULTS[mode] else 'l'}".encode())
        if self.keypad:
            result.extend(b"\x1b>")
        if self.styled:
            result.extend(b"\x1b[0m")
        return bytes(result)
