"""ANSI formatted prompts with complete control-sequence boundaries.

Control strings are dispatched separately from visible text. This is a prompt
formatter, not a terminal emulator: unknown controls are consumed, not executed.
The parser override uses prompt_toolkit internals; tested with version 3.0.53.
"""

from __future__ import annotations

import re
from collections.abc import Generator

from prompt_toolkit.formatted_text import ANSI

__all__ = ["ShellANSI"]

# The control hook exposes the VT family names; the parser state stays private.
_OSC = "OSC"
_CSI = "CSI"
_ESC = "ESC"
_C0 = "C0"
_TEXT, _ESCAPE, _CSI_BODY, _STRING, _STRING_ESCAPE, _ZERO_WIDTH = range(6)
_ESC_BYTE = "\x1b"
_C1_OFFSET = 0x40
_CSI_START = "["
_C1_CSI = "\x9b"
_CANCEL = ("\x18", "\x1a")  # CAN, SUB
_C1_ST = "\x9c"
_ST_FINAL = "\\"
_BEL = "\x07"
_SOH, _STX = "\x01", "\x02"  # prompt-toolkit zero-width annotation
_TEXT_CONTROLS = "\t\n\r"
_PRINTABLE_START = " "
_DEL, _C1_LAST = "\x7f", "\x9f"
_INTERMEDIATE_MIN, _INTERMEDIATE_MAX = " ", "/"
_ESC_FINAL_MIN, _CSI_FINAL_MIN, _FINAL_MAX = "0", "@", "~"
_CSI_PARAMETER_MAX = "?"
_PARAM_SEPARATOR = ";"
_TITLE_COMMANDS = ("0", "2")
_ICON_COMMANDS = ("0", "1")
_PTK_FINALS = ("m", "C")
# PTK accepts decimal/empty parameters. Look for a disallowed character or a
# five-digit field without allocating a match stack for thousands of separators.
_MAX_PTK_DIGITS = 4
_UNSUPPORTED_PARAMETER = re.compile(
    rf"[^0-9{_PARAM_SEPARATOR}]|[0-9]{{{_MAX_PTK_DIGITS + 1}}}"
)


class ShellANSI(ANSI):
    """Keep ANSI's styling and add an overridable control dispatch hook.

    ``handle_control(kind, payload)`` receives complete unsupported sequences,
    without their introducer or terminator. The default stores OSC titles as
    metadata and ignores other controls. It never writes to the terminal while
    formatting, since prompt_toolkit may redraw a prompt repeatedly.
    """

    _STRING_STARTS = {"P": "DCS", "X": "SOS", "]": _OSC, "^": "PM", "_": "APC"}
    _C1_STARTS = {
        chr(ord(final) + _C1_OFFSET): kind for final, kind in _STRING_STARTS.items()
    }
    _MAX_CONTROL = 65536

    def __init__(self, value: str) -> None:
        """Prepare title metadata and parse content through the parent ANSI formatter."""
        self.title: str | None = None
        self.icon_title: str | None = None
        super().__init__(value)

    def handle_control(self, kind: str, payload: str) -> None:
        """Override to add OSC or other control handling; call super for titles."""
        if kind == _OSC:
            command, separator, text = payload.partition(_PARAM_SEPARATOR)
            if separator:
                if command in _TITLE_COMMANDS:
                    self.title = text
                if command in _ICON_COMMANDS:
                    self.icon_title = text

    def _parse_corot(self) -> Generator[None, str, None]:
        """Separate control sequences from text and pass only supported styles to the
        parent.
        """
        parent = super()._parse_corot()
        next(parent)
        state = _TEXT
        kind = ""
        buffer: list[str] = []
        overflow = False

        def append(char: str) -> None:
            """Retain control payload within the limit and record overflow."""
            nonlocal overflow
            if len(buffer) < self._MAX_CONTROL:
                buffer.append(char)
            else:
                overflow = True

        def finish() -> None:
            """Handle a complete control sequence and return to ordinary text parsing."""
            nonlocal state
            if not overflow:
                self.handle_control(kind, "".join(buffer))
            buffer.clear()
            state = _TEXT

        try:
            while True:
                char = yield

                if state == _TEXT and (
                    _PRINTABLE_START <= char < _DEL
                    or char > _C1_LAST
                    or char in _TEXT_CONTROLS
                ):
                    parent.send(char)
                    continue

                # Preserve prompt_toolkit's explicit zero-width escape contract.
                if state == _ZERO_WIDTH:
                    parent.send(char)
                    if char == _STX:
                        state = _TEXT
                    continue

                if state == _STRING or state == _STRING_ESCAPE:
                    if char in _CANCEL:
                        buffer.clear()
                        state = _TEXT
                    elif char == _C1_ST or (kind == _OSC and char == _BEL):
                        finish()
                    elif state == _STRING_ESCAPE and char == _ST_FINAL:
                        finish()
                    else:
                        if state == _STRING_ESCAPE:
                            append(_ESC_BYTE)
                        if char == _ESC_BYTE:
                            state = _STRING_ESCAPE
                        else:
                            append(char)
                            state = _STRING
                    continue

                if char == _ESC_BYTE:
                    state = _ESCAPE
                    buffer.clear()
                    overflow = False
                    continue

                if state == _ESCAPE:
                    if not buffer and char in self._STRING_STARTS:
                        kind = self._STRING_STARTS[char]
                        state = _STRING
                    elif not buffer and char == _CSI_START:
                        kind = _CSI
                        state = _CSI_BODY
                    elif _INTERMEDIATE_MIN <= char <= _INTERMEDIATE_MAX:
                        append(char)
                    elif _ESC_FINAL_MIN <= char <= _FINAL_MAX:
                        kind = _ESC
                        append(char)
                        finish()
                    else:
                        buffer.clear()
                        state = _TEXT
                    continue

                if state == _CSI_BODY:
                    if _CSI_FINAL_MIN <= char <= _FINAL_MAX:
                        # Delegate only the grammar and operations ANSI supports.
                        # Otherwise e.g. CSI ?25l leaks '25l' through its parser.
                        if (
                            not overflow
                            and char in _PTK_FINALS
                            and not _UNSUPPORTED_PARAMETER.search("".join(buffer))
                        ):
                            parent.send(_ESC_BYTE)
                            parent.send(_CSI_START)
                            for part in buffer:
                                parent.send(part)
                            parent.send(char)
                            buffer.clear()
                            state = _TEXT
                        else:
                            append(char)
                            finish()
                    elif _INTERMEDIATE_MIN <= char <= _CSI_PARAMETER_MAX:
                        append(char)
                    else:
                        buffer.clear()
                        state = _TEXT
                    continue

                if char in self._C1_STARTS or char == _C1_CSI:
                    buffer.clear()
                    overflow = False
                    kind = self._C1_STARTS.get(char, _CSI)
                    state = _CSI_BODY if kind == _CSI else _STRING
                elif char == _SOH:
                    parent.send(char)
                    state = _ZERO_WIDTH
                else:
                    self.handle_control(_C0, char)
        finally:
            parent.close()

    def format(self, *args: str, **kwargs: str) -> ShellANSI:
        """Preserve ShellANSI parsing and metadata handling after string formatting."""
        return type(self)(super().format(*args, **kwargs).value)

    def __mod__(self, value: object) -> ShellANSI:
        """Return a ShellANSI instance after percent formatting."""
        return type(self)(super().__mod__(value).value)
