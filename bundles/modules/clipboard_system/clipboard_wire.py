"""Stdlib-only transport declarations reused from Hub's clipboard worker."""

MAX_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 3
READ_CHUNK = 64 * 1024


class ClipboardText:
    """Retain the Hub worker's text encoding contract."""

    UTF8 = "utf-8"


class X11Wire:
    """Frame private worker input and acknowledge real selection ownership."""

    WORKER = "_x11_selection.py"
    READ = "read"
    COPY = "copy"
    HEADER_BYTES = 8
    BYTE_ORDER = "big"
    OWNED = b"owned\n"
    SUCCESS = 0
    TRANSFER_ERROR = 1
    LIBRARY_ERROR = 2
    CONNECTION_ERROR = 3
    TIMEOUT_ERROR = 4

    @classmethod
    def header(cls, length):
        """Encode the exact following text payload length."""
        return length.to_bytes(cls.HEADER_BYTES, cls.BYTE_ORDER)

    @classmethod
    def length(cls, header):
        """Decode a complete private transport header."""
        return int.from_bytes(header, cls.BYTE_ORDER)
