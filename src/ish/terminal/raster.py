"""Encode already-composited pixels as iTerm PNG with Pillow."""

from __future__ import annotations

import base64
import io

from PIL import Image

from .graphics import GraphicsLimitError


def iterm(image: Image.Image, limit: int) -> str:
    """Encode PNG inline, with the verified WezTerm no-cursor-movement extension."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    if len(encoded) > limit:
        raise GraphicsLimitError("Encoded iTerm image exceeds the wire budget")
    return (
        "\x1b]1337;File=inline=1;doNotMoveCursor=1;preserveAspectRatio=0;"
        f"width={image.width}px;height={image.height}px:{encoded}\x1b\\"
    )
