"""Verify SIXEL output pixels from wire, independently of the production codec."""

import asyncio
import base64
import os
import re
import sys
import tempfile
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from ish.terminal.graphics import (
    GraphicsCapabilityError,
    GraphicsLimitError,
    RasterPresenter,
    Rect,
)
from ish.terminal.session import Session
from ish.terminal.sixel import Decoder, Encoder, encode
from ish.terminal.vt import Placement, Terminal


def read_pixels(wire):
    """Read the encoder's documented subset using independent literal tokens."""
    match = re.search(r'\x1bP0;1q"1;1;(\d+);(\d+)(.*?)\x1b\\', wire, re.S)
    if not match:
        raise AssertionError("Missing transparent SIXEL raster")
    width, height = int(match[1]), int(match[2])
    pixels = Image.new("RGBA", (width, height))
    colors = {}
    x = y = selected = 0
    text = match[3]
    while text:
        color = re.match(r"#(\d+)(?:;2;(\d+);(\d+);(\d+))?", text)
        if color:
            selected = int(color[1])
            if color[2] is not None:
                colors[selected] = tuple(
                    (int(v) * 255 + 50) // 100 for v in color.groups()[1:]
                ) + (255,)
            text = text[color.end() :]
        elif text[0] in "$-":
            if text[0] == "-":
                y += 6
            x = 0
            text = text[1:]
        else:
            data = re.match(r"(?:!(\d+))?([?-~])", text)
            if not data:
                raise AssertionError(f"Unexpected SIXEL token: {text[:20]!r}")
            repeat = int(data[1] or 1)
            mask = ord(data[2]) - 63
            for col in range(x, x + repeat):
                for bit in range(6):
                    if mask & (1 << bit) and y + bit < height:
                        pixels.putpixel((col, y + bit), colors[selected])
            x += repeat
            text = text[data.end() :]
    return pixels


class SixelEncoderTests(unittest.TestCase):
    """Exact binary transparency, independent palette planes, and hard budgets."""

    def test_all_column_masks_at_the_repeat_threshold(self):
        """Every printable SIXEL mask survives short and long adjacent runs."""
        image = Image.new("RGBA", (64 * 8, 6))
        x = 0
        for width in (3, 5):
            for mask in range(64):
                for bit in range(6):
                    if mask & (1 << bit):
                        image.paste((255, 0, 0, 255), (x, bit, x + width, bit + 1))
                x += width
        self.assertEqual(read_pixels(encode(image, 4096)).tobytes(), image.tobytes())

    def test_sparse_pixels_bands_and_repeat_preserve_transparency(self):
        """Sparse pixels bands and repeat preserve transparency."""
        image = Image.new("RGBA", (20, 13))
        for x in range(20):
            image.putpixel((x, 0), (255, 0, 0, 255))
            image.putpixel((x, 12), (0, 0, 255, 255))
        image.putpixel((7, 6), (0, 255, 0, 255))
        wire = encode(image, 4096)
        self.assertIn("!20", wire)
        self.assertEqual(read_pixels(wire).tobytes(), image.tobytes())
        self.assertNotIn("\x1b[", wire)

    def test_all_transparent_and_color_255_do_not_share_an_alpha_sentinel(self):
        """All transparent and color 255 do not share an alpha sentinel."""
        empty = Image.new("RGBA", (2, 2))
        self.assertEqual(read_pixels(encode(empty, 100)).tobytes(), empty.tobytes())
        image = Image.new("RGBA", (256, 1))
        for x in range(256):
            image.putpixel((x, 0), (x, x, x, 255))
        actual = read_pixels(encode(image, 20000))
        self.assertEqual(actual.getpixel((255, 0)), (255, 255, 255, 255))
        self.assertEqual(actual.getchannel("A").getextrema(), (255, 255))

    def test_partial_alpha_is_rejected_without_silent_background_flattening(self):
        """Partial alpha is rejected without silent background flattening."""
        with self.assertRaises(GraphicsCapabilityError):
            encode(Image.new("RGBA", (1, 1), (255, 0, 0, 128)), 1000)

    def test_wire_pixel_and_zero_budgets_are_bounded(self):
        """Wire pixel and zero budgets are bounded."""
        with self.assertRaises(GraphicsLimitError):
            encode(Image.new("RGBA", (1, 1), "red"), 8)
        with self.assertRaises(GraphicsLimitError):
            Encoder(0)
        with patch.object(Encoder, "MAX_PIXELS", 3):
            with self.assertRaises(GraphicsLimitError):
                encode(Image.new("RGBA", (2, 2)), 1000)


