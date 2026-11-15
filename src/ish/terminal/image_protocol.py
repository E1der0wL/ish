"""Normalize bounded inline image inputs into the existing native image store.

Pillow owns decoding. libghostty owns image pixels, placements,
scrollback and cell erasure; this adapter owns only incomplete wire transfers.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import re
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vt import Terminal

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_ENCODED_BYTES = (MAX_SOURCE_BYTES + 2) // 3 * 4
MAX_PIXEL_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 4096
MAX_PART_BYTES = 1024 * 1024
MAX_ERRORS = 16
RASTER_FORMATS = frozenset(("PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF", "ICO", "PPM"))
KITTY_CHUNK_BYTES = 3072
PLACEHOLDER = "\U0010eeee"
DIMENSION = re.compile(rb"([0-9]{1,6})(px|%)?\Z")
# Numeric protocol table from Kitty's rowcolumn-diacritics.txt (Unicode 6 data).
# https://sw.kovidgoyal.net/kitty/graphics-protocol/#unicode-placeholders
DIACRITICS = tuple(
    chr(int(value, 16))
    for value in (
        "0305 030D 030E 0310 0312 033D 033E 033F 0346 034A 034B 034C 0350 0351 0352 0357 035B "
        "0363 0364 0365 0366 0367 0368 0369 036A 036B 036C 036D 036E 036F 0483 0484 0485 0486 0487 "
        "0592 0593 0594 0595 0597 0598 0599 059C 059D 059E 059F 05A0 05A1 05A8 05A9 05AB 05AC 05AF 05C4 "
        "0610 0611 0612 0613 0614 0615 0616 0617 0657 0658 0659 065A 065B 065D 065E 06D6 06D7 06D8 "
        "06D9 06DA 06DB 06DC 06DF 06E0 06E1 06E2 06E4 06E7 06E8 06EB 06EC 0730 0732 0733 0735 0736 "
        "073A 073D 073F 0740 0741 0743 0745 0747 0749 074A 07EB 07EC 07ED 07EE 07EF 07F0 07F1 07F3 "
        "0816 0817 0818 0819 081B 081C 081D 081E 081F 0820 0821 0822 0823 0825 0826 0827 0829 082A "
        "082B 082C 082D 0951 0953 0954 0F82 0F83 0F86 0F87 135D 135E 135F 17DD 193A 1A17 1A75 1A76 "
        "1A77 1A78 1A79 1A7A 1A7B 1A7C 1B6B 1B6D 1B6E 1B6F 1B70 1B71 1B72 1B73 1CD0 1CD1 1CD2 "
        "1CDA 1CDB 1CE0 1DC0 1DC1 1DC3 1DC4 1DC5 1DC6 1DC7 1DC8 1DC9 1DCB 1DCC 1DD1 1DD2 1DD3 "
        "1DD4 1DD5 1DD6 1DD7 1DD8 1DD9 1DDA 1DDB 1DDC 1DDD 1DDE 1DDF 1DE0 1DE1 1DE2 1DE3 1DE4 "
        "1DE5 1DE6 1DFE 20D0 20D1 20D4 20D5 20D6 20D7 20DB 20DC 20E1 20E7 20E9 20F0 2CEF 2CF0 "
        "2CF1 2DE0 2DE1 2DE2 2DE3 2DE4 2DE5 2DE6 2DE7 2DE8 2DE9 2DEA 2DEB 2DEC 2DED 2DEE 2DEF "
        "2DF0 2DF1 2DF2 2DF3 2DF4 2DF5 2DF6 2DF7 2DF8 2DF9 2DFA 2DFB 2DFC 2DFD 2DFE 2DFF A66F "
        "A67C A67D A6F0 A6F1 A8E0 A8E1 A8E2 A8E3 A8E4 A8E5 A8E6 A8E7 A8E8 A8E9 A8EA A8EB A8EC "
        "A8ED A8EE A8EF A8F0 A8F1 AAB0 AAB2 AAB3 AAB7 AAB8 AABE AABF AAC1 FE20 FE21 FE22 FE23 FE24 "
        "FE25 FE26 10A0F 10A38 1D185 1D186 1D187 1D188 1D189 1D1AA 1D1AB 1D1AC 1D1AD 1D242 1D243 1D244"
    ).split()
)


def _color(code, value):
    tag, first, second, third = value
    if tag == 0:
        return str(code + 1)
    if tag == 1:
        return f"{code};5;{first}"
    return f"{code};2;{first};{second};{third}"


def _restore(style):
    flags = int.from_bytes(style[12:14], "little")
    values = ["0"]
    values.extend(
        str(code)
        for bit, code in enumerate((1, 3, 2, 5, 7, 8, 9, 53))
        if flags & (1 << bit)
    )
    values.append(f"4:{(flags >> 8) & 7}")
    values.extend(
        _color(code, style[offset : offset + 4])
        for code, offset in ((38, 0), (48, 4), (58, 8))
    )
    return ("\x1b[" + ";".join(values) + "m").encode()


def _dimension(value, cells, pixels):
    if value in (None, b"auto"):
        return None
    match = DIMENSION.fullmatch(value)
    if not match or int(match[1]) == 0:
        raise ValueError("Invalid inline image dimension")
    number = int(match[1])
    if match[2] == b"px":
        return number
    if match[2] == b"%":
        return max(1, cells * pixels * number // 100)
    return number * pixels


class Images:
    """Supplement iTerm input until native support passes the same contract.

    This adapter owns only incomplete multipart data. Remove it once the pinned
    engine handles iTerm cursor, erase, history and cancellation semantics via
    its public API; the host output presenters are independent of that change.
    """

    def __init__(self, terminal: Terminal):
        """Keep only image transfer state around the single native VT owner."""
        self.terminal = terminal
        self.errors = []
        self.cancel()

    def cancel(self):
        """Cancel an incomplete multipart transaction without changing the VT."""
        self._header = None
        self._parts = bytearray()

    def handle(self, body: bytes) -> None:
        """Consume one iTerm image body already assigned by the input boundary."""
        try:
            self._iterm(body[5:])
        except (ValueError, OSError, binascii.Error) as error:
            self.cancel()
            self.errors.append(str(error))
            del self.errors[:-MAX_ERRORS]

    def _iterm(self, body):
        if body.startswith(b"File="):
            self.cancel()
            header, colon, payload = body[5:].partition(b":")
            if not colon:
                raise ValueError("Missing inline image payload")
            self._display(header, payload)
        elif body.startswith(b"MultipartFile="):
            self.cancel()
            self._header = body[len(b"MultipartFile=") :]
            if len(self._header) > MAX_HEADER_BYTES:
                raise ValueError("Inline image header exceeds limit")
        elif body.startswith(b"FilePart="):
            if self._header is None:
                return
            part = body[len(b"FilePart=") :]
            if (
                len(part) > MAX_PART_BYTES
                or len(self._parts) + len(part) > MAX_ENCODED_BYTES
            ):
                raise ValueError("Inline image transfer exceeds limit")
            self._parts.extend(part)
        elif body == b"FileEnd":
            if self._header is not None:
                header, payload = self._header, bytes(self._parts)
                self.cancel()
                self._display(header, payload)
        else:
            raise ValueError("Invalid inline image terminator")

    def _display(self, header, encoded):
        from PIL import Image

        if not self.terminal.graphics_enabled:
            return
        if len(header) > MAX_HEADER_BYTES or len(encoded) > MAX_ENCODED_BYTES:
            raise ValueError("Inline image transfer exceeds limit")
        options = {}
        for item in header.split(b";"):
            key, separator, value = item.partition(b"=")
            if not separator or key in options:
                raise ValueError("Invalid inline image metadata")
            options[key] = value
        if options.get(b"inline", b"0") != b"1":
            raise ValueError("File download controls are not inline images")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Inline image file exceeds limit")
        context = self.terminal.image_context()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as source:
                    if source.format not in RASTER_FORMATS:
                        raise ValueError(
                            "Inline image format is not a supported raster"
                        )
                    if source.width * source.height * 4 > MAX_PIXEL_BYTES:
                        raise ValueError("Decoded inline image exceeds the pixel limit")
                    if getattr(source, "n_frames", 1) != 1:
                        raise ValueError("Animated iTerm images are not supported")
                    image = source.convert("RGBA")
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
            raise ValueError("Decoded inline image exceeds the pixel limit") from error
        width = _dimension(options.get(b"width"), context.cols, context.cell_width)
        height = _dimension(options.get(b"height"), context.rows, context.cell_height)
        if width is None and height is None:
            width = min(
                image.width, max(1, context.cols - context.x) * context.cell_width
            )
        if width is None:
            width = max(1, image.width * height // image.height)
        if height is None:
            height = max(1, image.height * width // image.width)
        elif options.get(b"preserveAspectRatio", b"1") != b"0":
            scale = min(width / image.width, height / image.height)
            width, height = (
                max(1, int(image.width * scale)),
                max(1, int(image.height * scale)),
            )
        if width * height * 4 > MAX_PIXEL_BYTES:
            raise ValueError("Displayed inline image exceeds the pixel limit")
        if image.size != (width, height):
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        advance = "stay" if options.get(b"doNotMoveCursor") == b"1" else "right"
        self._place(image, advance=advance)

    def _upload(self, image, identity, columns, rows):
        payload = image.tobytes()
        prefix = f"a=T,f=32,s={image.width},v={image.height},i={identity},q=2,C=1"
        prefix += f",U=1,c={columns},r={rows}"
        for offset in range(0, len(payload), KITTY_CHUNK_BYTES):
            chunk = payload[offset : offset + KITTY_CHUNK_BYTES]
            more = int(offset + len(chunk) < len(payload))
            keys = prefix + f",m={more}" if offset == 0 else f"m={more},q=2"
            self.terminal.feed_raw(
                b"\x1b_G" + keys.encode() + b";" + base64.b64encode(chunk) + b"\x1b\\"
            )

    def _place(self, image, *, advance):
        from PIL import Image

        context = self.terminal.image_context()
        if not context.ground or not context.cell_width or not context.cell_height:
            raise ValueError(
                "Inline images need a complete parser boundary and measured cell pixels"
            )
        if context.origin or context.margins:
            raise ValueError(
                "Inline images with origin or horizontal margins are unsupported"
            )
        if advance == "stay" and context.pending_wrap:
            raise ValueError("Stationary inline image at a pending wrap is unsupported")
        columns = math.ceil(image.width / context.cell_width)
        rows = math.ceil(image.height / context.cell_height)
        if rows > len(DIACRITICS):
            raise ValueError("Inline image exceeds the placeholder row limit")
        size = columns * context.cell_width, rows * context.cell_height
        if size[0] * size[1] * 4 > MAX_PIXEL_BYTES:
            raise ValueError("Padded inline image exceeds the pixel limit")
        padded = Image.new("RGBA", size)
        padded.paste(image)
        image = padded
        identity = self.terminal.unused_image_id()
        self._upload(image, identity, columns, rows)
        if self.terminal.image(identity) is None:
            raise ValueError("Native image storage rejected the image")
        write = self.terminal.feed_raw
        start = f"\x1b[{context.x + 1}G".encode()
        red, green, blue = (identity >> 16) & 255, (identity >> 8) & 255, identity & 255
        background = _color(48, context.style[4:8])
        paint = f"\x1b[0;38;2;{red};{green};{blue};58;2;0;0;0;{background}m".encode()
        write(b"\x1b[?7l\x1b[4l" + paint + start)
        visible_columns = min(columns, context.cols - context.x)
        visible_rows = (
            min(rows, context.rows - context.y) if advance == "stay" else rows
        )
        for row in range(visible_rows):
            line = PLACEHOLDER + DIACRITICS[row] + DIACRITICS[0]
            line += PLACEHOLDER * (visible_columns - 1)
            write(line.encode())
            if row < visible_rows - 1 or advance == "below":
                write(start + b"\x1bD")
        if advance == "stay":
            write(f"\x1b[{context.y + 1};{context.x + 1}H".encode())
        write(_restore(context.style))
        if context.wraparound:
            write(b"\x1b[?7h")
        if context.insert:
            write(b"\x1b[4h")
