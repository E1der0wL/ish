"""Image wire inputs through actual framing, native storage and grid mutation."""

import base64
import struct
import unittest
import zlib

from ish.terminal.vt import Terminal

RED = b"\xff\0\0\xff"
BLUE = b"\0\0\xff\xff"
SIXEL = b'\x1bP0;0q"1;1;2;6#1;2;100;0;0~?\x1b\\'


def png(width, height, pixel=RED):
    """Encode known solid pixels with stdlib rather than the production codec."""

    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data))
        )

    raw = (b"\0" + pixel * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def iterm(payload, options=b"inline=1"):
    """Build an OSC 1337 image transfer independently of its implementation."""
    return b"\x1b]1337;File=" + options + b":" + base64.b64encode(payload) + b"\x07"


class ImageProtocolTests(unittest.TestCase):
    """Assert actual raster bytes and native cursor, erase and storage behavior."""

    def setUp(self):
        """Create a small native grid with known measured cell dimensions."""
        self.replies = []
        self.terminal = Terminal(8, 4, self.replies.append)
        self.addCleanup(self.terminal.close)
        self.terminal.resize(8, 4, 2, 3)
        self.terminal.set_graphics(True)

    def image(self):
        """Read the native asset referenced by a visible placement."""
        frame = self.terminal.frame()
        self.assertTrue(frame.images, self.terminal.image_errors)
        return self.terminal.image(frame.images[0].image_id)

    def test_iterm_file_uses_native_pixels_cursor_and_placeholder_erasure(self):
        """Inline cells move the cursor right and disappear on ordinary erasure."""
        self.terminal.feed(b"A" + iterm(png(4, 3)) + b"B")
        image = self.image()
        self.assertEqual((image.width, image.height, image.rgba), (4, 3, RED * 12))
        frame = self.terminal.frame()
        self.assertEqual((frame.cursor_x, frame.cursor_y), (4, 0))
        self.assertEqual(frame.cells[0][0].text, "A")
        self.assertEqual(frame.cells[0][3].text, "B")
        self.terminal.feed(b"\x1b[1;2H\x1b[2X")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(self.replies, [])

    def test_fragmented_multipart_commits_only_at_fileend(self):
        """Arbitrary PTY splits cannot make a partial image visible."""
        encoded = base64.b64encode(png(2, 3, BLUE))
        self.terminal.feed(b"\x1b]1337;MultipartFile=inline=1\x07")
        for part in (encoded[:7], encoded[7:]):
            for byte in b"\x1b]1337;FilePart=" + part + b"\x1b\\":
                self.terminal.feed(bytes((byte,)))
        self.assertEqual(self.terminal.frame().images, ())
        self.terminal.feed(b"\x1b]1337;FileEnd\x07")
        self.assertEqual(self.image().rgba, BLUE * 6)

    def test_cancel_reset_and_invalid_data_never_render_encoded_payload(self):
        """Cancel and invalid base64 cannot leak image bytes into visible text."""
        self.terminal.feed(b"\x1b]1337;MultipartFile=inline=1\x07")
        self.terminal.feed(b"\x1b]1337;FilePart=AAAA\x18")
        self.terminal.feed(b"\x1b]1337;FileEnd\x07")
        self.terminal.feed(b"\x1b]1337;File=inline=1:%%%\x07OK")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(
            "".join(c.text for c in self.terminal.frame().cells[0])[:2], "OK"
        )
        self.assertTrue(self.terminal.image_errors)
        self.terminal.feed(iterm(png(2, 3)))
        self.assertTrue(self.terminal.frame().images)
        self.terminal.feed(b"\x1bc")
        self.assertEqual(self.terminal.frame().images, ())

    def test_dimensions_aspect_padding_and_stationary_cursor(self):
        """Percent and cell dimensions honor an explicit stationary cursor."""
        self.terminal.feed(b"AB")
        self.terminal.feed(
            iterm(
                png(2, 1),
                b"inline=1;width=50%;height=2;preserveAspectRatio=0;doNotMoveCursor=1",
            )
        )
        image = self.image()
        self.assertEqual((image.width, image.height), (8, 6))
        self.assertEqual(image.rgba, RED * 48)
        frame = self.terminal.frame()
        self.assertEqual((frame.cursor_x, frame.cursor_y), (2, 0))
        self.terminal.feed(b"X")
        self.assertEqual(self.terminal.frame().cells[0][2].text, "X")

    def test_image_preserves_current_style_and_preexisting_saved_cursor(self):
        """Image insertion cannot replace the child's saved cursor or SGR state."""
        self.terminal.feed(
            b"\x1b[31m\x1b7\x1b[2;3H\x1b[4:3;38;2;1;2;3;48;2;4;5;6;58;5;9m"
        )
        before = self.terminal.image_context().style
        self.terminal.feed(iterm(png(2, 3)))
        self.assertEqual(self.terminal.image_context().style, before)
        self.terminal.feed(b"Z\x1b8Q")
        frame = self.terminal.frame()
        self.assertEqual(frame.cells[1][3].text, "Z")
        self.assertEqual(frame.cells[1][3].fg, (1, 2, 3))
        self.assertEqual(frame.cells[1][3].underline, 3)
        self.assertEqual(frame.cells[0][0].text, "Q")
        self.assertNotEqual(frame.cells[0][0].fg, (1, 2, 3))

    def test_native_scrollback_and_screen_switch_own_inline_image_lifetime(self):
        """Image cells follow the existing native history and screen buffers."""
        self.terminal.feed(b"\x1b[4;1H" + iterm(png(2, 6)))
        identity = self.image().id
        self.terminal.feed(b"\r\n" * 5)
        self.assertFalse(self.terminal.frame().images)
        self.terminal.scroll(-5)
        self.assertTrue(
            any(p.image_id == identity for p in self.terminal.frame().images)
        )
        self.terminal.scroll_bottom()
        self.terminal.feed(b"\x1b[?1049h")
        self.assertIsNone(self.terminal.image(identity))
        self.terminal.feed(b"\x1b[?1049l")
        self.assertIsNotNone(self.terminal.image(identity))

    def test_mixed_kitty_ids_are_not_replaced_by_normalization(self):
        """Choosing an adapter image ID skips already loaded child assets."""
        self.terminal.feed(
            b"\x1b_Ga=T,i=16777215,f=32,s=1,v=1,q=2,C=1;"
            + base64.b64encode(BLUE)
            + b"\x1b\\"
        )
        self.terminal.feed(iterm(png(2, 3)))
        self.assertEqual(self.terminal.image(16777215).rgba, BLUE)
        self.assertEqual(self.terminal.image(16777214).rgba, RED * 6)
        self.terminal.feed(
            b"\x1b_Ga=T,i=16777215,f=32,s=1,v=1,q=2,C=1;"
            + base64.b64encode(RED)
            + b"\x1b\\"
        )
        self.assertEqual(self.terminal.image(16777214).rgba, RED * 6)

    def test_disabled_images_and_downloads_do_not_create_files_or_cells(self):
        """No host capability means no image and downloads never write files."""
        self.terminal.set_graphics(False)
        self.terminal.feed(iterm(png(2, 3)) + SIXEL + b"OK")
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(self.terminal.frame().cells[0][0].text, "O")
        self.terminal.set_graphics(True)
        self.terminal.feed(iterm(png(2, 3), b"inline=0;name=L3RtcC9ub3QtYS1maWxl"))
        self.assertEqual(self.terminal.frame().images, ())
        self.assertIn("File download", self.terminal.image_errors[-1])

    def test_sixel_adapter_replaces_covered_text_and_uses_native_storage(self):
        """Both SIXEL backgrounds use native image cells without protocol leakage."""
        self.terminal.feed(b"AB\r")
        for parameter in (b"0", b"1", b"01"):
            wire = SIXEL.replace(b"0;0q", b"0;" + parameter + b"q")
            for byte in wire:
                self.terminal.feed(bytes((byte,)))
        frame = self.terminal.frame()
        self.assertEqual("".join(c.text for c in frame.cells[0])[:2], " B")
        self.assertEqual((frame.cursor_x, frame.cursor_y), (0, 0))
        self.assertEqual(len({image.image_id for image in frame.images}), 1)
        self.assertEqual(self.terminal.image_errors, ())
        self.terminal.feed(b"\x1b[c")
        self.assertEqual(self.replies.pop(), b"\x1b[?62;22;4c")

    def test_iterm_upload_inside_synchronized_output_waits_for_frame_release(self):
        """Live storage validation is independent of the frozen display snapshot."""
        self.terminal.frame()
        self.terminal.feed(b"\x1b[?2026h" + iterm(png(2, 3)))
        self.assertEqual(self.terminal.frame().images, ())
        self.assertEqual(self.terminal.image_errors, ())
        self.terminal.feed(b"\x1b[?2026l")
        self.assertEqual(self.image().rgba, RED * 6)

    def test_stationary_iterm_image_never_scrolls_a_smaller_margin_region(self):
        """doNotMoveCursor also preserves rows outside its image cell rectangle."""
        self.terminal.feed(
            b"\x1b[1;5HTOP\x1b[2;5HMID\x1b[3;5HLOW\x1b[4;5HEND\x1b[1;2r\x1b[H"
        )
        self.terminal.feed(iterm(png(2, 9), b"inline=1;doNotMoveCursor=1"))
        frame = self.terminal.frame()
        self.assertEqual((frame.cursor_x, frame.cursor_y), (0, 0))
        self.assertEqual(
            ["".join(cell.text for cell in row)[4:7] for row in frame.cells],
            ["TOP", "MID", "LOW", "END"],
        )
        coverage = {
            (x, y)
            for item in frame.images
            for y in range(item.y, item.y + item.height)
            for x in range(item.x, item.x + item.width)
        }
        self.assertEqual(coverage, {(x, y) for x in range(2) for y in range(9)})
        self.assertEqual(self.image().rgba, RED * 18)


if __name__ == "__main__":
    unittest.main()
