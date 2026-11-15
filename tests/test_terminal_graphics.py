"""Verify composited image pixels and Kitty commands with independent wire reads."""

import base64
import io
import os
import random
import re
import unittest
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.widgets import TextArea

from ish.terminal.graphics import (
    GraphicsCapabilityError,
    GraphicsError,
    GraphicsLimitError,
    GraphicsPresenter,
    RasterPresenter,
    Rect,
    create_presenter,
)


@dataclass(frozen=True)
class Image:
    """Native asset fixture, without using the production snapshot encoder."""

    id: int = 7
    generation: int = 1
    width: int = 4
    height: int = 4
    rgba: bytes = bytes((255, 0, 0, 128)) * 16


@dataclass(frozen=True)
class Placement:
    """Native placement fixture in pixel coordinates."""

    id: int = 9
    image_id: int = 7
    image_generation: int = 1
    x: int = 0
    y: int = 0
    width: int = 4
    height: int = 4
    source_x: int = 0
    source_y: int = 0
    source_width: int = 4
    source_height: int = 4
    layer: int = 2
    z: int = 0


@dataclass(frozen=True)
class Frame:
    """Minimal public snapshot accepted by the presenter."""

    images: tuple[Placement, ...] = (Placement(),)
    cols: int = 10
    rows: int = 10
    cell_width: int = 2
    cell_height: int = 2
    cells: tuple = ()


@dataclass(frozen=True)
class BlankCell:
    """Independent native text fixture for below-text placement checks."""

    x: int
    text: str = " "
    width: int = 1
    underline: int = 0
    overline: bool = False
    style: str = ""


def packets(wire):
    """Read literal Kitty APC packets without invoking a production decoder."""
    return [
        (dict(part.split("=", 1) for part in control.split(",")), payload)
        for control, payload in re.findall("\x1b_G([^;]*);(.*?)\x1b\\\\", wire, re.S)
    ]


def uploads(wire):
    """Reassemble and decompress complete RGBA uploads from wire packets."""
    found = []
    metadata = None
    encoded = []
    for control, payload in packets(wire):
        if control.get("a") == "t":
            metadata = control
            encoded = []
        if metadata is not None:
            encoded.append(payload)
            if control.get("m") == "0":
                found.append(
                    (metadata, zlib.decompress(base64.b64decode("".join(encoded))))
                )
                metadata = None
    return found


