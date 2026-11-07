"""Track a safe prompt prefix while output is a simple, sequential line.

This is deliberately not a terminal emulator. Cursor movement, erasing, screen
switches and other unsupported controls invalidate the prefix for the current
command. The original output still goes to the terminal unchanged.
"""

import re

from .constants import (
    BEL,
    CANCEL_BYTES,
    CSI,
    DEL_BYTE,
    ESC,
    LF_BYTE,
    OSC,
    SPACE_BYTE,
    ST,
)

_DEFAULT_PREFIX_BYTES = 64 * 1024
_TEXT, _CR, _ESCAPE, _CSI, _OSC, _OSC_ESCAPE = (
    "text",
    "cr",
    "escape",
    "csi",
    "osc",
    "osc_escape",
)
_OSC_STATES = (_OSC, _OSC_ESCAPE)
_TITLE_OPERATIONS = (b"0", b"1", b"2")
_TITLE_SEPARATOR = b";"
_SGR_PARAMETERS = b"0123456789;:"
_SGR_FINAL = ord("m")
_ESC = ESC[0]
_BEL = BEL[0]
_CSI_START = CSI[-1]
_OSC_START = OSC[-1]
_ST_FINAL = ST[-1]
_CR_BYTE = ord("\r")
_TAB_BYTE = ord("\t")
# Tab occupies cells; other C0 bytes and DEL need individual handling.
_NON_TEXT_BYTES = (
    bytes(range(_TAB_BYTE))
    + bytes(range(_TAB_BYTE + 1, SPACE_BYTE))
    + bytes([DEL_BYTE])
)
_NON_TEXT = re.compile(b"[" + re.escape(_NON_TEXT_BYTES) + b"]")


class OutputLine:
    """Bounded output tracker that copies only ordinary text and SGR into prompts.

    Stop prefix tracking for the current command on cursor movement or erasure. Do not
    modify raw PTY output or emulate the final screen.
    """

    def __init__(self, limit: int = _DEFAULT_PREFIX_BYTES):
        """Prepare the prefix byte limit and incremental parser state."""
        self.limit = limit
        self.clear()

    def clear(self) -> None:
        """Reset all state to track output from the next command."""
        self._line = bytearray()
        self._control = bytearray()
        self._state = _TEXT
        self._valid = True

    @property
    def prefix(self) -> bytes:
        """Return complete, valid linear output, or empty bytes otherwise."""
        if self._valid and self._state == _TEXT:
            return bytes(self._line)
        return b""

    def _invalidate(self) -> None:
        """Discard the current command's prefix and stop tracking until the next clear."""
        self._valid = False
        self._line.clear()
        self._control.clear()

    def feed(self, data: bytes) -> None:
        """Track CRLF, SGR, and title strings across chunks while collecting a safe prefix."""
        index = 0
        end = len(data)
        while index < end and self._valid:
            byte = data[index]
            index += 1
            state = self._state
            if state == _CR:
                if byte == LF_BYTE:
                    self._line.clear()
                    self._state = _TEXT
                else:
                    self._invalidate()  # A bare CR overwrites existing cells.
            elif state == _TEXT:
                if byte == _CR_BYTE:
                    self._state = _CR
                elif byte == LF_BYTE:
                    self._line.clear()
                elif byte == _ESC:
                    self._state = _ESCAPE
                    self._control[:] = ESC
                elif byte == _BEL:
                    pass  # BEL does not change the displayed line.
                elif byte not in _NON_TEXT_BYTES:
                    start = index - 1
                    room = self.limit - len(self._line)
                    if room < 1:
                        self._invalidate()
                        return
                    # Scan only through the first byte that could overflow. A
                    # later LF cannot rescue a prefix that already overflowed.
                    bound = int(min(end, start + room + 1))
                    control = _NON_TEXT.search(data, index, bound)
                    index = control.start() if control else bound
                    if index - start > room:
                        self._invalidate()
                        return
                    self._line.extend(data[start:index])
                else:
                    self._invalidate()
            elif state == _ESCAPE:
                self._control.append(byte)
                if byte == _CSI_START:
                    self._state = _CSI
                elif byte == _OSC_START:
                    self._state = _OSC
                else:
                    self._invalidate()
            elif state == _CSI:
                self._control.append(byte)
                if byte == _SGR_FINAL:
                    self._line.extend(self._control)
                    self._control.clear()
                    self._state = _TEXT
                elif byte not in _SGR_PARAMETERS:
                    self._invalidate()
            elif state in _OSC_STATES:
                if byte == _BEL or state == _OSC_ESCAPE and byte == _ST_FINAL:
                    # Titles do not occupy cells. Other OSC operations may.
                    if (
                        self._control[len(OSC) :].partition(_TITLE_SEPARATOR)[0]
                        not in _TITLE_OPERATIONS
                    ):
                        self._invalidate()
                    self._control.clear()
                    self._state = _TEXT
                elif state == _OSC_ESCAPE or byte in CANCEL_BYTES:
                    self._invalidate()
                elif byte == _ESC:
                    self._state = _OSC_ESCAPE
                else:
                    self._control.append(byte)
            if len(self._line) + len(self._control) > self.limit:
                self._invalidate()
