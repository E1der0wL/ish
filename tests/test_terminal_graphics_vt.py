"""Observable rich VT contracts through the compiled native engine."""

import base64
import struct
import unittest
import zlib

from ish.terminal.vt import CTRL, Terminal

RED = b"\xff\x00\x00\xff"
BLUE = b"\x00\x00\xff\xff"


def graphics(parameters, payload=b""):
    """Build a documented Kitty command independently of the native binding."""
    return (
        b"\x1b_G" + parameters.encode() + b";" + base64.b64encode(payload) + b"\x1b\\"
    )


def png_pixel(pixel):
    """A deterministic 1x1 RGBA PNG, without invoking the production decoder."""

    def chunk(kind, data):
        """Encode a PNG chunk with its independent CRC."""
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
        + chunk(b"IEND", b"")
    )


class RichVTTests(unittest.TestCase):
    """Read rich terminal metadata from real native protocol processing."""

    def test_native_cells_preserve_width_link_and_underline_color(self):
        """Wide graphemes retain positions and OSC 8/SGR metadata."""
        with Terminal(20, 4, lambda _: None) as terminal:
            terminal.feed(
                b"\x1b]8;;https://example.com/\x1b\\\x1b[4:3;58:2::1:2:3m"
                + "한e\u0301".encode()
                + b"\x1b]8;;\x1b\\\x1b[0mZ"
            )
            cells = terminal.frame().cells[0]
            self.assertEqual(
                [(cell.x, cell.text, cell.width) for cell in cells[:3]],
                [(0, "한", 2), (2, "e\u0301", 1), (3, "Z", 1)],
            )
            self.assertEqual(cells[0].hyperlink, "https://example.com/")
            self.assertEqual(cells[0].underline, 3)
            self.assertEqual(cells[0].underline_color, (1, 2, 3))
            self.assertIsNone(cells[2].hyperlink)

    def test_cursor_shape_focus_and_effects_follow_native_state(self):
        """Effects are consumed once and focus reports obey mode 1004."""
        with Terminal(20, 4, lambda _: None) as terminal:
            self.assertEqual(terminal.encode_focus(True), b"")
            terminal.feed(
                b"\x1b[6 q\x1b[?1004h\x1b]2;new title\x07\x07\x1b]7;file://localhost/tmp\x1b\\"
            )
            frame = terminal.frame()
            self.assertEqual(frame.cursor_shape, "bar")
            self.assertFalse(frame.cursor_blink)
            self.assertEqual(terminal.encode_focus(True), b"\x1b[I")
            self.assertEqual(terminal.encode_focus(False), b"\x1b[O")
            self.assertEqual(
                [(e.kind, e.data) for e in terminal.drain_events()],
                [
                    ("title", b"new title"),
                    ("bell", b""),
                    ("pwd", b"file://localhost/tmp"),
                ],
            )
            self.assertEqual(terminal.drain_events(), ())
            terminal.feed(b"\x1b[?1004l")
            self.assertEqual(terminal.encode_focus(True), b"")

    def test_pixel_reports_use_confirmed_cell_geometry(self):
        """XTWINOPS replies agree with the frontend-provided cell pixels."""
        replies = []
        with Terminal(20, 4, replies.append) as terminal:
            terminal.resize(20, 4, 8, 16)
            terminal.feed(b"\x1b[14t\x1b[16t")
            self.assertEqual(b"".join(replies), b"\x1b[4;64;160t\x1b[6;16;8t")
            terminal.resize(30, 5)
            self.assertEqual(
                (terminal.frame().cell_width, terminal.frame().cell_height), (8, 16)
            )

    def test_sync_mode_and_key_actions_follow_child_negotiation(self):
        """Native repeat/release encodings never become duplicate plain input."""
        with Terminal(20, 4, lambda _: None) as terminal:
            self.assertFalse(terminal.synchronized_output)
            self.assertEqual(terminal.encode_key("c", CTRL, action="release"), b"")
            terminal.feed(b"\x1b[?2026h\x1b[>3u")
            self.assertTrue(terminal.synchronized_output)
            self.assertEqual(
                terminal.encode_key("c", CTRL, action="repeat"), b"\x1b[99;5:2u"
            )
            self.assertEqual(
                terminal.encode_key("c", CTRL, action="release"), b"\x1b[99;5:3u"
            )
            terminal.feed(b"\x1b[?2026l")
            self.assertFalse(terminal.synchronized_output)

    def test_graphics_cannot_enable_without_pixel_geometry(self):
        """An unrenderable image path cannot be advertised by the public API."""
        with Terminal(20, 4, lambda _: None) as terminal:
            with self.assertRaises(ValueError):
                terminal.set_graphics(True)

    def test_pixel_mouse_requires_real_pixels_and_converts_for_cell_clients(self):
        """The same physical point follows the child's negotiated wire format."""
        with Terminal(20, 6, lambda _: None) as terminal:
            terminal.resize(20, 6, 8, 16)
            terminal.feed(b"\x1b[?1000h\x1b[?1006h\x1b[?1016h")
            self.assertEqual(terminal.encode_mouse("press", "left", 2, 2), b"")
            self.assertEqual(
                terminal.encode_mouse("press", "left", 0, 0, pixel_position=(18, 35)),
                b"\x1b[<0;18;35M",
            )
            terminal.feed(b"\x1b[?1016l\x1b[?1006h")
            self.assertEqual(
                terminal.encode_mouse("press", "left", 0, 0, pixel_position=(18, 35)),
                b"\x1b[<0;3;3M",
            )

    def test_hold_captures_exact_boundary_and_end_begin_in_one_write(self):
        """The committed frame advances even if a second hold starts immediately."""
        replies = []
        with Terminal(20, 4, replies.append) as terminal:
            terminal.feed(b"old")
            terminal.frame()
            terminal.feed(b"\rready\x1b[?2026h\rpartial\x1b[6n")
            first = terminal.frame()
            self.assertEqual("".join(c.text for c in first.cells[0]).rstrip(), "ready")
            self.assertEqual(first.cursor_x, 5)
            self.assertEqual(replies[-1], b"\x1b[1;8R")
            serial = terminal.render_hold_serial
            terminal.feed(b"\x1b[?2026h")
            self.assertEqual(terminal.render_hold_serial, serial)
            terminal.feed(b"\rcomplete\x1b[K\x1b[?2026l\x1b[?2026h\rpartial")
            second = terminal.frame()
            self.assertEqual(
                "".join(c.text for c in second.cells[0]).rstrip(), "complete"
            )
            self.assertEqual(terminal.render_hold_serial, serial + 1)
            terminal.release_render_hold()
            self.assertFalse(terminal.synchronized_output)
            self.assertEqual(
                "".join(c.text for c in terminal.frame().cells[0]).rstrip(), "partiale"
            )
            terminal.feed(b"\x1b[?2026$p")
            self.assertEqual(replies[-1], b"\x1b[?2026;2$y")

    def test_resize_and_reset_end_a_presentation_hold(self):
        """Native geometry and reset cannot retain stale committed snapshots."""
        with Terminal(20, 4, lambda _: None) as terminal:
            terminal.feed(b"before\x1b[?2026h\rafter\x1b[K")
            terminal.resize(30, 5)
            self.assertFalse(terminal.synchronized_output)
            self.assertEqual(terminal.frame().cols, 30)
            self.assertEqual(
                "".join(c.text for c in terminal.frame().cells[0]).rstrip(), "after"
            )
            terminal.feed(b"\x1b[?2026h\x1bc")
            self.assertFalse(terminal.synchronized_output)
            self.assertEqual(
                "".join(c.text for c in terminal.frame().cells[0]).strip(), ""
            )


