"""Named sRGB shades and their roles in ish help and settings windows.

The Tailwind CSS color palette is distributed under the following license.

MIT License

Copyright (c) Tailwind Labs, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import re
from functools import lru_cache

from prompt_toolkit.output.vt100 import ANSI_COLORS_TO_RGB
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.styles.base import DEFAULT_ATTRS

from ish.lang import Message
from ish.runtime.settings import SettingsError

# Tailwind CSS v3.4.17, src/public/colors.js. Store sRGB values directly so
# management colors don't depend on the user's terminal-emulation palette.
SHADES = tuple(range(100, 1000, 100))
FAMILIES = {
    "slate": "f1f5f9 e2e8f0 cbd5e1 94a3b8 64748b 475569 334155 1e293b 0f172a",
    "gray": "f3f4f6 e5e7eb d1d5db 9ca3af 6b7280 4b5563 374151 1f2937 111827",
    "zinc": "f4f4f5 e4e4e7 d4d4d8 a1a1aa 71717a 52525b 3f3f46 27272a 18181b",
    "neutral": "f5f5f5 e5e5e5 d4d4d4 a3a3a3 737373 525252 404040 262626 171717",
    "stone": "f5f5f4 e7e5e4 d6d3d1 a8a29e 78716c 57534e 44403c 292524 1c1917",
    "red": "fee2e2 fecaca fca5a5 f87171 ef4444 dc2626 b91c1c 991b1b 7f1d1d",
    "orange": "ffedd5 fed7aa fdba74 fb923c f97316 ea580c c2410c 9a3412 7c2d12",
    "amber": "fef3c7 fde68a fcd34d fbbf24 f59e0b d97706 b45309 92400e 78350f",
    "yellow": "fef9c3 fef08a fde047 facc15 eab308 ca8a04 a16207 854d0e 713f12",
    "lime": "ecfccb d9f99d bef264 a3e635 84cc16 65a30d 4d7c0f 3f6212 365314",
    "green": "dcfce7 bbf7d0 86efac 4ade80 22c55e 16a34a 15803d 166534 14532d",
    "emerald": "d1fae5 a7f3d0 6ee7b7 34d399 10b981 059669 047857 065f46 064e3b",
    "teal": "ccfbf1 99f6e4 5eead4 2dd4bf 14b8a6 0d9488 0f766e 115e59 134e4a",
    "cyan": "cffafe a5f3fc 67e8f9 22d3ee 06b6d4 0891b2 0e7490 155e75 164e63",
    "sky": "e0f2fe bae6fd 7dd3fc 38bdf8 0ea5e9 0284c7 0369a1 075985 0c4a6e",
    "blue": "dbeafe bfdbfe 93c5fd 60a5fa 3b82f6 2563eb 1d4ed8 1e40af 1e3a8a",
    "indigo": "e0e7ff c7d2fe a5b4fc 818cf8 6366f1 4f46e5 4338ca 3730a3 312e81",
    "violet": "ede9fe ddd6fe c4b5fd a78bfa 8b5cf6 7c3aed 6d28d9 5b21b6 4c1d95",
    "purple": "f3e8ff e9d5ff d8b4fe c084fc a855f7 9333ea 7e22ce 6b21a8 581c87",
    "fuchsia": "fae8ff f5d0fe f0abfc e879f9 d946ef c026d3 a21caf 86198f 701a75",
    "pink": "fce7f3 fbcfe8 f9a8d4 f472b6 ec4899 db2777 be185d 9d174d 831843",
    "rose": "ffe4e6 fecdd3 fda4af fb7185 f43f5e e11d48 be123c 9f1239 881337",
}
FAMILY_LABELS = {
    family: Message[f"MANAGE_COLOR_{family.upper()}"] for family in FAMILIES
}
HOST_COLOR = "default"
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
MIN_TEXT_CONTRAST = 4.5
COLORS = {
    f"{family}-{shade}": f"#{rgb}"
    for family, row in FAMILIES.items()
    for shade, rgb in zip(SHADES, row.split(), strict=True)
}
COLORS[HOST_COLOR] = HOST_COLOR
DEFAULTS = {
    "background": HOST_COLOR,
    "text": HOST_COLOR,
    "accent": "lime-300",
    "muted": "slate-400",
    "edge": "slate-600",
    "selection": "slate-800",
    "error": "rose-400",
}


class PanelStyle(DynamicStyle):
    """Desaturate the finished foreground and background of the inactive pane."""

    INACTIVE = "class:panel.inactive"

    @staticmethod
    def _gray(color):
        if color in ("", "default", "ansidefault", None):
            return color
        rgb = ANSI_COLORS_TO_RGB.get(color)
        if rgb is None:
            rgb = bytes.fromhex(color)
        red, green, blue = rgb
        value = round(0.2126 * red + 0.7152 * green + 0.0722 * blue)
        return f"{value:02x}" * 3

    def get_attrs_for_style_str(self, style_str, default=DEFAULT_ATTRS):
        """Retain host defaults, font attributes and the luminance of each swatch."""
        attrs = super().get_attrs_for_style_str(style_str, default)
        if self.INACTIVE in style_str.split():
            return attrs._replace(
                color=self._gray(attrs.color), bgcolor=self._gray(attrs.bgcolor)
            )
        return attrs


def validate_palette(value):
    """Validate all role overrides and retain only differences from the defaults."""
    if not isinstance(value, dict) or any(
        role not in DEFAULTS
        or not isinstance(token, str)
        or (token not in COLORS and not HEX_COLOR.fullmatch(token))
        for role, token in value.items()
    ):
        raise SettingsError(Message.MANAGE_PALETTE_INVALID)
    return {
        role: token.lower() for role, token in value.items() if token != DEFAULTS[role]
    }


def _luminance(rgb):
    channels = (component / 255 for component in bytes.fromhex(rgb[1:]))
    red, green, blue = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


@lru_cache(maxsize=512)
def contrast_ink(background, preferred=HOST_COLOR):
    """Prefer the named ink at 4.5:1, then choose the more legible black or white."""
    if background == HOST_COLOR:
        return HOST_COLOR
    level = _luminance(background)
    if preferred != HOST_COLOR:
        low, high = sorted((level, _luminance(preferred)))
        if (high + 0.05) / (low + 0.05) >= MIN_TEXT_CONTRAST:
            return preferred
    black_contrast = (level + 0.05) / 0.05
    white_contrast = 1.05 / (level + 0.05)
    return "#000000" if black_contrast >= white_contrast else "#ffffff"


def ui_style(overrides):
    """Build management styles from role tokens. Host colors remain PTK defaults."""
    tokens = tuple(overrides.get(role, token) for role, token in DEFAULTS.items())
    return _style(tokens)


@lru_cache(maxsize=64)
def _style(tokens):
    """Reuse PTK's compiled style and its invalidation hash across unchanged draws."""
    colors = {
        role: COLORS.get(token, token)
        for role, token in zip(DEFAULTS, tokens, strict=True)
    }
    surface = f"fg:{colors['text']} bg:{colors['background']}"
    accent = f"fg:{colors['accent']}"
    muted = f"fg:{colors['muted']}"
    edge = f"fg:{colors['edge']}"
    bar = f"bg:{colors['selection']}"
    selected = f"{accent} {bar} bold noreverse"
    chip = colors["muted"]
    segment = f"fg:{colors['selection']} bg:{chip}"
    return Style.from_dict(
        {
            "": surface,
            "surface": surface,
            "title": f"{accent} bold",
            "header": f"fg:{contrast_ink(colors['selection'], colors['text'])} {bar} bold",
            "section.title": f"{accent} bold",
            "muted": muted,
            "edge": edge,
            "frame.border": edge,
            "dialog.body": surface,
            "dialog frame.label": f"{accent} bold",
            "dialog.body text-area": f"{accent} {bar}",
            "dialog.body text-area last-line": "nounderline",
            "dialog.body button": f"{muted} {bar} noreverse",
            "dialog.body button.focused": selected,
            "accent": accent,
            "status": muted,
            "status.busy": accent,
            "status.dirty": f"{accent} bold",
            "error": f"fg:{colors['error']} bold",
            "nav.selected": selected,
            "nav.current": f"{muted} bold",
            "field": "",
            "field.focused": f"{accent} bold",
            "setting.title": "bold",
            "setting.title.focused": f"{accent} bold",
            "setting.description": muted,
            "setting.edge": edge,
            "setting.edge.focused": accent,
            "setting.value": f"fg:{contrast_ink(colors['selection'], colors['text'])} {bar}",
            "shortcut.bar": f"{muted} {bar}",
            "shortcut.leading": segment,
            "shortcut.edge": f"{muted} {bar}",
            "shortcut.key": f"fg:{contrast_ink(chip, colors['accent'])} bg:{chip} bold",
            "shortcut.label": f"fg:{contrast_ink(chip, colors['selection'])} bg:{chip} bold",
            "toolbar": surface,
            "button": f"fg:{contrast_ink(colors['selection'], colors['text'])} {bar} bold noreverse nounderline",
            "auxiliary": surface,
            "auxiliary.focused": f"{accent} bold noreverse",
            "button.disabled": f"{muted} {bar} noreverse",
            "button.focused": selected,
            "button.focused button.text": selected,
            "button.disabled button.text": f"{muted} {bar} nobold",
            "radio-selected": f"{accent} bold noreverse",
            "radio-checked": f"{accent} bold",
            "checkbox-selected": f"{accent} bold noreverse",
            "checkbox-checked": f"{accent} bold",
            "scrollbar.arrow": muted,
            "scrollbar.button": f"bg:{colors['accent']}"
            if colors["accent"] not in (HOST_COLOR, colors["background"])
            else "reverse",
            "scrollbar.highlight": f"bg:{colors['muted']}"
            if colors["muted"] not in (HOST_COLOR, colors["background"])
            else "reverse",
        }
    )
