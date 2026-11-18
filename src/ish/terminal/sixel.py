"""Bounded SIXEL pixel encoding, independent of screen and host ownership.

The presenter supplies a composed raster. SIXEL has a binary transparency
mask, not an alpha channel: partial alpha must never silently become opaque.
Pillow owns palette reduction; this module owns only the documented wire form.
"""

from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass

from PIL import Image

from .graphics import GraphicsCapabilityError, GraphicsLimitError


@dataclass(frozen=True)
class Raster:
    """One decoded image and the final sixel row's pixel origin."""

    image: Image.Image
    cursor_y: int
    transparent: bool


class Decoder:
    """Decode one bounded SIXEL record without owning terminal screen state.

    Palette registers are private to each image, matching xterm's default
    private-color-register mode. Pixels use a separate mask: color 255 is a
    valid color and can never double as a transparent-pixel sentinel.
    """

    MAX_BYTES = 16 * 1024 * 1024
    MAX_PIXELS = 4 * 1024 * 1024
    MAX_DIMENSION = 16384
    MAX_COMMANDS = 1024 * 1024
    MAX_PARAMETER = 65535
    MAX_PARAMETER_DIGITS = len(str(MAX_PARAMETER))
    HEADER = re.compile(rb"([0-9;]*)q")
    PARAMETERS = re.compile(rb"[0-9;]*")
    # VT340 default registers, in the protocol's percentage color space.
    COLORS = (
        (0, 0, 0),
        (20, 20, 80),
        (80, 13, 13),
        (20, 80, 20),
        (80, 20, 80),
        (20, 80, 80),
        (80, 80, 20),
        (53, 53, 53),
        (26, 26, 26),
        (33, 33, 60),
        (60, 26, 26),
        (33, 60, 33),
        (60, 33, 60),
        (33, 60, 60),
        (60, 60, 33),
        (80, 80, 80),
    )
    ASPECTS = (2, 2, 5, 3, 3, 2, 2, 1, 1, 1)

    @classmethod
    def matches(cls, body):
        """Claim only the SIXEL DCS final, never another numeric DCS command."""
        return cls.HEADER.match(body) is not None

    @classmethod
    def _numbers(cls, raw, maximum):
        if len(raw) > maximum * (cls.MAX_PARAMETER_DIGITS + 1) - 1:
            raise ValueError("Too many or oversized SIXEL parameters")
        fields = raw.split(b";")
        if len(fields) > maximum or any(
            len(field) > cls.MAX_PARAMETER_DIGITS for field in fields
        ):
            raise ValueError("Too many or oversized SIXEL parameters")
        values = [int(field or b"0") for field in fields]
        if any(value > cls.MAX_PARAMETER for value in values):
            raise ValueError("SIXEL parameter exceeds the limit")
        return values

    @classmethod
    def _palette(cls):
        colors = [tuple((v * 255 + 50) // 100 for v in rgb) for rgb in cls.COLORS]
        levels = (0, 95, 135, 175, 215, 255)
        colors.extend((r, g, b) for r in levels for g in levels for b in levels)
        colors.extend((8 + 10 * i,) * 3 for i in range(24))
        return colors

    def _scan(self, body, aspect, paint=None):
        """Two bounded passes avoid an unbounded intermediate command list."""
        palette = self._palette()
        x = y = width = height = position = commands = 0
        selected = 15
        while position < len(body):
            commands += 1
            if commands > self.MAX_COMMANDS:
                raise ValueError("SIXEL exceeds the command budget")
            char = body[position]
            position += 1
            repeat = 1
            if char in (33, 34, 35):
                match = self.PARAMETERS.match(body, position)
                numbers = self._numbers(match[0], 5 if char == 35 else 4)
                position = match.end()
                if char == 33:
                    if len(numbers) != 1 or position >= len(body):
                        raise ValueError("Invalid SIXEL repeat")
                    repeat = max(1, numbers[0])
                    char = body[position]
                    position += 1
                    if not 63 <= char <= 126:
                        raise ValueError("SIXEL repeat needs pixel data")
                elif char == 34:
                    values = numbers + [0] * (4 - len(numbers))
                    if values[1]:
                        aspect = max(1, math.ceil(values[0] / values[1]))
                    width = max(width, values[2])
                    height = max(height, values[3])
                    x = 0
                else:
                    selected = numbers[0]
                    if selected >= 256:
                        raise ValueError("SIXEL palette exceeds 256 colors")
                    if len(numbers) != 1:
                        if len(numbers) != 5:
                            raise ValueError("Invalid SIXEL color definition")
                        model, first, second, third = numbers[1:]
                        if model == 2 and max(first, second, third) <= 100:
                            palette[selected] = tuple(
                                (v * 255 + 50) // 100 for v in (first, second, third)
                            )
                        elif model == 1 and first <= 360 and max(second, third) <= 100:
                            rgb = colorsys.hls_to_rgb(
                                ((first + 240) % 360) / 360, second / 100, third / 100
                            )
                            palette[selected] = tuple(
                                round(value * 255) for value in rgb
                            )
                        else:
                            raise ValueError("Invalid SIXEL color space or range")
            if 63 <= char <= 126:
                mask = char - 63
                width = max(width, x + repeat)
                height = max(height, y + mask.bit_length() * aspect)
                if paint is not None:
                    paint(x, y, repeat, mask, aspect, selected)
                x += repeat
            elif char == 36:
                x = 0
            elif char == 45:
                x = 0
                y += 6 * aspect
            elif char not in (33, 34, 35, 9, 10, 13, 32):
                raise ValueError("Unsupported SIXEL data command")
            if (
                max(width, height, y, aspect) > self.MAX_DIMENSION
                or max(1, width) * max(1, height, y) > self.MAX_PIXELS
            ):
                raise ValueError("SIXEL exceeds the pixel budget")
        return max(1, width), max(1, height), y, palette

    def decode(self, body: bytes) -> Raster:
        """Decode a complete DCS body; malformed input never partially commits."""
        if len(body) > self.MAX_BYTES:
            raise ValueError("SIXEL exceeds the source byte budget")
        header = self.HEADER.match(body)
        if header is None:
            raise ValueError("Invalid SIXEL DCS header")
        parameters = self._numbers(header[1], 3)
        macro, background, _ = (parameters + [0, 0, 0])[:3]
        if background not in (0, 1, 2):
            raise ValueError("Invalid SIXEL background option")
        aspect = self.ASPECTS[macro] if macro < len(self.ASPECTS) else 1
        data = body[header.end() :]
        width, height, cursor_y, palette = self._scan(data, aspect)
        indexed = Image.new("P", (width, height))
        mask_image = Image.new("L", (width, height), 0 if background == 1 else 255)

        def paint(x, y, repeat, mask, ratio, selected):
            for bit in range(6):
                if mask & (1 << bit):
                    box = (
                        x,
                        y + bit * ratio,
                        min(x + repeat, width),
                        min(y + (bit + 1) * ratio, height),
                    )
                    indexed.paste(selected, box)
                    mask_image.paste(255, box)

        self._scan(data, aspect, paint)
        indexed.putpalette([channel for rgb in palette for channel in rgb])
        rgba = indexed.convert("RGBA")
        rgba.putalpha(mask_image)
        return Raster(rgba, cursor_y, background == 1)


class Encoder:
    """Encode a bounded RGBA raster using transparent sixel color planes."""

    MAX_COLORS = 256
    MAX_PIXELS = 4 * 1024 * 1024
    REPEAT_THRESHOLD = 4

    def __init__(self, limit: int):
        """Bind one positive wire budget without allocating a screen buffer."""
        if limit <= 0:
            raise GraphicsLimitError("SIXEL wire budget must be positive")
        self.limit = limit

    @classmethod
    def _runs(cls, values):
        """Encode one color plane without expanding repetition on the wire."""
        pieces = []
        position = 0
        while position < len(values):
            end = position + 1
            while end < len(values) and values[end] == values[position]:
                end += 1
            count = end - position
            char = chr(values[position] + 63)
            pieces.append(
                f"!{count}{char}" if count >= cls.REPEAT_THRESHOLD else char * count
            )
            position = end
        return "".join(pieces)

    def encode(self, image: Image.Image) -> str:
        """Return one complete DCS; no terminal modes or cursor are modified."""
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > self.MAX_PIXELS:
            raise GraphicsLimitError("SIXEL raster exceeds the pixel budget")
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        histogram = alpha.histogram()
        if any(histogram[1:255]):
            raise GraphicsCapabilityError("SIXEL output cannot preserve partial alpha")
        indexed = rgba.convert("RGB").quantize(
            colors=self.MAX_COLORS,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        )
        indices, opacity = indexed.tobytes(), alpha.tobytes()
        palette = indexed.getpalette()
        parts = []
        length = 0

        def append(value):
            nonlocal length
            length += len(value)
            if length > self.limit:
                raise GraphicsLimitError("Encoded SIXEL exceeds the wire budget")
            parts.append(value)

        # Square pixels and an explicit transparent background. The caller owns
        # display mode 80 and therefore placement/clipping on the outer host.
        append(f'\x1bP0;1q"1;1;{width};{height}')
        used = {
            index for index, visible in zip(indices, opacity, strict=True) if visible
        }
        for index in sorted(used):
            rgb = palette[index * 3 : index * 3 + 3]
            percent = [(value * 100 + 127) // 255 for value in rgb]
            append(f"#{index};2;{percent[0]};{percent[1]};{percent[2]}")
        for top in range(0, height, 6):
            planes = {}
            for row in range(top, min(top + 6, height)):
                offset = row * width
                bit = 1 << (row - top)
                for col in range(width):
                    position = offset + col
                    if opacity[position]:
                        index = indices[position]
                        plane = planes.get(index)
                        if plane is None:
                            plane = planes[index] = bytearray(width)
                        plane[col] |= bit
            for index, plane in sorted(planes.items()):
                # Trailing zero columns carry no pixels and need not be sent.
                while plane and not plane[-1]:
                    plane.pop()
                append(f"#{index}" + self._runs(plane) + "$")
            if top + 6 < height:
                append("-")
        append("\x1b\\")
        return "".join(parts)


def encode(image: Image.Image, limit: int) -> str:
    """Encode one already-composed image within the presenter's wire budget."""
    return Encoder(limit).encode(image)
