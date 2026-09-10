"""FIFO framing: SOH ISH2 RS (category US base64(body) RS ... ) EOT.

Version 2 bodies cannot contain frame/record delimiters. Environments use
NUL-separated `env -0` records to preserve embedded newlines in values.
Legacy unversioned frames remain readable during integration script updates.
"""

import base64

SOH, RS, US, EOT = b"\x01", b"\x1e", b"\x1f", b"\x04"
VERSION = b"ISH2"


class FrameDecoder:
    """Decode multiple FIFO frames and partial tails within a byte limit."""

    def __init__(self, max_frame_bytes=16 * 1024 * 1024):
        """Initialize the receive buffer and maximum frame size."""
        self.buffer = bytearray()
        self.max_frame_bytes = max_frame_bytes

    def feed(self, chunk: bytes):
        """Return complete category-payload pairs from a chunk and retain the remaining
        tail.

        Decode ISH2 payloads as strict base64 and reject invalid encodings or oversized
        frames.
        """
        self.buffer.extend(chunk)
        records = []
        consumed = 0
        while True:
            start = self.buffer.find(SOH, consumed)
            if start < 0:
                consumed = len(self.buffer)
                break
            end = self.buffer.find(EOT, start + 1)
            if end < 0:
                consumed = start
                if len(self.buffer) - start > self.max_frame_bytes:
                    self.buffer.clear()
                    raise ValueError("Shell context frame exceeds size limit")
                break
            if end - start > self.max_frame_bytes:
                self.buffer.clear()
                raise ValueError("Shell context frame exceeds size limit")
            payload = bytes(self.buffer[start + 1 : end])
            consumed = end + 1
            encoded = payload.startswith(VERSION + RS)
            if encoded:
                payload = payload[len(VERSION) + 1 :]
            for record in payload.split(RS):
                if US not in record:
                    continue
                category, body = record.split(US, 1)
                if encoded:
                    body = base64.b64decode(body, validate=True)
                records.append((category.decode("ascii"), body))
        del self.buffer[:consumed]
        return records
