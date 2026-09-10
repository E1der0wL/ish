"""Retain a prompt prefix only while output is a simple, sequential line.

This is deliberately not a terminal emulator. Cursor movement, erasing, screen
switches and other unsupported controls invalidate the prefix for the current
command. The original output still goes to the terminal unchanged.
"""


class OutputLine:
    """Bounded output tracker that copies only ordinary text and SGR into prompts.

    Stop prefix tracking for the current command on cursor movement or erasure. Do not
    modify raw PTY output or emulate the final screen.
    """

    def __init__(self, limit: int = 65536):
        """Prepare the prefix byte limit and incremental parser state."""
        self.limit = limit
        self.clear()

    def clear(self) -> None:
        """Reset all state to track output from the next command."""
        self._line = bytearray()
        self._control = bytearray()
        self._state = "text"
        self._valid = True

    @property
    def prefix(self) -> bytes:
        """Return complete, valid linear output, or empty bytes otherwise."""
        if self._valid and self._state == "text":
            return bytes(self._line)
        return b""

    def _invalidate(self) -> None:
        """Discard the current command's prefix and stop tracking until the next clear."""
        self._valid = False
        self._line.clear()
        self._control.clear()

    def feed(self, data: bytes) -> None:
        """Track CRLF, SGR, and title strings across chunks while collecting a safe prefix."""
        for byte in data:
            if not self._valid:
                return
            state = self._state
            if state == "cr":
                if byte == 0x0A:
                    self._line.clear()
                    self._state = "text"
                else:
                    self._invalidate()  # A bare CR overwrites existing cells.
            elif state == "text":
                if byte == 0x0D:
                    self._state = "cr"
                elif byte == 0x0A:
                    self._line.clear()
                elif byte == 0x1B:
                    self._state = "escape"
                    self._control[:] = b"\x1b"
                elif byte == 0x07:
                    pass  # BEL does not change the displayed line.
                elif byte == 0x09 or byte >= 0x20 and byte != 0x7F:
                    self._line.append(byte)
                else:
                    self._invalidate()
            elif state == "escape":
                self._control.append(byte)
                if byte == ord("["):
                    self._state = "csi"
                elif byte == ord("]"):
                    self._state = "osc"
                else:
                    self._invalidate()
            elif state == "csi":
                self._control.append(byte)
                if byte == ord("m"):
                    self._line.extend(self._control)
                    self._control.clear()
                    self._state = "text"
                elif byte not in b"0123456789;:":
                    self._invalidate()
            elif state in ("osc", "osc_escape"):
                if byte == 0x07 or state == "osc_escape" and byte == ord("\\"):
                    # Titles do not occupy cells. Other OSC operations may.
                    if self._control[2:].partition(b";")[0] not in (b"0", b"1", b"2"):
                        self._invalidate()
                    self._control.clear()
                    self._state = "text"
                elif state == "osc_escape" or byte in (0x18, 0x1A):
                    self._invalidate()
                elif byte == 0x1B:
                    self._state = "osc_escape"
                else:
                    self._control.append(byte)
            if len(self._line) + len(self._control) > self.limit:
                self._invalidate()
