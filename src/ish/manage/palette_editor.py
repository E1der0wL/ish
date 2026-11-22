"""Edit management color roles with a token picker and live swatches."""

from functools import lru_cache, partial

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Label, TextArea

from ish.lang import Message, i18n

from .palette import (
    COLORS,
    DEFAULTS,
    FAMILIES,
    FAMILY_LABELS,
    HEX_COLOR,
    HOST_COLOR,
    SHADES,
    validate_palette,
)
from .screen import on_click, reset_clicks
from .scroll import HitWindow, hover_style, on_hover


class PaletteEditor:
    """Keep picker candidates separate from edits accepted into the settings draft."""

    TITLE = Message.MANAGE_ROLE_COLORS
    LABELS = {
        "background": Message.MANAGE_PALETTE_BACKGROUND,
        "text": Message.MANAGE_PALETTE_TEXT,
        "accent": Message.MANAGE_PALETTE_ACCENT,
        "accent2": Message.MANAGE_PALETTE_ACCENT2,
        "accent3": Message.MANAGE_PALETTE_ACCENT3,
        "accent4": Message.MANAGE_PALETTE_ACCENT4,
        "warning": Message.MANAGE_PALETTE_WARNING,
        "muted": Message.MANAGE_PALETTE_MUTED,
        "muted2": Message.MANAGE_PALETTE_MUTED2,
        "selection": Message.MANAGE_PALETTE_SELECTION,
        "error": Message.MANAGE_PALETTE_ERROR,
    }
    ROLES = tuple(DEFAULTS)
    CUSTOM = "custom"
    FAMILIES = (*FAMILIES, HOST_COLOR, CUSTOM)
    INITIAL_SHADE = 500
    HEX_PROMPT = " HEX #"
    MIN_SWATCH_WIDTH = 3
    MAX_SWATCH_WIDTH = 5
    COLOR_COLUMN_WIDTH = 3 + max(map(len, COLORS))  # Swatch, space and color token.
    MAX_FAMILY_COLUMNS = 6

    def __init__(self, overrides, *, focus, confirm, width, icons):
        """Build the picker from the currently saved role overrides."""
        self._values = validate_palette(overrides)
        self.role = "accent"
        self.candidate = None
        self.focus = focus
        self.width = width
        self.icons = icons
        self.family_names = {
            name: i18n.get(message) for name, message in FAMILY_LABELS.items()
        }
        self.family_names[HOST_COLOR] = i18n.get(Message.MANAGE_PALETTE_HOST)
        self.family_names[self.CUSTOM] = i18n.get(Message.MANAGE_PALETTE_CUSTOM)
        self.visible_roles = self.ROLES
        self.visible_families = self.FAMILIES
        bindings = KeyBindings()
        self.control = FormattedTextControl(
            self.fragments,
            focusable=True,
            show_cursor=False,
            get_cursor_position=self.cursor_position,
            key_bindings=bindings,
        )
        self.hex_field = TextArea(
            multiline=False,
            prompt=self.HEX_PROMPT,
            width=self.shade_row_width,
            dont_extend_width=True,
            height=1,
            style="class:setting.value",
        )
        self.hex_field.window.char = " "
        self.hex_error = False
        self.hex_field.buffer.on_text_changed += self._hex_changed
        self.container = HSplit(
            [
                HitWindow(self.control, dont_extend_height=True, wrap_lines=False),
                ConditionalContainer(
                    self.hex_field, Condition(lambda: self.candidate == self.CUSTOM)
                ),
                ConditionalContainer(
                    Label(
                        i18n.get(Message.MANAGE_PALETTE_HEX_ERROR), style="class:error"
                    ),
                    Condition(lambda: self.hex_error),
                ),
                HitWindow(
                    FormattedTextControl(self.family_grid),
                    dont_extend_height=True,
                    wrap_lines=False,
                ),
            ],
        )
        self.hex_field.control.key_bindings = bindings
        picking = Condition(lambda: self.picking)
        results = Condition(lambda: bool(self.visible_roles))

        @bindings.add("up", filter=results)
        @bindings.add("down", filter=results)
        def move(event):
            step = 1 if event.key_sequence[-1].key == "down" else -1
            if self.picking:
                self.change_family(step)
            else:
                roles = self.visible_roles
                position = roles.index(self.role) + step
                if not 0 <= position < len(roles):
                    return NotImplemented
                self.role = roles[position]

        @bindings.add("enter", filter=results)
        @bindings.add(" ", filter=results)
        def choose(event):
            if self.picking:
                confirm()
            else:
                self._begin()

        named = Condition(
            lambda: self.candidate in COLORS and self.candidate != HOST_COLOR
        )

        @bindings.add("left", filter=named)
        @bindings.add("right", filter=named)
        def change_shade(event):
            family, shade = self.family_shade()
            step = 1 if event.key_sequence[-1].key == "right" else -1
            position = max(0, min(len(SHADES) - 1, SHADES.index(shade) + step))
            self.candidate = f"{family}-{SHADES[position]}"

        @bindings.add("tab", filter=picking)
        @bindings.add("s-tab", filter=picking)
        def cycle_family(event):
            self.change_family(-1 if event.key_sequence[-1].key == "s-tab" else 1)

        @bindings.add(
            "home",
            filter=picking
            & Condition(
                lambda: (
                    self.candidate != self.CUSTOM
                    and HOST_COLOR in self.visible_families
                )
            ),
        )
        def host_default(event):
            self._activate(HOST_COLOR, self.INITIAL_SHADE)

    @property
    def picking(self):
        """Return whether a color candidate is being edited."""
        return self.candidate is not None

    @property
    def active_input(self):
        """Expose the editable text box while the user is entering a custom color."""
        return self.hex_field if self.candidate == self.CUSTOM else None

    @property
    def token(self):
        """Return the selected role's accepted token or its default."""
        return self._values.get(self.role, DEFAULTS[self.role])

    def apply_filter(self, query):
        """Retain matching roles and families until the next explicit filter change."""
        self.cancel()
        query = query.strip().casefold()
        if not query or query in i18n.get(self.TITLE).casefold():
            self.visible_roles, self.visible_families = self.ROLES, self.FAMILIES
            return

        roles = tuple(
            role
            for role, message in self.LABELS.items()
            if any(
                query in value.casefold()
                for value in (
                    role,
                    i18n.get(message),
                    self._values.get(role, DEFAULTS[role]),
                )
            )
        )
        token_family = query.partition("-")[0] if query in COLORS else None
        families = tuple(
            name
            for name, label in self.family_names.items()
            if query in name or query in label.casefold() or name == token_family
        )
        # A family result still needs an editable target. A role result still
        # needs its full set of colors unless the query also matched a family.
        self.visible_roles = roles or ((self.role,) if families else ())
        self.visible_families = families or (self.FAMILIES if roles else ())
        if roles and self.role not in roles:
            self.role = roles[0]

    def _begin(self):
        family, shade = self.family_shade()
        self._activate(family, shade)

    def _hex_changed(self, buffer):
        self.hex_error = False
        # The prefix is fixed in the prompt, including when pasting a CSS color.
        digits = buffer.text.lstrip("#")
        if digits != buffer.text:
            prefix_length = len(buffer.text) - len(digits)
            buffer.document = Document(
                digits, max(0, buffer.cursor_position - prefix_length)
            )

    def _candidate_token(self):
        """Only complete color values may enter the live preview or the draft."""
        if self.candidate != self.CUSTOM:
            return self.candidate
        token = "#" + self.hex_field.text.lower()
        return token if HEX_COLOR.fullmatch(token) else None

    def family_shade(self):
        """Resolve the displayed token to its position in the swatch grid."""
        token = self.candidate or self.token
        if token in (HOST_COLOR, self.CUSTOM) or token.startswith("#"):
            family = HOST_COLOR if token == HOST_COLOR else self.CUSTOM
            shade = self.INITIAL_SHADE
        else:
            family, level = token.split("-")
            shade = int(level)
        return (
            family if family in self.visible_families else self.visible_families[0]
        ), shade

    def _activate(self, family, shade):
        """Focus the HEX field only while a custom color is being edited."""
        if family == self.CUSTOM:
            if self.candidate != self.CUSTOM:
                reset_clicks(self.hex_field)
                self.hex_field.text = self.custom_digits()
                self.hex_field.buffer.cursor_position = len(self.hex_field.text)
            self.candidate = self.CUSTOM
            get_app().layout.focus(self.hex_field)
        else:
            self.candidate = HOST_COLOR if family == HOST_COLOR else f"{family}-{shade}"
            if get_app().layout.has_focus(self.hex_field):
                get_app().layout.focus(self.control)
        self.hex_error = False

    def change_family(self, step):
        """Cycle the candidate's color family while retaining its shade."""
        family, shade = self._neighbor(step)
        if self.candidate == self.CUSTOM and family != self.CUSTOM:
            self.focus()
        self._activate(family, shade)

    def _neighbor(self, step):
        family, shade = self.family_shade()
        families = self.visible_families
        family = families[(families.index(family) + step) % len(families)]
        return family, shade

    def cancel(self):
        """Discard only the unconfirmed color, keeping earlier role edits."""
        if self.candidate == self.CUSTOM and get_app().layout.has_focus(self.hex_field):
            get_app().layout.focus(self.control)
        self.candidate = None
        self.hex_error = False

    def accept(self):
        """Apply a valid candidate to the draft or leave its input open for correction."""
        token = self._candidate_token()
        if token is None:
            self.hex_error = True
            raise ValueError(i18n.get(Message.MANAGE_PALETTE_HEX_ERROR))
        self._values = validate_palette({**self._values, self.role: token})
        self.cancel()

    def _select_role(self, role):
        self.focus()
        self.role = role

    def _set_token(self, token):
        self.focus()
        self.candidate = token
        self.accept()

    def _select_family(self, family, shade):
        self.focus()
        self._activate(family, shade)
        if family != self.CUSTOM:
            self.accept()

    def values(self):
        """Return a copy of the overrides accepted into the draft."""
        return dict(self._values)

    def preview(self):
        """Include the current candidate in the palette used for live preview."""
        token = self._candidate_token()
        return (
            {**self._values, self.role: token} if token is not None else self.values()
        )

    def cursor_position(self):
        """Keep the edited role or shade in view in a short terminal."""
        if not self.visible_roles:
            return Point(x=0, y=0)
        if self.picking and self.candidate != self.CUSTOM:
            return Point(x=0, y=len(self.visible_roles) + 2)
        return Point(x=0, y=self.visible_roles.index(self.role))

    def _roles(self):
        icons = self.icons()
        marker = icons.edit if self.picking else icons.current_role
        rows = []
        color_column = self.shade_row_width() - self.COLOR_COLUMN_WIDTH
        for role in self.visible_roles:
            current = self.role == role
            token = (
                self._candidate_token() or self.token
                if current and self.picking
                else self._values.get(role, DEFAULTS[role])
            )
            style = "class:field.focused" if current else ""
            title = i18n.get(self.LABELS[role])
            heading = (marker if current else " ") + " " + title
            padding = " " * max(1, color_column - get_cwidth(heading))
            target = (self.control, role)
            select = on_hover(
                target, on_click(partial(self._select_role, role), target=target)
            )
            token_style = hover_style(
                target, style or "class:muted", "class:field.focused"
            )
            style = hover_style(target, style, "class:field.focused")
            rows.append((style, heading + padding, select))
            if token == HOST_COLOR:
                rows.append(
                    (hover_style(target, "fg:default bg:default"), "◇  ", select)
                )
            else:
                rows.append(
                    (
                        hover_style(target, f"bg:{COLORS.get(token, token)}"),
                        "  ",
                        select,
                    )
                )
                rows.append(("", " ", select))
            rows.append((token_style, token, select))
            rows.append(("", "\n"))
        return rows

    def custom_digits(self):
        """Read only the editable part of the selected role's saved HEX token."""
        return self.token[1:] if self.token.startswith("#") else ""

    @staticmethod
    @lru_cache(maxsize=256)
    def _swatch_ink(rgb):
        """Choose ink for the shade number. The swatch itself keeps its exact RGB."""
        channels = (component / 255 for component in bytes.fromhex(rgb[1:]))
        red, green, blue = [
            level / 12.92 if level <= 0.04045 else ((level + 0.055) / 1.055) ** 2.4
            for level in channels
        ]
        luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        black_contrast = (luminance + 0.05) / 0.05
        white_contrast = 1.05 / (luminance + 0.05)
        return "#000000" if black_contrast >= white_contrast else "#ffffff"

    def swatch_layout(self):
        """Return the width per shade and the number that fit on one row."""
        width = max(1, self.width())
        swatch = min(
            self.MAX_SWATCH_WIDTH, max(self.MIN_SWATCH_WIDTH, width // len(SHADES))
        )
        return swatch, min(len(SHADES), max(1, width // swatch))

    def shade_row_width(self):
        """Use the painted span of a shade row for both forms of the HEX field."""
        swatch, count = self.swatch_layout()
        return swatch * count

    def fragments(self):
        """Render each role and the swatches that update its draft token."""
        if not self.visible_roles:
            return [("class:muted", i18n.get(Message.MANAGE_FILTER_EMPTY))]
        rows = self._roles()
        family, shade = self.family_shade()

        def arrow(step, glyph):
            target = (self.control, step)
            return (
                hover_style(target, "class:accent bold", "class:nav.selected"),
                glyph,
                on_hover(
                    target,
                    on_click(
                        partial(self._select_family, *self._neighbor(step)),
                        target=target,
                    ),
                ),
            )

        arrows = self.icons().small
        rows.extend(
            [
                ("class:muted", "\n" + i18n.get(Message.MANAGE_PALETTE_FAMILY) + "   "),
                arrow(-1, arrows.left + " "),
                ("class:accent bold", self.family_names[family]),
                arrow(1, " " + arrows.right),
            ]
        )
        if family == self.CUSTOM:
            if not self.picking:
                rows.append(
                    (
                        hover_style(
                            (self.control, "hex"),
                            "class:setting.value",
                            "class:nav.selected",
                        ),
                        "\n"
                        + (self.HEX_PROMPT + self.custom_digits()).ljust(
                            self.shade_row_width()
                        ),
                        on_hover(
                            (self.control, "hex"),
                            on_click(
                                partial(self._select_family, family, shade),
                                target=(self.control, "hex"),
                            ),
                        ),
                    )
                )
            return rows
        if family == HOST_COLOR:
            rows.append(("", "\n"))
            return rows
        rows.append(("class:muted", "   " + COLORS[f"{family}-{shade}"].upper()))
        rows.append(("", "\n"))
        token = self._candidate_token() or self.token
        swatch_width, per_line = self.swatch_layout()
        for number, level in enumerate(SHADES):
            if number and number % per_line == 0:
                rows.append(("", "\n"))
            sample = f"{family}-{level}"
            rgb = COLORS[sample]
            style = f"fg:{self._swatch_ink(rgb)} bg:{rgb}"
            if sample == token:
                style += " bold underline"
            rows.append(
                (
                    hover_style((self.control, sample), style, style + " bold"),
                    f"{level:^{swatch_width}}",
                    on_hover(
                        (self.control, sample),
                        on_click(
                            partial(self._set_token, sample),
                            target=(self.control, sample),
                        ),
                    ),
                )
            )
        return rows

    def family_grid(self):
        """End the family list with the unmodified host color and direct HEX entry."""
        if not self.visible_roles:
            return []
        family, shade = self.family_shade()
        rows = [("", "\n")]
        width = max(1, self.width())
        column_width = (
            max(get_cwidth(self.family_names[name]) for name in self.visible_families)
            + 4
        )
        columns = max(1, min(self.MAX_FAMILY_COLUMNS, width // column_width))
        for number, name in enumerate(self.visible_families):
            title = self.family_names[name]
            if number and number % columns == 0:
                rows.append(("", "\n"))
            target = (self.control, name)
            select = on_hover(
                target,
                on_click(partial(self._select_family, name, shade), target=target),
            )
            swatch = COLORS.get(f"{name}-500")
            rows.append(
                (
                    hover_style(target, f"fg:{swatch}" if swatch else "class:muted"),
                    "■ " if swatch else "◇ ",
                    select,
                )
            )
            padding = " " * (column_width - 2 - get_cwidth(title))
            style = "class:field.focused" if name == family else ""
            rows.append(
                (hover_style(target, style, "class:field.focused"), title, select)
            )
            rows.append(("", padding))
        return rows