class RasterTests(unittest.TestCase):
    """Check legacy wire payloads, composition and explicit semantic limitations."""

    def png(self, wire):
        """Decode the file actually sent in the OSC instead of using scene internals."""
        from PIL import Image as Pixels

        match = re.search("\x1b]1337;File=([^:]+):([^\x1b]+)\x1b\\\\", wire)
        self.assertIsNotNone(match)
        self.assertIn("inline=1", match[1])
        self.assertIn("doNotMoveCursor=1", match[1])
        return Pixels.open(io.BytesIO(base64.b64decode(match[2]))).convert("RGBA")

    def test_output_factory_requires_a_verified_protocol(self):
        """The selection point emits only the selected wire or no graphics at all."""
        disabled = create_presenter(None)
        plan = disabled.plan(Frame(), lambda _: self.fail("Disabled asset read"))
        self.assertEqual(
            (plan.before_text, plan.after_text, plan.repaint), ("", "", False)
        )
        self.assertEqual(disabled.close(), "")
        self.assertFalse(disabled.invalidate(7))
        kitty = create_presenter("kitty").plan(Frame(), lambda _: Image())
        self.assertEqual(uploads(kitty.before_text)[0][1], Image().rgba)
        iterm = create_presenter("iterm").plan(Frame(), lambda _: Image())
        self.assertEqual(self.png(iterm.after_text).tobytes(), Image().rgba)
        for unsupported in ("sixel", "unknown"):
            with self.subTest(protocol=unsupported):
                with self.assertRaises(GraphicsError):
                    create_presenter(unsupported)

    def test_iterm_popup_alpha_overlap_and_native_asset_preservation(self):
        """A single composited PNG preserves overlapping alpha and masks the popup."""
        red = replace(Image(), rgba=bytes((255, 0, 0, 255)) * 16)
        blue = replace(Image(), id=8, rgba=bytes((0, 0, 255, 128)) * 16)
        assets = {7: red, 8: blue}
        second = replace(Placement(), id=10, image_id=8, x=2, z=1)
        presenter = RasterPresenter("iterm")
        plan = presenter.plan(
            Frame(images=(Placement(), second)),
            assets.get,
            occluders=(Rect(2, 2, 2, 2),),
        )
        pixels = self.png(plan.after_text)
        self.assertEqual(pixels.size, (6, 4))
        self.assertEqual(pixels.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(pixels.getpixel((2, 0)), (127, 0, 128, 255))
        self.assertEqual(pixels.getpixel((4, 0)), (0, 0, 255, 128))
        self.assertEqual(pixels.getpixel((2, 2)), (0, 0, 0, 0))
        self.assertEqual(assets[7].rgba, bytes((255, 0, 0, 255)) * 16)

    def test_iterm_scene_cache_erase_and_hide_restore(self):
        """Unchanged pixels avoid reencoding; changes erase owned cells and repaint text."""
        presenter = RasterPresenter("iterm")
        first = presenter.plan(Frame(), lambda _: Image())
        with patch(
            "ish.terminal.raster.iterm",
            side_effect=AssertionError("Unexpected reencode"),
        ):
            same = presenter.plan(
                Frame(), lambda _: self.fail("Unexpected native pixel read")
            )
        self.assertEqual(first.after_text, same.after_text)
        self.assertFalse(same.repaint)
        hidden = presenter.plan(
            Frame(), lambda _: Image(), occluders=(Rect(0, 0, 4, 4),)
        )
        self.assertTrue(hidden.repaint)
        self.assertEqual(
            hidden.before_text, "\x1b7\x1b[0m\x1b[1;1H\x1b[2X\x1b[2;1H\x1b[2X\x1b8"
        )
        self.assertFalse(hidden.after_text)
        restored = presenter.plan(Frame(), lambda _: Image())
        self.assertEqual(self.png(restored.after_text).tobytes(), Image().rgba)
        self.assertEqual(presenter.close(), hidden.before_text)
        self.assertFalse(presenter.close())
        self.assertFalse(presenter.invalidate(7))

    def test_iterm_clips_huge_scale_and_offsets_with_transparent_padding(self):
        """The viewport bounds allocation and cell alignment preserves pixel offsets."""
        presenter = RasterPresenter("iterm", max_image_bytes=64)
        placement = replace(Placement(), x=-5000, y=-5000, width=10000, height=10000)
        plan = presenter.plan(
            Frame(images=(placement,), cols=2, rows=2), lambda _: Image()
        )
        self.assertEqual(self.png(plan.after_text).size, (4, 4))
        small = replace(Placement(), x=1, y=1, width=2, height=2)
        shifted = presenter.plan(Frame(images=(small,)), lambda _: Image())
        pixels = self.png(shifted.after_text)
        self.assertEqual(pixels.size, (3, 3))
        self.assertEqual(pixels.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(pixels.getpixel((1, 1)), (255, 0, 0, 128))

    def test_legacy_semantic_and_resource_failures_do_not_destroy_previous_state(self):
        """Unsupported layering is explicit and a rejected plan leaves cleanup usable."""
        presenter = RasterPresenter("iterm", max_image_bytes=64)
        presenter.plan(Frame(), lambda _: Image())
        with self.assertRaises(GraphicsCapabilityError):
            presenter.plan(
                Frame(images=(replace(Placement(), layer=1),)), lambda _: Image()
            )
        with self.assertRaises(GraphicsLimitError):
            presenter.plan(
                Frame(images=(replace(Placement(), width=8, height=8),)),
                lambda _: Image(),
            )
        self.assertIn("\x1b[2X", presenter.close())
        self.assertIsInstance(create_presenter("kitty"), GraphicsPresenter)
        with self.assertRaises(GraphicsError):
            create_presenter("unknown")

    def test_below_text_inline_placeholders_need_proven_blank_cells(self):
        """Blank native placeholder cells are safe, but visible text is not bypassed."""
        presenter = RasterPresenter("iterm")
        cells = tuple(tuple(BlankCell(x) for x in range(10)) for _ in range(10))
        frame = Frame(images=(replace(Placement(), layer=1),), cells=cells)
        self.assertEqual(
            self.png(presenter.plan(frame, lambda _: Image()).after_text).tobytes(),
            Image().rgba,
        )
        changed = (
            tuple(
                replace(cell, text="X") if cell.x == 0 else cell for cell in cells[0]
            ),
            *cells[1:],
        )
        with self.assertRaises(GraphicsCapabilityError):
            presenter.plan(replace(frame, cells=changed), lambda _: Image())

    def test_overlined_spaces_are_visible_below_text_content(self):
        """An image must not cover a visible line merely because its glyph is space."""
        cells = tuple(tuple(BlankCell(x) for x in range(10)) for _ in range(10))
        frame = Frame(images=(replace(Placement(), layer=1),), cells=cells)
        presenter = create_presenter("iterm")
        self.png(presenter.plan(frame, lambda _: Image()).after_text)
        decorated = (
            (replace(cells[0][0], overline=True), *cells[0][1:]),
            *cells[1:],
        )
        with self.assertRaises(GraphicsCapabilityError):
            presenter.plan(replace(frame, cells=decorated), lambda _: Image())
        self.assertIn("\x1b[2X", presenter.close())
        self.assertEqual(
            self.png(presenter.plan(frame, lambda _: Image()).after_text).tobytes(),
            Image().rgba,
        )


class GraphicsTests(unittest.TestCase):
    """Exercise visible outcomes, ownership and bounded resource lifetimes."""

    def test_partial_popup_removes_only_the_covered_pixels(self):
        """Opaque popup pixels never contain the native image; alpha survives."""
        presenter = GraphicsPresenter(first_id=101)
        plan = presenter.plan(Frame(), lambda _: Image(), occluders=(Rect(1, 1, 2, 2),))
        metadata, rgba = uploads(plan.before_text)[0]
        self.assertEqual(metadata["i"], "101")
        self.assertEqual(rgba, bytes((255, 0, 0, 128)) * 16)
        placements = [p for p, _ in packets(plan.after_text) if p.get("a") == "p"]
        visible = set()
        for item in placements:
            left, top, width, height = (int(item[k]) for k in ("x", "y", "w", "h"))
            area = {
                (x, y)
                for x in range(left, left + width)
                for y in range(top, top + height)
            }
            self.assertFalse(visible & area)
            visible |= area
            self.assertEqual(item["C"], "1")
            self.assertNotIn("c", item)
            self.assertNotIn("r", item)
        expected = {(x, y) for x in range(4) for y in range(4)} - {
            (1, 1),
            (1, 2),
            (2, 1),
            (2, 2),
        }
        self.assertEqual(visible, expected)
        self.assertTrue(plan.after_text.startswith("\x1b7"))
        self.assertTrue(plan.after_text.endswith("\x1b8"))

    def test_hide_restore_and_unchanged_frames_reuse_uploaded_pixels(self):
        """A popup hides placements without deleting the underlying outer asset."""
        presenter = GraphicsPresenter(first_id=111)
        calls = []

        def lookup(image_id):
            calls.append(image_id)
            return Image()

        first = presenter.plan(Frame(), lookup)
        hidden = presenter.plan(Frame(), lookup, occluders=(Rect(0, 0, 20, 20),))
        self.assertEqual(hidden.after_text, "")
        self.assertEqual([p[0]["d"] for p in packets(hidden.before_text)], ["i"])
        restored = presenter.plan(Frame(), lookup)
        repeated = presenter.plan(Frame(), lookup)
        self.assertEqual(calls, [7])
        self.assertEqual(uploads(restored.before_text), [])
        self.assertEqual(repeated.before_text, "")
        self.assertTrue(first.after_text)
        self.assertTrue(restored.after_text)
        self.assertTrue(repeated.after_text)

    def test_native_delete_and_close_never_delete_unowned_images(self):
        """Deletion targets the allocated outer ID, never a native or global ID."""
        presenter = GraphicsPresenter(first_id=121)
        presenter.plan(Frame(), lambda _: Image())
        deleted = presenter.plan(Frame(images=()), lambda _: None)
        commands = [p for p, _ in packets(deleted.before_text)]
        self.assertEqual(commands, [{"a": "d", "d": "I", "i": "121", "q": "2"}])
        self.assertEqual(presenter.close(), "")
        presenter.plan(Frame(), lambda _: Image())
        self.assertIn("i=122", presenter.close())

    def test_animation_generation_replaces_pixels_even_when_hidden(self):
        """Uncover displays the newest native frame, not a stale cached image."""
        presenter = GraphicsPresenter(first_id=131)
        presenter.plan(Frame(), lambda _: Image())
        changed = Frame(images=(replace(Placement(), image_generation=2),))
        hidden = presenter.plan(changed, lambda _: None, occluders=(Rect(0, 0, 4, 4),))
        self.assertIn("d=I,i=131", hidden.before_text)
        blue = Image(generation=2, rgba=bytes((0, 0, 255, 64)) * 16)
        shown = presenter.plan(changed, lambda _: blue)
        metadata, rgba = uploads(shown.before_text)[0]
        self.assertEqual(metadata["i"], "132")
        self.assertEqual(rgba, bytes((0, 0, 255, 64)) * 16)

    def test_scaled_source_crop_and_pixel_origin_are_exact(self):
        """Scale once before clipping; outer placements need no rounded cell scaling."""
        rgba = bytes((255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 64, 255, 255, 255, 0))
        image = Image(width=4, height=1, rgba=rgba)
        placement = replace(
            Placement(),
            x=-1,
            y=1,
            source_x=1,
            source_y=0,
            source_width=2,
            source_height=1,
            width=4,
            height=2,
        )
        presenter = GraphicsPresenter(first_id=141)
        plan = presenter.plan(
            Frame(images=(placement,)),
            lambda _: image,
            origin=(2, 2),
            clip=Rect(2, 2, 20, 20),
        )
        _, output = uploads(plan.before_text)[0]
        self.assertEqual(output, (rgba[4:8] * 2 + rgba[8:12] * 2) * 2)
        control = packets(plan.after_text)[0][0]
        self.assertEqual(
            {k: control[k] for k in ("x", "y", "w", "h", "X", "Y")},
            {"x": "1", "y": "0", "w": "3", "h": "2", "X": "0", "Y": "1"},
        )
        self.assertIn("\x1b[2;2H", plan.after_text)

    def test_layer_order_preserves_below_background_and_below_text(self):
        """Host z values preserve all three native compositing layers."""
        frame = Frame(
            images=tuple(
                replace(Placement(), id=layer + 1, layer=layer) for layer in range(3)
            )
        )
        plan = GraphicsPresenter(first_id=151).plan(frame, lambda _: Image())
        self.assertEqual(len(uploads(plan.before_text)), 1)
        z = [int(p["z"]) for p, _ in packets(plan.after_text)]
        self.assertLess(z[0], -1073741824)
        self.assertGreaterEqual(z[1], -1073741824)
        self.assertLess(z[1], 0)
        self.assertGreaterEqual(z[2], 0)
        self.assertEqual(len({p["p"] for p, _ in packets(plan.after_text)}), 3)

    def test_outer_error_invalidates_only_owned_resource(self):
        """An ENOENT recovery sends pixels again under a fresh outer identity."""
        presenter = GraphicsPresenter(first_id=161)
        presenter.plan(Frame(), lambda _: Image())
        self.assertFalse(presenter.invalidate(7))
        self.assertTrue(presenter.invalidate(161))
        recovered = presenter.plan(Frame(), lambda _: Image())
        self.assertIn("d=I,i=161", recovered.before_text)
        self.assertEqual(uploads(recovered.before_text)[0][0]["i"], "162")

    def test_chunked_upload_is_complete_and_bounded(self):
        """Incompressible pixels are split at protocol payload limits without loss."""
        pixels = random.Random(42).randbytes(64 * 64 * 4)
        image = Image(width=64, height=64, rgba=pixels)
        placement = replace(
            Placement(), width=64, height=64, source_width=64, source_height=64
        )
        plan = GraphicsPresenter(first_id=171).plan(
            Frame(images=(placement,), cols=32, rows=32), lambda _: image
        )
        messages = packets(plan.before_text)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(0 < len(payload) <= 4096 for _, payload in messages))
        self.assertEqual(messages[-1][0]["m"], "0")
        self.assertEqual(uploads(plan.before_text)[0][1], pixels)

    def test_rejected_frame_does_not_corrupt_previous_presentation(self):
        """Quota failures preserve a working frame and do not reserve host IDs."""
        presenter = GraphicsPresenter(first_id=181, max_bytes=64)
        presenter.plan(Frame(), lambda _: Image())
        oversized = Frame(images=(replace(Placement(), width=5),))
        with self.assertRaises(GraphicsError):
            presenter.plan(oversized, lambda _: Image())
        unchanged = presenter.plan(Frame(), lambda _: self.fail("Cache must survive"))
        self.assertEqual(unchanged.before_text, "")
        self.assertEqual([p["i"] for p, _ in packets(unchanged.after_text)], ["181"])

    def test_overlapping_popups_and_viewport_clip_leave_no_double_draw(self):
        """Overlapping masks cover their union, including partly offscreen images."""
        placement = replace(Placement(), x=-1, y=-1)
        plan = GraphicsPresenter(first_id=191).plan(
            Frame(images=(placement,)),
            lambda _: Image(),
            clip=Rect(0, 0, 3, 3),
            occluders=(Rect(1, 0, 1, 2), Rect(0, 1, 3, 1)),
        )
        visible = []
        for p, _ in packets(plan.after_text):
            sx, sy, width, height = (int(p[k]) for k in ("x", "y", "w", "h"))
            visible.extend(
                (x - 1, y - 1)
                for x in range(sx, sx + width)
                for y in range(sy, sy + height)
            )
        self.assertEqual(sorted(visible), [(0, 0), (0, 2), (1, 2), (2, 0), (2, 2)])

    def test_resize_rebuilds_derived_pixels_without_mutating_native_asset(self):
        """A new display size replaces its host variant, preserving the VT source."""
        image = Image()
        presenter = GraphicsPresenter(first_id=201)
        presenter.plan(Frame(), lambda _: image)
        larger = Frame(images=(replace(Placement(), width=8, height=8),))
        plan = presenter.plan(larger, lambda _: image)
        self.assertIn("d=I,i=201", plan.before_text)
        metadata, pixels = uploads(plan.before_text)[0]
        self.assertEqual((metadata["s"], metadata["v"]), ("8", "8"))
        self.assertEqual(pixels, bytes((255, 0, 0, 128)) * 64)
        self.assertEqual(image.rgba, bytes((255, 0, 0, 128)) * 16)

    def test_hidden_cached_asset_can_be_evicted_without_native_deletion(self):
        """Derived cache quota is enforced even when a popup covers old content."""
        presenter = GraphicsPresenter(first_id=211, max_bytes=64)
        presenter.plan(Frame(), lambda _: Image())
        second = replace(Placement(), id=10, image_id=8, x=4)
        plan = presenter.plan(
            Frame(images=(Placement(), second)),
            lambda image_id: Image(id=image_id),
            occluders=(Rect(0, 0, 4, 4),),
        )
        self.assertIn("d=I,i=211", plan.before_text)
        self.assertEqual(uploads(plan.before_text)[0][0]["i"], "212")
        self.assertEqual([p["i"] for p, _ in packets(plan.after_text)], ["212"])

    def test_huge_magnification_only_rasterizes_its_visible_crop(self):
        """Offscreen magnification cannot force a huge derived pixel allocation."""
        image = Image(width=2, height=1, rgba=bytes((255, 0, 0, 255, 0, 0, 255, 64)))
        placement = replace(
            Placement(),
            x=-50_000,
            y=-25_000,
            width=100_000,
            height=50_000,
            source_width=2,
            source_height=1,
        )
        presenter = GraphicsPresenter(first_id=221, max_bytes=256)
        plan = presenter.plan(
            Frame(images=(placement,), cols=4, rows=4), lambda _: image
        )
        metadata, pixels = uploads(plan.before_text)[0]
        self.assertEqual((metadata["s"], metadata["v"]), ("8", "8"))
        self.assertEqual(pixels, bytes((0, 0, 255, 64)) * 64)
        p = packets(plan.after_text)[0][0]
        self.assertEqual(
            {key: p[key] for key in ("x", "y", "w", "h")},
            {"x": "0", "y": "0", "w": "8", "h": "8"},
        )

    def test_policy_limit_can_clear_and_recover_without_hiding_invalid_data(self):
        """Capacity failures are distinct from broken snapshots and recover cleanly."""
        presenter = GraphicsPresenter(first_id=231, max_bytes=64)
        presenter.plan(Frame(), lambda _: Image())
        larger = Frame(images=(replace(Placement(), width=8, height=8),))
        with self.assertRaises(GraphicsLimitError):
            presenter.plan(larger, lambda _: Image())
        self.assertIn("d=I,i=231", presenter.close())
        recovered = presenter.plan(Frame(), lambda _: Image())
        self.assertEqual(uploads(recovered.before_text)[0][0]["i"], "232")
        changed = Frame(images=(replace(Placement(), image_generation=2),))
        with self.assertRaises(GraphicsError) as caught:
            presenter.plan(changed, lambda _: Image(generation=2, rgba=b"broken"))
        self.assertNotIsInstance(caught.exception, GraphicsLimitError)


@unittest.skipUnless(os.name == "posix", "Native VT rendering requires POSIX")
class GraphicsRendererTests(unittest.IsolatedAsyncioTestCase):
    """Verify failure policy through actual native snapshots and PTK output."""

    async def asyncSetUp(self):
        """Negotiate a real graphics profile and capture PTK's outer wire."""
        from ish.terminal.app import TerminalApp

        self.pipe_context = create_pipe_input()
        self.pipe = self.pipe_context.__enter__()
        self.stream = io.StringIO()
        self.outer_size = Size(rows=6, columns=12)
        self.output = Vt100_Output(
            self.stream,
            lambda: self.outer_size,
            term="xterm-256color",
            enable_cpr=False,
        )
        with create_app_session(input=self.pipe, output=self.output):
            with patch("ish.terminal.app.create_input", return_value=self.pipe):
                self.owner = TerminalApp("/bin/sh", Path(__file__).resolve().parents[1])
        self.render()
        self.owner.host.begin_probe(12, 6, query_id=2**31 + 17)
        for reply in (
            "\x1b[6;2;2t",
            "\x1bP>|kitty(0.50.0)\x1b\\",
            f"\x1b_Gi={2**31 + 17};OK\x1b\\",
        ):
            self.assertTrue(self.owner.host.control(reply))
        self.owner.host.finish_probe()
        self.assertTrue(self.owner.profile.kitty_graphics)
        self.owner.vt.resize(12, 6, 2, 2)
        self.owner.vt.set_graphics(True)
        self.renderer = self.owner.app.renderer
        self.renderer.graphics = GraphicsPresenter(first_id=301, max_bytes=64)

    async def asyncTearDown(self):
        """Close only this test's application resources."""
        self.renderer.reset()
        await self.owner.plugins.close()
        await self.owner.session.close()
        self.owner.vt.close()
        self.pipe_context.__exit__(None, None, None)

    def render(self):
        """Invoke the real PTK layout, composition and diff renderer."""
        before = self.stream.tell()
        with set_app(self.owner.app):
            self.owner.app.renderer.render(self.owner.app, self.owner.app.layout)
        return self.stream.getvalue()[before:]

    def show_image(self, *, color=(255, 0, 0, 255), cells=2, z=1):
        """Feed independently serialized child graphics into libghostty-vt."""
        encoded = base64.b64encode(bytes(color) * 16).decode("ascii")
        self.owner.vt.feed(
            (
                "\x1b[1;1H"
                f"\x1b_Ga=T,i=7,p=9,f=32,s=4,v=4,c={cells},r={cells},"
                f"C=1,z={z},q=2;{encoded}\x1b\\"
            ).encode("ascii")
        )

    def write_label(self, text):
        """Change shell text independently of its image generation."""
        self.owner.vt.feed(f"\x1b[6;1H\x1b[2K{text}".encode("ascii"))

    def screen(self):
        """Read final displayed text using an independent VT interpreter."""
        import pyte

        screen = pyte.Screen(self.outer_size.columns, self.outer_size.rows)
        text_wire = re.sub("\x1b_G.*?\x1b\\\\", "", self.stream.getvalue(), flags=re.S)
        # Pyte does not implement colon SGR parameters; text expectations do not
        # depend on underline styles. The composition suite checks those bytes.
        text_wire = re.sub("\x1b\\[[0-9;:]*:[0-9;:]*m", "", text_wire)
        pyte.Stream(screen).feed(text_wire)
        return screen

    def reject(self, image_id, message="ENOENT:missing image"):
        """Deliver an actual host reply through the production broker."""
        self.assertTrue(self.owner.host.control(f"\x1b_Gi={image_id};{message}\x1b\\"))

    def assert_no_graphics(self, wire):
        """Neither uploads nor placements may escape a failed presentation."""
        self.assertFalse(uploads(wire))
        self.assertFalse(
            [control for control, _ in packets(wire) if control.get("a") == "p"]
        )

    async def test_quota_clears_owned_images_preserves_text_and_recovers(self):
        """A bounded resource failure keeps the terminal usable until content changes."""
        self.show_image()
        self.assertEqual(uploads(self.render())[0][0]["i"], "301")
        self.show_image(cells=4)
        self.write_label("QUOTA")
        failed = self.render()
        self.assert_no_graphics(failed)
        self.assertEqual(
            [(control["d"], control["i"]) for control, _ in packets(failed)],
            [("I", "301")],
        )
        self.assertTrue(self.renderer.graphics_error)
        self.assertEqual(self.screen().display[5].strip(), "QUOTA")
        self.write_label("TEXT LIVES")
        with patch.object(
            self.renderer.graphics, "plan", wraps=self.renderer.graphics.plan
        ) as plan:
            self.assert_no_graphics(self.render())
            self.assert_no_graphics(self.render())
            plan.assert_not_called()
        self.assertEqual(self.screen().display[5].strip(), "TEXT LIVES")
        self.show_image(color=(0, 0, 255, 255))
        restored = uploads(self.render())
        self.assertEqual(restored[0][0]["i"], "302")
        self.assertEqual(restored[0][1], bytes((0, 0, 255, 255)) * 16)
        self.assertIsNone(self.renderer.graphics_error)

    async def test_broken_native_pixels_propagate_instead_of_becoming_quota(self):
        """Structural native failures remain visible at the renderer boundary."""
        self.show_image()
        malformed = replace(self.owner.vt.image(7), rgba=b"broken")
        with patch.object(self.owner.vt, "image", return_value=malformed):
            with self.assertRaises(GraphicsError) as caught:
                self.render()
        self.assertNotIsInstance(caught.exception, GraphicsLimitError)
        self.assertIsNone(self.renderer.graphics_error)
        self.assertFalse(self.renderer.transaction.active)
        self.assertEqual(uploads(self.render())[0][1], bytes((255, 0, 0, 255)) * 16)

    async def test_host_failure_ignores_unknown_duplicates_and_unchanged_content(self):
        """Host errors cannot trigger a repaint loop or consume retry budget twice."""
        self.show_image()
        self.assertEqual(uploads(self.render())[0][0]["i"], "301")
        self.reject(9001)
        self.assertIsNone(self.renderer.graphics_error)
        self.assertTrue([c for c, _ in packets(self.render()) if c.get("a") == "p"])
        message = "EINVAL:" + "x" * 400
        self.reject(301, message)
        self.assertEqual(self.renderer.graphics_error, message[:256])
        self.reject(301)
        self.write_label("HOST FAILED")
        failed = self.render()
        self.assert_no_graphics(failed)
        self.assertIn("d=I,i=301", failed)
        with patch.object(
            self.renderer.graphics, "plan", wraps=self.renderer.graphics.plan
        ) as plan:
            self.assert_no_graphics(self.render())
            self.assert_no_graphics(self.render())
            popup = self.owner.plugins.show_popup(
                TextArea(text="UI"), width=4, height=2, left=1, top=1
            )
            self.assert_no_graphics(self.render())
            self.owner.plugins.close_popup(popup)
            self.assert_no_graphics(self.render())
            plan.assert_not_called()
        self.assertEqual(self.screen().display[5].strip(), "HOST FAILED")
        self.show_image(color=(0, 0, 255, 255))
        self.assertEqual(uploads(self.render())[0][0]["i"], "302")
        self.reject(301)  # Delayed duplicate from the retired generation.
        self.assertIsNone(self.renderer.graphics_error)
        self.reject(302)
        self.assert_no_graphics(self.render())
        self.show_image(color=(0, 255, 0, 255))
        self.assertEqual(uploads(self.render())[0][0]["i"], "303")
        self.reject(303)
        self.assert_no_graphics(self.render())
        self.show_image(color=(255, 255, 0, 255))
        self.write_label("STILL ALIVE")
        self.assert_no_graphics(self.render())
        self.assertEqual(self.screen().display[5].strip(), "STILL ALIVE")

    async def test_real_geometry_change_allows_one_host_retry(self):
        """A changed terminal size can retry unchanged source pixels exactly once."""
        self.show_image()
        self.assertEqual(uploads(self.render())[0][0]["i"], "301")
        self.reject(301)
        self.assert_no_graphics(self.render())
        self.outer_size = Size(rows=6, columns=14)
        with patch.object(self.owner, "_geometry", return_value=(14, 6, 28, 12)):
            recovered = uploads(self.render())
            self.assertEqual(recovered[0][0]["i"], "302")
            self.assertEqual(recovered[0][1], bytes((255, 0, 0, 255)) * 16)
            self.reject(302)
            self.assert_no_graphics(self.render())
            self.assert_no_graphics(self.render())

    async def test_host_output_change_retires_owned_assets_before_new_backend(self):
        """Switching verified output protocols leaves neither old IDs nor old wire."""
        from ish.terminal.host import HostBroker

        self.show_image()
        self.assertEqual(uploads(self.render())[0][0]["i"], "301")
        broker = HostBroker()
        broker.begin_probe(12, 6, query_id=2**31 + 18)
        broker.control("\x1b[6;2;2t")
        broker.control("\x1bP>|WezTerm 20240203-110809-5046fc22\x1b\\")
        broker.finish_probe()
        self.owner.host = broker
        switched = self.render()
        retired = "\x1b_Ga=d,d=I,i=301,q=2;\x1b\\"
        self.assertIn(retired, switched)
        self.assertIn("\x1b]1337;File=", switched)
        self.assertLess(switched.index(retired), switched.index("\x1b]1337;File="))
        self.assertFalse(self.renderer.reject_image(301, "delayed previous backend"))
        self.owner.host = HostBroker()
        disabled = self.render()
        self.assertIn("\x1b[2X", disabled)
        self.assertNotIn("\x1b]1337;File=", disabled)
        self.assert_no_graphics(disabled)
        self.assertIsNone(self.renderer.graphics_error)
        self.assert_no_graphics(self.render())

    async def test_legacy_below_text_block_recovers_when_only_text_changes(self):
        """Removing overlapping glyphs must release a capability-limited image scene."""
        from ish.terminal.host import HostBroker

        broker = HostBroker()
        broker.begin_probe(12, 6, query_id=2**31 + 18)
        broker.control("\x1b[6;2;2t")
        broker.control("\x1bP>|WezTerm 20240203-110809-5046fc22\x1b\\")
        broker.finish_probe()
        self.owner.host = broker
        self.show_image(z=-1)
        self.assertIn("\x1b]1337;File=", self.render())
        original = self.owner.control.frame
        self.owner.vt.feed(b"\x1b[1;1HX")
        self.assertNotIn("\x1b]1337;File=", self.render())
        self.assertIn("below-text", self.renderer.graphics_error)
        self.owner.vt.feed(b"\x1b[1;1H ")
        restored = self.render()
        current = self.owner.control.frame
        self.assertEqual(current.images, original.images)
        self.assertEqual(current.image_generation, original.image_generation)
        self.assertIn("\x1b]1337;File=", restored)
        self.assertIsNone(self.renderer.graphics_error)

    async def test_overline_only_blocks_raster_then_recovers_without_image_change(self):
        """Real native overline survives instead of being covered by an outer PNG."""
        from ish.terminal.host import HostBroker

        broker = HostBroker()
        broker.begin_probe(12, 6, query_id=2**31 + 18)
        broker.control("\x1b[6;2;2t")
        broker.control("\x1bP>|WezTerm 20240203-110809-5046fc22\x1b\\")
        broker.finish_probe()
        self.owner.host = broker
        self.show_image(z=-1)
        self.assertIn("\x1b]1337;File=", self.render())
        original = self.owner.control.frame
        self.owner.vt.feed(b"\x1b[1;1H\x1b[53m \x1b[0m")
        blocked = self.render()
        self.assertNotIn("\x1b]1337;File=", blocked)
        self.assertIn("\x1b[53m ", blocked)
        self.assertIn("below-text", self.renderer.graphics_error)
        self.assertNotIn("\x1b]1337;File=", self.render())
        self.owner.vt.feed(b"\x1b[1;1H ")
        restored = self.render()
        current = self.owner.control.frame
        self.assertEqual(current.images, original.images)
        self.assertEqual(current.image_generation, original.image_generation)
        self.assertIn("\x1b]1337;File=", restored)
        self.assertIsNone(self.renderer.graphics_error)


if __name__ == "__main__":
    unittest.main()