class GraphicsVTTests(unittest.TestCase):
    """Exercise image commands against native storage and placement state."""

    def terminal(self, replies=None):
        """Create a terminal with explicit geometry and cleanup."""
        terminal = Terminal(20, 6, (replies if replies is not None else []).append)
        self.addCleanup(terminal.close)
        terminal.resize(20, 6, 8, 16)
        return terminal

    def test_graphics_are_unadvertised_until_enabled_and_disable_erases_assets(self):
        """An unsupported output path must never answer image queries with OK."""
        replies = []
        terminal = self.terminal(replies)
        query = graphics("a=q,f=32,i=40,s=1,v=1", RED)
        terminal.feed(query)
        self.assertNotIn(b";OK", b"".join(replies))
        terminal.feed(b"\x1bc\x1b[?1049h" + query + b"\x1b[?1049l")
        self.assertNotIn(b";OK", b"".join(replies))
        terminal.set_graphics(True)
        replies.clear()
        terminal.feed(query)
        self.assertEqual(b"".join(replies), b"\x1b_Gi=40;OK\x1b\\")
        terminal.feed(graphics("a=T,f=32,i=7,s=1,v=1", RED))
        self.assertEqual(terminal.image(7).rgba, RED)
        terminal.set_graphics(False)
        self.assertEqual(terminal.frame().images, ())
        self.assertIsNone(terminal.image(7))

    def test_pixels_crop_layer_and_generation_are_real_native_results(self):
        """A source crop, scaled placement, and replacement match known pixels."""
        terminal = self.terminal()
        terminal.set_graphics(True)
        terminal.feed(
            b"\x1b[2;3H"
            + graphics(
                "a=T,f=32,i=7,p=9,s=2,v=1,x=1,y=0,w=1,h=1,c=2,r=1,z=-1", RED + BLUE
            )
        )
        frame = terminal.frame()
        self.assertEqual(len(frame.images), 1)
        placement = frame.images[0]
        self.assertEqual((placement.id, placement.image_id), (9, 7))
        self.assertEqual(
            (placement.x, placement.y, placement.width, placement.height),
            (16, 16, 16, 16),
        )
        self.assertEqual(
            (
                placement.source_x,
                placement.source_y,
                placement.source_width,
                placement.source_height,
            ),
            (1, 0, 1, 1),
        )
        self.assertEqual((placement.layer, placement.z), (1, -1))
        image = terminal.image(7)
        self.assertEqual((image.width, image.height, image.rgba), (2, 1, RED + BLUE))
        self.assertEqual(image.generation, placement.image_generation)
        terminal.feed(graphics("a=t,f=32,i=7,s=2,v=1", BLUE + RED))
        replaced = terminal.image(7)
        self.assertNotEqual(replaced.generation, image.generation)
        self.assertEqual(replaced.rgba, BLUE + RED)

    def test_png_decoder_and_split_transmission_preserve_pixels(self):
        """A PNG split at every wire byte decodes to the independent fixture."""
        terminal = self.terminal()
        terminal.set_graphics(True)
        command = graphics("a=T,f=100,i=8", png_pixel(BLUE))
        for byte in command:
            terminal.feed(bytes([byte]))
        self.assertEqual(terminal.image(8).rgba, BLUE)
        self.assertEqual(len(terminal.frame().images), 1)

    def test_virtual_and_relative_placements_are_resolved_without_leaking_placeholders(
        self,
    ):
        """The outer frame carries rectangles, never child image-ID glyphs."""
        terminal = self.terminal()
        terminal.set_graphics(True)
        terminal.feed(graphics("a=T,f=32,i=1,p=1,s=1,v=1,U=1,c=2,r=1,q=2", RED))
        terminal.feed(b"\x1b[2;4H\x1b[38;5;1;58;5;1m" + ("\U0010eeee" * 2).encode())
        terminal.feed(
            graphics("a=T,f=32,i=2,p=2,s=1,v=1,P=1,Q=1,H=2,V=1,c=1,r=1,q=2", BLUE)
        )
        frame = terminal.frame()
        placements = {p.image_id: p for p in frame.images}
        self.assertEqual(set(placements), {1, 2})
        parent, child = placements[1], placements[2]
        self.assertEqual(
            (parent.x, parent.y, parent.width, parent.height), (24, 16, 16, 16)
        )
        self.assertEqual((child.x, child.y, child.width, child.height), (40, 32, 8, 16))
        self.assertNotIn(
            "\U0010eeee", "".join(c.text for row in frame.cells for c in row)
        )

    def test_screens_delete_and_scrollback_keep_assets_separate(self):
        """Screen switches and history preserve the correct image identity."""
        terminal = self.terminal()
        terminal.set_graphics(True)
        terminal.feed(graphics("a=T,f=32,i=7,s=1,v=1,c=2,r=2", RED))
        primary_generation = terminal.image(7).generation
        terminal.feed(b"\x1b[?1049h\x1b[H" + graphics("a=T,f=32,i=7,s=1,v=1", BLUE))
        self.assertEqual(terminal.image(7).rgba, BLUE)
        terminal.feed(b"\x1b[?1049l")
        self.assertEqual(terminal.image(7).generation, primary_generation)
        self.assertEqual(terminal.image(7).rgba, RED)
        terminal.feed(b"\r\n" * 12)
        self.assertEqual(terminal.frame().images, ())
        terminal.scroll_top()
        self.assertTrue(terminal.frame().images)
        terminal.feed(graphics("a=d,d=I,i=7"))
        self.assertIsNone(terminal.image(7))
        self.assertEqual(terminal.frame().images, ())

    def test_animation_tick_changes_pixels_and_next_deadline(self):
        """Clock advancement selects the next native animation frame."""
        terminal = self.terminal()
        terminal.set_graphics(True)
        terminal.feed(graphics("a=T,f=32,i=2,s=1,v=1,q=2", RED))
        terminal.feed(graphics("a=f,f=32,i=2,s=1,v=1,z=40,q=2", BLUE))
        terminal.feed(graphics("a=a,i=2,r=1,z=30,q=2"))
        terminal.feed(graphics("a=a,i=2,s=3,q=2"))
        first = terminal.image(2)
        next_ms = terminal.tick(1000)
        self.assertIsNotNone(next_ms)
        terminal.tick(next_ms)
        second = terminal.image(2)
        self.assertNotEqual(first.generation, second.generation)
        self.assertEqual(second.rgba, BLUE)
        terminal.feed(graphics("a=a,i=2,s=1,q=2"))
        self.assertIsNone(terminal.tick(next_ms + 100))

    def test_hold_retains_image_pixels_until_the_next_committed_boundary(self):
        """Replacing or deleting a live asset cannot mutate a held presentation."""
        terminal = self.terminal()
        terminal.set_graphics(True)
        terminal.feed(graphics("a=T,f=32,i=7,p=1,s=1,v=1", RED))
        original = terminal.image(7)
        terminal.feed(b"\x1b[?2026h" + graphics("a=T,f=32,i=7,p=1,s=1,v=1", BLUE))
        self.assertEqual(terminal.image(7), original)
        self.assertEqual(
            terminal.frame().images[0].image_generation, original.generation
        )
        terminal.feed(b"\x1b[?2026l\x1b[?2026h" + graphics("a=d,d=I,i=7"))
        self.assertEqual(terminal.image(7).rgba, BLUE)
        self.assertTrue(terminal.frame().images)
        terminal.feed(b"\x1b[?2026l")
        self.assertIsNone(terminal.image(7))
        self.assertEqual(terminal.frame().images, ())


if __name__ == "__main__":
    unittest.main()
