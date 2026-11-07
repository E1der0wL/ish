"""Track terminal mode state that may need cleanup after interrupted TUIs."""

from .constants import ALTERNATE_SCREENS, CSI, ESC
from .sequencer import Sequencer

_PRIVATE_MODE = CSI + b"?"
_SET = b"h"
_RESET = b"l"
_MODE_FINALS = (_SET, _RESET)
_MAX_MODE_BYTES = 128
_MAX_PARAMETER_DIGITS = 4
_PARAMETER_SEPARATOR = b";"
_TERMINAL_RESET = ESC + b"c"
_KEYPAD_APPLICATION = ESC + b"="
_KEYPAD_NUMERIC = ESC + b">"
_KEYPAD_MODES = (_KEYPAD_APPLICATION, _KEYPAD_NUMERIC)
_SGR = b"m"
_SGR_RESET = CSI + b"0" + _SGR
_SGR_RESETS = (CSI + _SGR, _SGR_RESET)


class TerminalState:
    """Track mode controls without emulating a screen or changing live output.

    Defaults describe the ordinary caller prompt. Only observed changes generate
    restoration bytes; this is not a query of arbitrary pre-existing terminal state.
    """

    DEFAULTS = {
        1: False,
        25: True,
        **dict.fromkeys(ALTERNATE_SCREENS, False),
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
        if sequence == _TERMINAL_RESET:
            self.modes.clear()
            self.keypad = self.styled = False
        elif sequence.startswith(_PRIVATE_MODE) and sequence.endswith(_MODE_FINALS):
            # Avoid parsing huge or private extensions as ordinary DEC modes.
            if len(sequence) > _MAX_MODE_BYTES:
                return
            enabled = sequence.endswith(_SET)
            for parameter in sequence[len(_PRIVATE_MODE) : -1].split(
                _PARAMETER_SEPARATOR
            ):
                if len(parameter) <= _MAX_PARAMETER_DIGITS and parameter.isdigit():
                    mode = int(parameter)
                    if mode in self.DEFAULTS:
                        if enabled == self.DEFAULTS[mode]:
                            self.modes.pop(mode, None)
                        else:
                            self.modes[mode] = enabled
        elif sequence in _KEYPAD_MODES:
            self.keypad = sequence == _KEYPAD_APPLICATION
        elif sequence.startswith(CSI) and sequence.endswith(_SGR):
            self.styled = sequence not in _SGR_RESETS

    def feed(self, data: bytes) -> None:
        """Inspect raw tool output, skipping Python parsing for plain text chunks."""
        if self.parser.state != Sequencer.GROUND or ESC in data:
            self.parser.feed(data)

    def restore(self) -> bytes:
        """Return minimal common-mode resets for a terminating session."""
        result = bytearray()
        for mode in sorted(
            self.modes, key=lambda value: value not in ALTERNATE_SCREENS
        ):
            result.extend(
                _PRIVATE_MODE
                + str(mode).encode("ascii")
                + (_SET if self.DEFAULTS[mode] else _RESET)
            )
        if self.keypad:
            result.extend(_KEYPAD_NUMERIC)
        if self.styled:
            result.extend(_SGR_RESET)
        return bytes(result)