class SixelDecoderTests(unittest.TestCase):
    """Literal graphics fixtures establish mask, palette, ratio, and bounds."""

    def test_transparent_palette_255_and_blank_background_are_independent(self):
        """Transparent palette 255 and blank background are independent."""
        result = Decoder().decode(b'0;1q"1;1;2;2#255;2;100;0;0@?')
        self.assertTrue(result.transparent)
        self.assertEqual(result.image.size, (2, 2))
        self.assertEqual(result.image.tobytes(), bytes((255, 0, 0, 255)) + bytes(12))
        opaque = Decoder().decode(b'0;0q"1;1;2;2#1;2;100;0;0@?')
        self.assertEqual(opaque.image.getpixel((1, 1)), (0, 0, 0, 255))

    def test_repeat_carriage_return_bands_hls_and_final_cursor(self):
        """Repeat carriage return bands hls and final cursor."""
        result = Decoder().decode(b'0;1q"1;1;3;7#1;1;120;50;100!3@$#2;2;0;0;100?@-#1@')
        self.assertEqual(result.cursor_y, 6)
        self.assertEqual(result.image.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(result.image.getpixel((1, 0)), (0, 0, 255, 255))
        self.assertEqual(result.image.getpixel((2, 0)), (255, 0, 0, 255))
        self.assertEqual(result.image.getpixel((0, 6)), (255, 0, 0, 255))
        self.assertEqual(result.image.getpixel((1, 6))[3], 0)

    def test_aspect_and_palette_redefinition_are_applied_to_indexed_pixels(self):
        """Aspect and palette redefinition are applied to indexed pixels."""
        image = Decoder().decode(b"2;1q#1;2;100;0;0@#1;2;0;100;0").image
        self.assertEqual(image.size, (1, 5))
        self.assertEqual(image.tobytes(), bytes((0, 255, 0, 255)) * 5)

    def test_foreign_dcs_and_malformed_or_oversized_inputs_are_not_images(self):
        """Foreign dcs and malformed or oversized inputs are not images."""
        self.assertFalse(Decoder.matches(b"$qfoo"))
        for body in (
            b"0;1q!65535~",
            b'0;1q"1;1;65535;65535',
            b"0;1q#256@",
            b"0;1q#1;2;101;0;0@",
            b"0;1q!2#1",
            b'0;1q"1;1;1;1;1',
            b"0;3q@",
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                Decoder().decode(body)
        with patch.object(Decoder, "MAX_COMMANDS", 2):
            with self.assertRaises(ValueError):
                Decoder().decode(b"7;1q@$@")

    def test_excessive_parameter_fields_do_not_expand_into_a_large_object_list(self):
        """Reject a one-megabyte field list before allocating millions of objects."""
        source = b"0;1q#" + b";" * (1024 * 1024)
        tracemalloc.start()
        try:
            with self.assertRaises(ValueError):
                Decoder().decode(source)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 5 * 1024 * 1024)


class SixelPresenterTests(unittest.TestCase):
    """Output is derived from native pixels and popup rectangles, never raw replay."""

    def setUp(self):
        """Create independent fixture data for the observed public contracts."""
        self.asset = SimpleNamespace(
            id=1,
            generation=2,
            width=4,
            height=4,
            rgba=bytes((255, 0, 0, 255)) * 16,
        )
        self.placement = Placement(1, 1, 2, 2, 2, 4, 4, 0, 0, 4, 4, 2, 0)
        self.frame = SimpleNamespace(
            images=(self.placement,),
            cols=6,
            rows=5,
            cell_width=2,
            cell_height=2,
            cells=(),
        )
        self.presenter = RasterPresenter("sixel")

    def blank_rows(self, background=None):
        """Provide explicit undecorated cells underneath partial-alpha pixels."""
        return tuple(
            tuple(
                SimpleNamespace(
                    x=x,
                    width=1,
                    text=" ",
                    underline=0,
                    overline=False,
                    style="",
                    bg=background,
                )
                for x in range(self.frame.cols)
            )
            for _ in range(self.frame.rows)
        )

    def test_page_origin_padding_popup_occlusion_and_restoration(self):
        """Page origin padding popup occlusion and restoration."""
        first = self.presenter.plan(self.frame, lambda _: self.asset)
        pixels = read_pixels(first.after_text)
        self.assertEqual(pixels.size, (6, 6))
        self.assertEqual(pixels.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(pixels.getpixel((2, 2)), (255, 0, 0, 255))
        covered = self.presenter.plan(
            self.frame, lambda _: self.asset, occluders=(Rect(2, 2, 2, 2),)
        )
        self.assertTrue(covered.repaint)
        self.assertEqual(read_pixels(covered.after_text).getpixel((2, 2)), (0, 0, 0, 0))
        restored = self.presenter.plan(self.frame, lambda _: self.asset)
        self.assertEqual(read_pixels(restored.after_text).tobytes(), pixels.tobytes())
        self.assertEqual(self.asset.rgba, bytes((255, 0, 0, 255)) * 16)
        self.assertEqual(self.presenter.close(), "\x1b7\x1b[0m\x1b[2J\x1b8")
        self.assertEqual(self.presenter.close(), "")

    def test_unchanged_scene_reuses_wire_and_erasure_requests_text_repaint(self):
        """Unchanged scene reuses wire and erasure requests text repaint."""
        first = self.presenter.plan(self.frame, lambda _: self.asset)
        with patch(
            "ish.terminal.sixel.encode", side_effect=AssertionError("reencoded")
        ):
            same = self.presenter.plan(self.frame, lambda _: self.fail("asset read"))
        self.assertEqual(first.after_text, same.after_text)
        hidden = self.presenter.plan(
            self.frame, lambda _: self.asset, occluders=(Rect(0, 0, 20, 20),)
        )
        self.assertFalse(hidden.after_text)
        self.assertTrue(hidden.repaint)

    def test_rejection_preserves_previous_cleanup_and_below_text_contract(self):
        """Rejection preserves previous cleanup and below text contract."""
        self.presenter.plan(self.frame, lambda _: self.asset)
        self.frame.images = (replace(self.placement, layer=0),)
        with self.assertRaises(GraphicsCapabilityError):
            self.presenter.plan(self.frame, lambda _: self.asset)
        self.assertTrue(self.presenter.close())

    def test_partial_alpha_flattens_known_blank_background_and_preserves_holes(self):
        """Partial alpha flattens known blank background and preserves holes."""
        self.asset.rgba = bytes((255, 0, 0, 128)) * 15 + bytes((0, 0, 0, 0))
        self.frame.cells = self.blank_rows()
        self.presenter.background = (0, 0, 255)
        first = self.presenter.plan(self.frame, lambda _: self.asset)
        pixel = read_pixels(first.after_text).getpixel((2, 2))
        self.assertLessEqual(abs(pixel[0] - 128), 1)
        self.assertLessEqual(abs(pixel[2] - 127), 1)
        self.assertEqual(pixel[3], 255)
        self.assertEqual(read_pixels(first.after_text).getpixel((5, 5)), (0, 0, 0, 0))
        self.frame.cells = tuple(
            tuple(
                SimpleNamespace(**(vars(cell) | {"text": "X"}))
                if (row, cell.x) == (1, 1)
                else cell
                for cell in cells
            )
            for row, cells in enumerate(self.frame.cells)
        )
        with self.assertRaises(GraphicsCapabilityError):
            self.presenter.plan(self.frame, lambda _: self.asset)

    def test_partial_alpha_prefers_explicit_cell_color_and_rejects_unknown_defaults(
        self,
    ):
        """Partial alpha prefers explicit cell color and rejects unknown defaults."""
        self.asset.rgba = bytes((255, 0, 0, 128)) * 16
        self.frame.cells = self.blank_rows((0, 255, 0))
        self.presenter.background = (0, 0, 255)
        first = self.presenter.plan(self.frame, lambda _: self.asset)
        pixel = read_pixels(first.after_text).getpixel((2, 2))
        self.assertLessEqual(abs(pixel[1] - 127), 1)
        self.assertEqual(pixel[2], 0)
        self.presenter.background = None
        self.frame.cells = tuple(
            tuple(
                SimpleNamespace(**(vars(cell) | {"bg": None}))
                if (row, cell.x) == (1, 1)
                else cell
                for cell in cells
            )
            for row, cells in enumerate(self.frame.cells)
        )
        with self.assertRaises(GraphicsCapabilityError):
            self.presenter.plan(self.frame, lambda _: self.asset)

    def test_clipped_alpha_tracks_wide_heads_crossing_the_visible_edge(self):
        """A grapheme starting left of a clipped image still owns its second column."""
        self.frame.cells = self.blank_rows((0, 0, 255))
        self.asset.rgba = bytes((255, 0, 0, 128)) * 16
        view = {"origin": (4, 2), "clip": Rect(7, 4, 1, 2)}
        first = self.presenter.plan(self.frame, lambda _: self.asset, **view)
        pixel = read_pixels(first.after_text).getpixel((7, 4))
        self.assertLessEqual(abs(pixel[0] - 128), 1)
        self.assertLessEqual(abs(pixel[2] - 127), 1)
        rows = list(self.frame.cells)
        row = rows[1]
        # Column 2 is part of the source image but outside the presentation clip.
        outside = SimpleNamespace(**(vars(row[2]) | {"text": "X"}))
        rows[1] = (*row[:2], outside, *row[3:])
        self.frame.cells = tuple(rows)
        same = self.presenter.plan(
            self.frame, lambda _: self.fail("asset read"), **view
        )
        self.assertEqual(same.after_text, first.after_text)
        self.assertFalse(same.repaint)
        wide = SimpleNamespace(**(vars(row[0]) | {"text": "한", "width": 2}))
        rows[1] = (wide, *rows[1][2:])
        self.frame.cells = tuple(rows)
        with self.assertRaises(GraphicsCapabilityError):
            self.presenter.plan(self.frame, lambda _: self.asset, **view)


class SixelInputTests(unittest.TestCase):
    """Live framing and native lifetime; no screen or cursor shadow exists."""

    WIRE = b'\x1bP0;1q"1;1;4;6#1;2;100;0;0~?~?\x1b\\'

    def setUp(self):
        """Create independent fixture data for the observed public contracts."""
        self.replies = []
        self.terminal = Terminal(8, 4, self.replies.append)
        self.addCleanup(self.terminal.close)
        self.terminal.resize(8, 4, 2, 3)
        self.terminal.set_graphics(True)

    def test_every_byte_fragment_replaces_covered_cells_and_keeps_exact_alpha(self):
        """Image cells replace covered text while the native asset keeps alpha."""
        self.terminal.feed(b"ABCD\r")
        for byte in self.WIRE:
            self.terminal.feed(bytes((byte,)))
        frame = self.terminal.frame()
        self.assertEqual("".join(cell.text for cell in frame.cells[0]).strip(), "CD")
        self.assertEqual((frame.cursor_x, frame.cursor_y), (0, 0))
        image = self.terminal.image(frame.images[0].image_id)
        self.assertEqual(image.rgba, (bytes((255, 0, 0, 255)) + bytes(4)) * 12)

    def test_partial_cells_keep_alpha_padding_and_replace_only_the_image_rectangle(
        self,
    ):
        """Cell padding is transparent but still belongs to the native image cell."""
        self.terminal.feed(b"ABCD\r\nEFGH\x1b[H")
        self.terminal.feed(b'\x1bP0;1q"1;1;3;4#1;2;100;0;0!3N\x1b\\')
        frame = self.terminal.frame()
        self.assertEqual("".join(cell.text for cell in frame.cells[0]), "  CD    ")
        self.assertEqual("".join(cell.text for cell in frame.cells[1]), "  GH    ")
        image = self.terminal.image(frame.images[0].image_id)
        expected = (bytes((255, 0, 0, 255)) * 3 + bytes(4)) * 4 + bytes(4 * 4 * 2)
        self.assertEqual((image.width, image.height, image.rgba), (4, 6, expected))

    def test_cancel_reset_disable_and_foreign_dcs_do_not_leak_or_duplicate(self):
        """Cancel reset disable and foreign dcs do not leak or duplicate."""
        self.terminal.feed(self.WIRE[:-2] + b"\x18OK")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(
            "".join(c.text for c in self.terminal.frame().cells[0]).strip(), "OK"
        )
        self.terminal.feed(self.WIRE + b"\x1b[?80h\x1bc\x1b[?80$p")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(self.replies[-1], b"\x1b[?80;2$y")
        self.terminal.set_graphics(False)
        self.terminal.feed(self.WIRE + b"\x1b[c")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(self.replies[-1], b"\x1b[?62;22c")

    def test_display_modes_queries_and_native_modes_do_not_get_duplicate_responses(
        self,
    ):
        """Display modes queries and native modes do not get duplicate responses."""
        self.terminal.feed(
            b"\x1b[?80;25h\x1b[?80$p\x1b[?25$p\x1b[?1070$p\x1b[?8452$p\x1b[?1;1S"
        )
        self.assertEqual(
            self.replies,
            [
                b"\x1b[?80;1$y",
                b"\x1b[?25;1$y",
                b"\x1b[?1070;3$y",
                b"\x1b[?8452;2$y",
                b"\x1b[?1;0;256S",
            ],
        )
        self.terminal.feed(b"\x1b[3;4H" + self.WIRE)
        frame = self.terminal.frame()
        self.assertEqual((frame.cursor_x, frame.cursor_y), (3, 2))
        self.assertEqual((frame.images[0].x, frame.images[0].y), (0, 0))

    def test_stationary_image_keeps_viewport_rows_inside_scroll_margins(self):
        """Display mode uses absolute rows and cannot scroll a smaller DECSTBM region."""
        self.terminal.feed(
            b"\x1b[1;5HTOP\x1b[2;5HMID\x1b[3;5HLOW\x1b[4;5HEND"
            b"\x1b[1;2r\x1b[4;6H\x1b[?80h"
            b'\x1bP0;1q"1;1;2;9#1;2;100;0;0!2F$'
            b"#2;2;0;100;0!2w-#3;2;0;0;100!2F\x1b\\"
        )
        frame = self.terminal.frame()
        self.assertEqual((frame.cursor_x, frame.cursor_y), (5, 3))
        self.assertEqual(
            ["".join(cell.text for cell in row)[4:7] for row in frame.cells],
            ["TOP", "MID", "LOW", "END"],
        )
        pixels = read_pixels(
            RasterPresenter("sixel").plan(frame, self.terminal.image).after_text
        )
        self.assertEqual(pixels.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(pixels.getpixel((0, 3)), (0, 255, 0, 255))
        self.assertEqual(pixels.getpixel((0, 6)), (0, 0, 255, 255))

    def test_native_history_alternate_and_erase_keep_the_image_owner(self):
        """Native history alternate and erase keep the image owner."""
        self.terminal.feed(self.WIRE)
        identity = self.terminal.frame().images[0].image_id
        self.terminal.feed(b"\x1b[?1049h")
        self.assertIsNone(self.terminal.image(identity))
        self.terminal.feed(b"\x1b[?1049l\r\n" + b"\r\n" * 6)
        self.assertFalse(self.terminal.frame().images)
        self.terminal.scroll_top()
        self.assertTrue(self.terminal.frame().images)
        self.terminal.scroll_bottom()
        self.terminal.feed(self.WIRE + b"\x1b[2J")
        self.assertFalse(self.terminal.frame().images)

    def test_character_erasure_removes_image_cells_without_changing_source_pixels(self):
        """ECH clears its native cells while leaving other rows and RGBA untouched."""
        self.terminal.feed(self.WIRE)
        identity = self.terminal.frame().images[0].image_id
        original = self.terminal.image(identity).rgba
        self.terminal.feed(b"\x1b[H\x1b[2X")
        frame = self.terminal.frame()
        pixels = read_pixels(
            RasterPresenter("sixel").plan(frame, self.terminal.image).after_text
        )
        self.assertEqual(pixels.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(pixels.getpixel((0, 3)), (255, 0, 0, 255))
        self.assertEqual(self.terminal.image(identity).rgba, original)
        self.terminal.feed(b"\x1b[2J")
        self.assertFalse(self.terminal.frame().images)

    def test_bottom_scroll_saved_cursor_style_and_right_mode_are_independent(self):
        """Bottom scroll saved cursor style and right mode are independent."""
        self.terminal.feed(b"\x1b[2;3H\x1b[31;44m\x1b7\x1b[4;3H" + self.WIRE)
        frame = self.terminal.frame()
        self.assertEqual((frame.cursor_x, frame.cursor_y), (2, 2))
        self.assertEqual((frame.images[0].x, frame.images[0].y), (4, 6))
        self.terminal.feed(b"\x1b8Z")
        self.assertEqual(
            (self.terminal.frame().cursor_x, self.terminal.frame().cursor_y), (3, 1)
        )
        restored = self.terminal.frame().cells[1][2]
        self.assertEqual((restored.text, restored.flags), ("Z", 0))
        self.assertEqual((restored.fg, restored.bg), ((204, 102, 102), (129, 162, 190)))
        self.terminal.feed(b"\x1b[H\x1b[?8452h" + self.WIRE)
        self.assertEqual(self.terminal.frame().cursor_x, 2)

    def test_malformed_image_is_counted_and_following_valid_image_still_works(self):
        """Malformed image is counted and following valid image still works."""
        self.terminal.feed(b"\x1bP0;1q!99999~\x1b\\" + self.WIRE + b"OK")
        self.assertEqual(self.terminal.image_failure_count, 1)
        self.assertEqual(len(self.terminal.frame().images), 1)
        self.assertEqual(
            "".join(c.text for c in self.terminal.frame().cells[0]).strip(), "OK"
        )

    def test_later_transparent_image_replaces_previous_image_cells(self):
        """A transparent pixel belongs to the new image cell, not an older layer."""
        self.terminal.feed(self.WIRE.replace(b"~?~?", b"!4~"))
        self.terminal.feed(self.WIRE.replace(b"100;0;0", b"0;0;100"))
        frame = self.terminal.frame()
        pixels = read_pixels(
            RasterPresenter("sixel").plan(frame, self.terminal.image).after_text
        )
        self.assertEqual(pixels.getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual(pixels.getpixel((1, 0)), (0, 0, 0, 0))

    def test_stationary_pending_wrap_rejects_image_without_corrupting_next_text(self):
        """A failed unsupported cursor state must preserve the pending text wrap."""
        self.terminal.feed(b"ABCDEFGH\x1b[?80h" + self.WIRE + b"X")
        frame = self.terminal.frame()
        self.assertEqual("".join(c.text for c in frame.cells[0]), "ABCDEFGH")
        self.assertEqual(frame.cells[1][0].text, "X")
        self.assertFalse(frame.images)
        self.assertEqual(self.terminal.image_failure_count, 1)

    def test_synchronized_output_validates_live_storage_and_publishes_on_release(self):
        """The input validator must not confuse a held frame with live storage."""
        self.terminal.frame()
        self.terminal.feed(b"\x1b[?2026h" + self.WIRE)
        self.assertFalse(self.terminal.frame().images)
        self.assertEqual(self.terminal.image_failure_count, 0)
        self.terminal.feed(b"\x1b[?2026l")
        frame = self.terminal.frame()
        self.assertEqual(len({item.image_id for item in frame.images}), 1)
        self.assertEqual(
            self.terminal.image(frame.images[0].image_id).rgba,
            (bytes((255, 0, 0, 255)) + bytes(4)) * 12,
        )

    def test_native_mode_empty_and_large_parameters_never_close_input(self):
        """The supplement leaves unrelated native parameters and replies unchanged."""
        commands = (
            b"\x1b[?25;h",
            b"\x1b[?;25l",
            b"\x1b[?;h",
            b"\x1b[?00025h",
            b"\x1b[?65536l",
            b"\x1b[?" + b"9" * 4000 + b"h",
            b"\x1b[?" + b"9" * 4301 + b"h",
            b"\x1b[?25$p",
        )
        for command in commands:
            with self.subTest(command=command[:24]):
                expected, actual = [], []
                with (
                    Terminal(8, 4, expected.append) as native,
                    Terminal(8, 4, actual.append) as wrapped,
                ):
                    native.feed_raw(command + b"OK")
                    wrapped.feed(command + b"OK")
                    before, after = native.frame(), wrapped.frame()
                    self.assertEqual(actual, expected)
                    self.assertEqual(after.cells, before.cells)
                    self.assertEqual(after.cursor_visible, before.cursor_visible)

    def test_sixel_mode_groups_preserve_native_defaults_and_oversized_fields(self):
        """Mixed mode ownership never parses an empty or oversized native integer."""
        replies = []
        with Terminal(8, 4, replies.append) as terminal:
            terminal.feed(b"\x1b[?00080;;00025h\x1b[?00080$p\x1b[?25$p")
            self.assertEqual(replies, [b"\x1b[?80;1$y", b"\x1b[?25;1$y"])
            terminal.feed(b"\x1b[?80;" + b"9" * 4000 + b";25l")
            terminal.feed(b"\x1b[?80$p\x1b[?25$pOK")
            self.assertEqual(replies[-2:], [b"\x1b[?80;2$y", b"\x1b[?25;2$y"])
            self.assertEqual(
                "".join(c.text for c in terminal.frame().cells[0]).strip(), "OK"
            )


class SixelOverwriteTests(unittest.TestCase):
    """Native image cells disappear on text writes without mutating source pixels."""

    SOLID = b'\x1bP0;1q"1;1;8;6#1;2;100;0;0!8~\x1b\\'
    RED = (255, 0, 0, 255)
    CLEAR = (0, 0, 0, 0)

    def setUp(self):
        """Give four image columns exact independent two-by-three cell geometry."""
        self.terminal = Terminal(8, 4, lambda _: None)
        self.addCleanup(self.terminal.close)
        self.terminal.resize(8, 4, 2, 3)
        self.terminal.set_graphics(True)

    def pixels(self):
        """Decode final output rather than relying on the clipping implementation."""
        frame = self.terminal.frame()
        scene = Image.new("RGBA", (16, 12))
        wire = RasterPresenter("sixel").plan(frame, self.terminal.image).after_text
        if wire:
            scene.paste(read_pixels(wire))
        return scene

    def expected(self, *holes):
        """Construct independently specified cell rectangles, including untouched rows."""
        scene = Image.new("RGBA", (16, 12))
        scene.paste(self.RED, (0, 0, 8, 6))
        for rectangle in holes:
            scene.paste(self.CLEAR, rectangle)
        return scene.tobytes()

    def test_repeated_character_write_keeps_its_cell_clear_and_source_unchanged(self):
        """A cell replaced by B stays clear when B is written there again."""
        self.terminal.feed(b"ABCD\r" + self.SOLID)
        before = self.terminal.frame()
        identity = before.images[0].image_id
        original = self.terminal.image(identity)
        self.assertEqual(self.pixels().tobytes(), self.expected())
        self.terminal.feed(b"\x1b[1;2HB")
        after = self.terminal.frame()
        self.assertEqual(after.cells[0][1].text, "B")
        self.assertEqual(self.pixels().tobytes(), self.expected((2, 0, 4, 3)))
        self.terminal.feed(b"\x1b[1;2HB")
        self.assertEqual(self.terminal.frame().cells, after.cells)
        self.assertEqual(self.pixels().tobytes(), self.expected((2, 0, 4, 3)))
        self.assertEqual(self.terminal.image(identity).rgba, original.rgba)
        self.assertEqual(
            {item.image_generation for item in self.terminal.frame().images},
            {before.images[0].image_generation},
        )

    def test_cursor_style_and_writes_outside_the_raster_preserve_every_pixel(self):
        """Only actual covered writes change image pixels, not motion or SGR."""
        self.terminal.feed(self.SOLID)
        before = self.terminal.frame().images
        self.terminal.feed(b"\x1b[1;2H\x1b[31m\x1b[4;7HX\x1b[0m")
        self.assertEqual(self.terminal.frame().images, before)
        self.assertEqual(self.pixels().tobytes(), self.expected())
        self.assertEqual(self.terminal.frame().cells[3][6].text, "X")

    def test_byte_fragmented_wide_character_cuts_both_columns_on_the_right_row(self):
        """A UTF-8 character is one native write even when every byte arrives alone."""
        for byte in self.SOLID + b"\x1b[2;2H" + "한".encode():
            self.terminal.feed(bytes((byte,)))
        cell = next(c for c in self.terminal.frame().cells[1] if c.x == 1)
        self.assertEqual((cell.text, cell.width), ("한", 2))
        self.assertEqual(self.pixels().tobytes(), self.expected((2, 3, 6, 6)))

    def test_remaining_colored_cells_keep_original_source_coordinates(self):
        """A split image run keeps both its original row and column pixel crop."""
        wire = (
            b'\x1bP0;1q"1;1;8;6'
            b"#1;2;100;0;0!2F$#2;2;0;100;0??!2F$"
            b"#3;2;0;0;100!4?!2F$#4;2;100;100;0!6?!2F$"
            b"#5;2;0;100;100!2w$#6;2;100;0;100??!2w$"
            b"#7;2;100;100;100!4?!2w$#8;2;0;0;0!6?!2w\x1b\\"
        )
        expected = Image.new("RGBA", (16, 12))
        for row, colors in enumerate(
            (
                ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)),
                ((0, 255, 255), (255, 0, 255), (255, 255, 255), (0, 0, 0)),
            )
        ):
            for column, color in enumerate(colors):
                expected.paste(
                    (*color, 255), (column * 2, row * 3, column * 2 + 2, row * 3 + 3)
                )
        self.terminal.feed(wire)
        self.assertEqual(self.pixels().tobytes(), expected.tobytes())
        self.terminal.feed(b"\x1b[1;2HX\x1b[2;2H\x1b[X")
        expected.paste(self.CLEAR, (2, 0, 4, 6))
        self.assertEqual(self.pixels().tobytes(), expected.tobytes())

    def test_combining_mark_changes_existing_glyph_and_cuts_its_cell(self):
        """The base replaces one image cell and an accent keeps native text semantics."""
        self.terminal.feed(self.SOLID + b"\x1b[He")
        self.terminal.feed("\u0301".encode())
        frame = self.terminal.frame()
        self.assertEqual(frame.cells[0][0].text, "e\u0301")
        self.assertEqual((frame.cursor_x, frame.cursor_y), (1, 0))
        self.assertEqual(self.pixels().tobytes(), self.expected((0, 0, 2, 3)))

    def test_grapheme_width_changes_cut_both_new_and_previous_wide_cells(self):
        """Variation selectors invalidate the base and old or newly occupied tail."""
        for base, selector, width in (
            ("\u2764", "\ufe0f", 2),
            ("\u231a", "\ufe0e", 1),
        ):
            for fragmented in (False, True):
                with self.subTest(base=base, fragmented=fragmented):
                    self.terminal.feed(b"\x1bc\x1b[?2027h" + self.SOLID + b"\x1b[H")
                    tail = (base + selector).encode()
                    chunks = (
                        (bytes((byte,)) for byte in tail) if fragmented else (tail,)
                    )
                    for chunk in chunks:
                        self.terminal.feed(chunk)
                    cell = self.terminal.frame().cells[0][0]
                    self.assertEqual((cell.text, cell.width), (base + selector, width))
                    self.assertEqual(
                        self.pixels().tobytes(), self.expected((0, 0, 4, 3))
                    )

    def test_printed_space_repeat_and_character_erasure_remove_image_cells(self):
        """Printed blanks, REP and ECH all replace their native image cells."""
        self.terminal.feed(self.SOLID + b"\x1b[H\x1b[4X")
        self.assertEqual(self.pixels().tobytes(), self.expected((0, 0, 8, 3)))
        self.terminal.feed(self.SOLID + b"\x1b[H \x1b[2b")
        self.assertEqual(self.pixels().tobytes(), self.expected((0, 0, 6, 3)))

    def test_overlapping_image_replacement_does_not_restore_previous_cells(self):
        """Deleting a replacement image does not resurrect overwritten image cells."""
        self.terminal.feed(self.SOLID)
        previous = self.terminal.frame().images[0].image_id
        self.terminal.feed(self.SOLID.replace(b"100;0;0", b"0;0;100"))
        identities = {p.image_id for p in self.terminal.frame().images}
        self.assertEqual(len(identities), 1)
        identity = identities.pop()
        self.assertNotEqual(identity, previous)
        self.terminal.feed(b"\x1b[1;2HX")
        top = self.pixels()
        self.assertEqual(top.getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual(top.getpixel((2, 0)), self.CLEAR)
        self.terminal.feed(f"\x1b_Ga=d,d=I,i={identity},q=2;\x1b\\".encode())
        self.assertFalse(self.terminal.frame().images)
        self.assertEqual(self.pixels().tobytes(), bytes(16 * 12 * 4))

    def test_ordinary_kitty_placements_keep_their_own_write_semantics(self):
        """The SIXEL supplement must not impose destructive writes on Kitty images."""
        blue = bytes((0, 0, 255, 255)) * 48
        self.terminal.feed(
            b"\x1b_Ga=T,i=42,f=32,s=8,v=6,q=2,C=1;" + base64.b64encode(blue) + b"\x1b\\"
        )
        self.terminal.feed(self.SOLID + b"\x1b[1;2HX")
        self.assertEqual(self.terminal.image(42).rgba, blue)
        self.assertEqual(self.pixels().getpixel((2, 0)), (0, 0, 255, 255))
        self.assertEqual(self.pixels().getpixel((0, 0)), (0, 0, 255, 255))
        self.terminal.feed(b"\x1b_Ga=d,d=I,i=42,q=2;\x1b\\")
        self.assertIsNone(self.terminal.image(42))
        self.assertEqual(self.pixels().tobytes(), self.expected((2, 0, 4, 3)))

    def test_kitty_replacing_a_sixel_id_keeps_native_asset_write_semantics(self):
        """Reusing an image ID preserves regular native Kitty placement behavior."""
        self.terminal.feed(self.SOLID)
        identity = self.terminal.frame().images[0].image_id
        blue = bytes((0, 0, 255, 255)) * 48
        self.terminal.feed(
            f"\x1b_Ga=T,i={identity},f=32,s=8,v=6,q=2,C=1;".encode()
            + base64.b64encode(blue)
            + b"\x1b\\\x1b[1;2HX"
        )
        self.assertEqual(self.terminal.image(identity).rgba, blue)
        self.assertEqual(self.pixels().getpixel((2, 0)), (0, 0, 255, 255))

    def test_cut_pixels_follow_native_history_and_alternate_screen_lifetime(self):
        """Scrolling and buffer switches cannot resurrect previously overwritten pixels."""
        self.terminal.feed(self.SOLID + b"\x1b[1;2HX")
        expected = self.expected((2, 0, 4, 3))
        identity = self.terminal.frame().images[0].image_id
        self.terminal.feed(b"\x1b[?1049h")
        self.assertIsNone(self.terminal.image(identity))
        self.terminal.feed(b"\x1b[?1049l")
        self.assertEqual(self.pixels().tobytes(), expected)
        self.terminal.feed(b"\r\n" * 7)
        self.assertFalse(self.terminal.frame().images)
        self.terminal.scroll_top()
        self.assertEqual(self.pixels().tobytes(), expected)
        self.terminal.scroll_bottom()
        self.terminal.feed(b"\x1bc" + self.SOLID)
        self.assertEqual(self.pixels().tobytes(), self.expected())

    def test_synchronized_frame_keeps_the_previous_pixels_until_commit(self):
        """The same native presentation hold applies to text and cut image pixels."""
        self.terminal.feed(self.SOLID)
        self.terminal.frame()
        self.terminal.feed(b"\x1b[?2026h\x1b[1;2HX")
        self.assertEqual(self.pixels().tobytes(), self.expected())
        self.terminal.feed(b"\x1b[?2026l")
        self.assertEqual(self.pixels().tobytes(), self.expected((2, 0, 4, 3)))

    def test_pending_wrap_uses_the_actual_destination_row(self):
        """The next printed character cuts the wrapped cell, not the old cursor cell."""
        self.terminal.feed(self.SOLID + b"\x1b[1;8HAZ")
        frame = self.terminal.frame()
        self.assertEqual(frame.cells[0][7].text, "A")
        self.assertEqual(frame.cells[1][0].text, "Z")
        self.assertEqual(self.pixels().tobytes(), self.expected((0, 3, 2, 6)))

    def test_origin_mode_uses_native_absolute_cell_coordinates(self):
        """A relative CUP inside the scrolling region still targets the native row."""
        self.terminal.feed(self.SOLID + b"\x1b[2;4r\x1b[?6h\x1b[1;2HX")
        self.assertEqual(self.terminal.frame().cells[1][1].text, "X")
        self.assertEqual(self.pixels().tobytes(), self.expected((2, 3, 4, 6)))


class SixelPtyTests(unittest.IsolatedAsyncioTestCase):
    """An actual PTY child receives native input and overwrites its own SIXEL output."""

    async def test_child_output_cuts_pixels_and_input_reaches_its_result_file(self):
        """Child files and final rendered pixels survive a fragmented PTY transfer."""
        with tempfile.TemporaryDirectory(prefix="ish-sixel-write-") as directory:
            result = Path(directory) / "input.bin"
            done = asyncio.Event()
            received = asyncio.Event()
            errors, exits = [], []
            observed = bytearray()
            with Terminal(8, 4, lambda _: None) as terminal:
                terminal.resize(8, 4, 2, 3)
                terminal.set_graphics(True)

                def output(data):
                    """Use the production input boundary on actual child PTY bytes."""
                    observed.extend(data)
                    terminal.feed(data)
                    received.set()

                def exited(code):
                    """Only final output completion releases the result assertion."""
                    exits.append(code)
                    done.set()

                script = (
                    "import os, pathlib, tty\n"
                    "tty.setraw(0)\n"
                    f"wire={SixelOverwriteTests.SOLID!r}\n"
                    "for byte in wire: os.write(1, bytes((byte,)))\n"
                    "os.write(1,b'\\x1b[4;1HREADY')\n"
                    "data=os.read(0,5)\n"
                    "while len(data)<5: data+=os.read(0,5-len(data))\n"
                    f"pathlib.Path({str(result)!r}).write_bytes(data)\n"
                    "os.write(1,b'\\x1b[1;2HX')\n"
                )
                session = Session(
                    [sys.executable, "-u", "-c", script],
                    os.getcwd(),
                    output,
                    exited,
                    errors.append,
                )
                session.start(8, 4)
                try:

                    async def wait_ready():
                        """The marker is a test rendezvous, never product readiness."""
                        while b"READY" not in observed:
                            received.clear()
                            await received.wait()

                    await asyncio.wait_for(wait_ready(), 5)
                    session.send(b"INPUT")
                    await asyncio.wait_for(done.wait(), 5)
                    self.assertEqual(result.read_bytes(), b"INPUT")
                    self.assertEqual(exits, [0])
                    self.assertEqual(errors, [])
                    frame = terminal.frame()
                    self.assertEqual(frame.cells[0][1].text, "X")
                    pixels = read_pixels(
                        RasterPresenter("sixel").plan(frame, terminal.image).after_text
                    )
                    self.assertEqual(pixels.getpixel((2, 0)), (0, 0, 0, 0))
                    self.assertEqual(pixels.getpixel((0, 0)), (255, 0, 0, 255))
                    self.assertEqual(pixels.getpixel((2, 3)), (255, 0, 0, 255))
                finally:
                    await session.close()


if __name__ == "__main__":
    unittest.main()
