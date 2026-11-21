"""Glyphs chosen explicitly for the management windows."""

from dataclasses import dataclass

from ish.lang import Message
from ish.runtime.settings import SettingsError


@dataclass(frozen=True, slots=True)
class Arrows:
    """Directional glyphs for one visual role in management windows."""

    left: str
    down: str
    up: str
    right: str


@dataclass(frozen=True, slots=True)
class IconSet:
    """Choose management glyphs without requiring a private-use font by default."""

    label: Message
    book: str
    gear: str
    title_gap: str
    find: str
    filter: str
    refresh: str
    open_file: str
    separator: str
    edit: str
    number: str
    large: Arrows
    small: Arrows
    current_role: str


DEFAULT_ICONS = "unicode"
_NERD_LARGE = Arrows(left="\ueb6f", down="\ueb6e", up="\ueb71", right="\ueb70")
_NERD_SMALL = Arrows(left="\uf0d9", down="\uf0d7", up="\uf0d8", right="\uf0da")
ICON_SETS = {
    DEFAULT_ICONS: IconSet(
        label=Message.MANAGE_ICONS_UNICODE,
        book="📖",
        gear="⚙",
        title_gap="  ",
        find="⌕",
        filter="⚲",
        refresh="↻",
        open_file="✎",
        separator="│",
        edit="✎",
        number="↕",
        large=Arrows(left="◂", down="▽", up="△", right="▸"),
        small=Arrows(left="◂", down="▾", up="▴", right="▸"),
        current_role="›",
    ),
    "nerdfont": IconSet(
        label=Message.MANAGE_ICONS_NERD,
        book="\U000f02d7",
        gear="\uf013",
        title_gap=" ",
        find="\uf002",
        filter="\uf0b0",
        refresh="\uf021",
        open_file="\uf07c",
        separator="\ue0b0",
        edit="\uf044",
        number="\uf0dc",
        large=_NERD_LARGE,
        small=_NERD_SMALL,
        current_role=_NERD_SMALL.right,
    ),
}


def validate_icons(mode):
    """Accept only the two named glyph sets available to help and settings."""
    if not isinstance(mode, str) or mode not in ICON_SETS:
        raise SettingsError(
            Message.VT_SETTING_CHOICE, name="ui_icons", choices=", ".join(ICON_SETS)
        )
    return mode
