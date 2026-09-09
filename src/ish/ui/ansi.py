"""ANSI formatted prompts with complete control-sequence boundaries.

Control strings are dispatched separately from visible text. This is a prompt
formatter, not a terminal emulator: unknown controls are consumed, not executed.
The parser override uses prompt_toolkit internals; tested with version 3.0.53.
"""

from __future__ import annotations

from collections.abc import Generator

from prompt_toolkit.formatted_text import ANSI

__all__ = ["ShellANSI"]


class ShellANSI(ANSI):
    """Keep ANSI's styling and add an overridable control dispatch hook.

    ``handle_control(kind, payload)`` receives complete unsupported sequences,
    without their introducer or terminator. The default stores OSC titles as
    metadata and ignores other controls. It never writes to the terminal while
    formatting, since prompt_toolkit may redraw a prompt repeatedly.
    """

    _STRING_STARTS = {"P": "DCS", "X": "SOS", "]": "OSC", "^": "PM", "_": "APC"}
    _C1_STARTS = {"\x90": "DCS", "\x98": "SOS", "\x9d": "OSC", "\x9e": "PM", "\x9f": "APC"}
    _MAX_CONTROL = 65536

    def __init__(self, value: str) -> None:
        self.title: str | None = None
        self.icon_title: str | None = None
        super().__init__(value)

    def handle_control(self, kind: str, payload: str) -> None:
        """Override to add OSC or other control handling; call super for titles."""
        if kind == "OSC":
            command, separator, text = payload.partition(";")
            if separator:
                if command in ("0", "2"):
                    self.title = text
                if command in ("0", "1"):
                    self.icon_title = text

    def _parse_corot(self) -> Generator[None, str, None]:
        parent = super()._parse_corot()
        next(parent)
        state = "text"
        kind = ""
        buffer: list[str] = []
        overflow = False

        def append(char: str) -> None:
            nonlocal overflow
            if len(buffer) < self._MAX_CONTROL:
                buffer.append(char)
            else:
                overflow = True

        def finish() -> None:
            nonlocal state
            if not overflow:
                self.handle_control(kind, "".join(buffer))
            buffer.clear()
            state = "text"

        try:
            while True:
                char = yield

                # Preserve prompt_toolkit's explicit zero-width escape contract.
                if state == "zero_width":
                    parent.send(char)
                    if char == "\x02":
                        state = "text"
                    continue

                if state in ("string", "string_escape"):
                    if char in ("\x18", "\x1a"):  # CAN / SUB cancel the control.
                        buffer.clear()
                        state = "text"
                    elif char == "\x9c" or (kind == "OSC" and char == "\x07"):
                        finish()
                    elif state == "string_escape" and char == "\\":
                        finish()
                    else:
                        if state == "string_escape":
                            append("\x1b")
                        if char == "\x1b":
                            state = "string_escape"
                        else:
                            append(char)
                            state = "string"
                    continue

                if char == "\x1b":
                    state = "escape"
                    buffer.clear()
                    overflow = False
                    continue

                if state == "escape":
                    if not buffer and char in self._STRING_STARTS:
                        kind = self._STRING_STARTS[char]
                        state = "string"
                    elif not buffer and char == "[":
                        kind = "CSI"
                        state = "csi"
                    elif " " <= char <= "/":
                        append(char)
                    elif "0" <= char <= "~":
                        kind = "ESC"
                        append(char)
                        finish()
                    else:
                        buffer.clear()
                        state = "text"
                    continue

                if state == "csi":
                    if "@" <= char <= "~":
                        # Delegate only the grammar and operations ANSI supports.
                        # Otherwise e.g. CSI ?25l leaks '25l' through its parser.
                        if (not overflow and char in ("m", "C")
                                and all(c in "0123456789;" for c in buffer)
                                and all(len(p) <= 4 for p in "".join(buffer).split(";"))):
                            for part in ("\x1b", "[", *buffer, char):
                                parent.send(part)
                            buffer.clear()
                            state = "text"
                        else:
                            append(char)
                            finish()
                    elif " " <= char <= "?":
                        append(char)
                    else:
                        buffer.clear()
                        state = "text"
                    continue

                if char in self._C1_STARTS or char == "\x9b":
                    buffer.clear()
                    overflow = False
                    kind = self._C1_STARTS.get(char, "CSI")
                    state = "csi" if kind == "CSI" else "string"
                elif char == "\x01":
                    parent.send(char)
                    state = "zero_width"
                elif char in "\t\n\r" or (char >= " " and not "\x7f" <= char <= "\x9f"):
                    parent.send(char)
                else:
                    self.handle_control("C0", char)
        finally:
            parent.close()

    def format(self, *args: str, **kwargs: str) -> ShellANSI:
        return type(self)(super().format(*args, **kwargs).value)

    def __mod__(self, value: object) -> ShellANSI:
        return type(self)(super().__mod__(value).value)
