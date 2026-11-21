"""Compose native image placements for the sole outer terminal output owner.

This module never writes to a terminal.  The PTK renderer applies both halves of
each plan in its own output transaction.  Native assets remain owned by the VT;
only derived, bounded presentation assets are cached in the outer terminal.
"""

from __future__ import annotations

import base64
import secrets
import zlib
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ish.terminal.vt import Frame, Image, Placement

MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGES = 1024
MAX_FRAGMENTS = 4096
MAX_OCCLUDERS = 128
PAYLOAD_SIZE = 4096


class GraphicsError(ValueError):
    """An image snapshot cannot be presented within the configured bounds."""


class GraphicsLimitError(GraphicsError):
    """A valid scene exceeds a bounded presentation resource policy."""


class GraphicsCapabilityError(GraphicsError):
    """A valid scene needs an image semantic unavailable in the selected host."""


@dataclass(frozen=True)
class Rect:
    """A half-open rectangle in outer-terminal pixels."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        """Return the exclusive right edge."""
        return self.x + self.width

    @property
    def bottom(self) -> int:
        """Return the exclusive bottom edge."""
        return self.y + self.height

    def intersect(self, other: Rect) -> Rect | None:
        """Return the overlap, excluding empty rectangles."""
        left, top = max(self.x, other.x), max(self.y, other.y)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right - left, bottom - top)

    def subtract(self, other: Rect) -> tuple[Rect, ...]:
        """Return nonoverlapping pieces outside an opaque rectangle."""
        overlap = self.intersect(other)
        if overlap is None:
            return (self,)
        pieces = (
            Rect(self.x, self.y, self.width, overlap.y - self.y),
            Rect(self.x, overlap.bottom, self.width, self.bottom - overlap.bottom),
            Rect(self.x, overlap.y, overlap.x - self.x, overlap.height),
            Rect(overlap.right, overlap.y, self.right - overlap.right, overlap.height),
        )
        return tuple(piece for piece in pieces if piece.width > 0 and piece.height > 0)


@dataclass(frozen=True)
class GraphicsPlan:
    """Commands to apply before and after PTK's text diff, in one transaction."""

    before_text: str = ""
    after_text: str = ""
    repaint: bool = False


class Presenter(Protocol):
    """Translate a native scene into owned host output, without writing it.

    Input protocol support belongs to the VT engine, independently of this
    output contract. Replace a backend only when its replacement preserves
    clipping, layering, popup occlusion and cleanup for the same verified host.
    """

    protocol: str | None

    def scene_key(self, frame, *, origin=(0, 0), clip=None, occluders=()) -> tuple:
        """Identify every scene input that affects this backend's acceptance."""
        ...

    def plan(
        self, frame, image_lookup, *, origin=(0, 0), clip=None, occluders=()
    ) -> GraphicsPlan:
        """Prepare output without changing the native assets or outer terminal."""
        ...

    def close(self) -> str:
        """Retire only owned output resources and forget the previous scene."""
        ...

    def invalidate(self, image_id: int) -> bool:
        """Accept a host error only for a currently owned resource identity."""
        ...


def _image_scene(frame, origin, clip, occluders):
    return (
        frame.images,
        frame.cols,
        frame.rows,
        frame.cell_width,
        frame.cell_height,
        origin,
        clip,
        tuple(occluders),
    )


def _viewport(frame, origin, clip):
    bounds = Rect(
        *origin, frame.cols * frame.cell_width, frame.rows * frame.cell_height
    )
    if clip is not None:
        bounds = bounds.intersect(clip)
    if bounds is None:
        return None
    return bounds.intersect(Rect(0, 0, max(0, bounds.right), max(0, bounds.bottom)))


class DisabledPresenter:
    """Keep unselected output inert without reading native image assets."""

    protocol = None

    def scene_key(self, frame, *, origin=(0, 0), clip=None, occluders=()) -> tuple:
        """No scene can bypass the broker's output selection."""
        return ()

    def plan(self, frame, image_lookup, *, origin=(0, 0), clip=None, occluders=()):
        """Return an empty transaction for every native scene."""
        return GraphicsPlan()

    def close(self) -> str:
        """No output resources were acquired."""
        return ""

    def invalidate(self, image_id: int) -> bool:
        """No host identity belongs to disabled output."""
        return False


@dataclass(frozen=True)
class _Asset:
    wire_id: int
    size: int
    used: int


@dataclass(frozen=True)
class _Fragment:
    image_id: int
    placement_id: int
    rect: Rect
    source_x: int
    source_y: int
    z: int


@dataclass(frozen=True)
class _Raster:
    placement: Placement
    local: Rect

    @property
    def size(self) -> int:
        return self.local.width * self.local.height * 4


def _command(payload: str = "", **values: int | str) -> str:
    controls = ",".join(f"{key}={value}" for key, value in values.items())
    return f"\x1b_G{controls};{payload}\x1b\\"


def _delete(image_id: int, placement_id: int | None = None) -> str:
    values: dict[str, int | str] = {"a": "d", "d": "I", "i": image_id, "q": 2}
    if placement_id is not None:
        values.update(d="i", p=placement_id)
    return _command(**values)


def _upload(image_id: int, width: int, height: int, rgba: bytes) -> str:
    encoded = base64.b64encode(zlib.compress(rgba)).decode("ascii")
    chunks = [
        encoded[offset : offset + PAYLOAD_SIZE]
        for offset in range(0, len(encoded), PAYLOAD_SIZE)
    ]
    output = []
    for index, payload in enumerate(chunks):
        values: dict[str, int | str] = {"m": int(index + 1 < len(chunks)), "q": 1}
        if index == 0:
            values.update(a="t", f=32, s=width, v=height, i=image_id, o="z")
        output.append(_command(payload, **values))
    return "".join(output)


def _pixels(image: Image, placement: Placement, raster: Rect) -> bytes:
    """Crop and nearest-neighbor scale once, preserving unassociated RGBA bytes."""
    sx, sy = placement.source_x, placement.source_y
    sw, sh = placement.source_width, placement.source_height
    width, height = raster.width, raster.height
    if (
        image.width <= 0
        or image.height <= 0
        or len(image.rgba) != image.width * image.height * 4
        or sx < 0
        or sy < 0
        or sw <= 0
        or sh <= 0
        or sx + sw > image.width
        or sy + sh > image.height
    ):
        raise GraphicsError("Invalid native image pixels or source rectangle")
    if (
        sx == sy == raster.x == raster.y == 0
        and sw == image.width == width == placement.width
        and sh == image.height == height == placement.height
    ):
        return image.rgba
    rows = bytearray(width * height * 4)
    source = memoryview(image.rgba)
    x_offsets = tuple(
        (sx + (raster.x + x) * sw // placement.width) * 4 for x in range(width)
    )
    previous_y = -1
    previous_row = b""
    for y in range(height):
        source_y = sy + (raster.y + y) * sh // placement.height
        if source_y != previous_y:
            start = source_y * image.width * 4
            if sw == placement.width:
                left = sx + raster.x
                previous_row = bytes(
                    source[start + left * 4 : start + (left + width) * 4]
                )
            else:
                previous_row = b"".join(
                    source[start + x : start + x + 4] for x in x_offsets
                )
            previous_y = source_y
        rows[y * width * 4 : (y + 1) * width * 4] = previous_row
    return bytes(rows)


class GraphicsPresenter:
    """Cache outer image resources and compose clipped native placements."""

    # Host capability probes reserve the upper half of the Kitty ID space.
    MAX_WIRE_ID = (1 << 31) - 1
    LAYER_Z_BASES = (-(1 << 31), -(1 << 30), 0)

    protocol = "kitty"

    def __init__(
        self,
        *,
        max_bytes: int = MAX_CACHE_BYTES,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        first_id: int | None = None,
    ) -> None:
        """Create a bounded presentation namespace, independent of native IDs."""
        if max_bytes <= 0 or max_image_bytes <= 0:
            raise GraphicsError("Graphics limits must be positive")
        self.max_bytes = max_bytes
        self.max_image_bytes = min(max_bytes, max_image_bytes)
        self._next_id = (
            first_id if first_id is not None else secrets.randbelow(1 << 29) + (1 << 29)
        )
        if not 0 < self._next_id <= self.MAX_WIRE_ID:
            raise GraphicsError("Invalid outer image namespace")
        self._assets: dict[tuple[int, ...], _Asset] = {}
        self._fragments: dict[tuple, _Fragment] = {}
        self._retired: set[int] = set()
        self._generation = 0

    def scene_key(self, frame, *, origin=(0, 0), clip=None, occluders=()) -> tuple:
        """Kitty layers preserve text independently of the visible cell content."""
        return _image_scene(frame, origin, clip, occluders)

    def invalidate(self, image_id: int) -> bool:
        """Forget an owned image after a routed outer-terminal error reply."""
        keys = [key for key, asset in self._assets.items() if asset.wire_id == image_id]
        for key in keys:
            del self._assets[key]
        if keys:
            self._retired.add(image_id)
        return bool(keys)

    def close(self) -> str:
        """Release only resources owned by this presenter, without host-wide clears."""
        ids = self._retired | {asset.wire_id for asset in self._assets.values()}
        self._assets.clear()
        self._fragments.clear()
        self._retired.clear()
        return "".join(_delete(image_id) for image_id in sorted(ids))

    def plan(
        self,
        frame: Frame,
        image_lookup: Callable[[int], Image | None],
        *,
        origin: tuple[int, int] = (0, 0),
        clip: Rect | None = None,
        occluders: Sequence[Rect] = (),
    ) -> GraphicsPlan:
        """Plan image changes without writing, consuming keys, or changing VT state.

        ``origin``, ``clip`` and opaque ``occluders`` are outer pixel coordinates.
        Placements are viewport-local pixels.  Hidden assets are retained while
        their native placement exists; removed native placements release assets.
        A failed plan leaves the previous presentation intact.
        """
        cw, ch = frame.cell_width, frame.cell_height
        if cw <= 0 or ch <= 0:
            if not frame.images:
                return GraphicsPlan(self.close())
            raise GraphicsError("Image presentation requires negotiated cell pixels")
        if len(occluders) > MAX_OCCLUDERS or len(frame.images) > MAX_IMAGES:
            raise GraphicsLimitError("Too many image placements or popup rectangles")
        viewport = _viewport(frame, origin, clip)

        assets = dict(self._assets)
        retired = set(self._retired)
        fragments: dict[tuple, _Fragment] = {}
        next_id = self._next_id
        generation = self._generation + 1
        needed: set[tuple[int, ...]] = set()
        visible: dict[tuple[int, ...], _Raster] = {}
        pieces: list[tuple[tuple[int, ...], Placement, Rect, Rect, int]] = []
        ordered = sorted(frame.images, key=lambda p: (p.layer, p.z, p.image_id, p.id))
        for rank, placement in enumerate(ordered):
            if placement.layer not in (0, 1, 2):
                raise GraphicsError("Invalid native image layer")
            if placement.width <= 0 or placement.height <= 0:
                continue
            bounds = Rect(
                origin[0] + placement.x,
                origin[1] + placement.y,
                placement.width,
                placement.height,
            )
            inside = bounds.intersect(viewport) if viewport is not None else None
            if inside is None:
                continue
            # Large magnifications need only the visible pixels.  Cropping
            # before rasterization avoids allocating the offscreen destination.
            raster_bounds = (
                bounds
                if placement.width * placement.height * 4 <= self.max_image_bytes
                else inside
            )
            raster = _Raster(
                placement,
                Rect(
                    raster_bounds.x - bounds.x,
                    raster_bounds.y - bounds.y,
                    raster_bounds.width,
                    raster_bounds.height,
                ),
            )
            if raster.size > self.max_image_bytes:
                raise GraphicsLimitError("Visible image exceeds the presentation quota")
            key = (
                placement.image_id,
                placement.image_generation,
                placement.source_x,
                placement.source_y,
                placement.source_width,
                placement.source_height,
                placement.width,
                placement.height,
                raster.local.x,
                raster.local.y,
                raster.local.width,
                raster.local.height,
            )
            needed.add(key)
            uncovered = (inside,)
            for occluder in occluders:
                uncovered = tuple(
                    part for rect in uncovered for part in rect.subtract(occluder)
                )
                if len(uncovered) + len(pieces) > MAX_FRAGMENTS:
                    raise GraphicsLimitError(
                        "Image clipping exceeds the fragment limit"
                    )
            if uncovered:
                visible[key] = raster
                z = self.LAYER_Z_BASES[placement.layer] + rank
                pieces.extend(
                    (key, placement, raster_bounds, rect, z) for rect in uncovered
                )

        # Native deletion and leaving the viewport release derived outer assets.
        for key in tuple(assets):
            if key not in needed:
                retired.add(assets.pop(key).wire_id)
        visible_bytes = sum(raster.size for raster in visible.values())
        if visible_bytes > self.max_bytes:
            raise GraphicsLimitError("Visible images exceed the presentation quota")
        total = sum(asset.size for asset in assets.values())
        missing_bytes = sum(
            raster.size for key, raster in visible.items() if key not in assets
        )
        for key in sorted(assets, key=lambda item: assets[item].used):
            if (
                total + missing_bytes <= self.max_bytes
                and len(assets) + sum(key not in assets for key in visible)
                <= MAX_IMAGES
            ):
                break
            if key not in visible:
                asset = assets.pop(key)
                total -= asset.size
                retired.add(asset.wire_id)

        uploads: list[str] = []
        for key, raster in visible.items():
            placement = raster.placement
            asset = assets.get(key)
            if asset is None:
                image = image_lookup(placement.image_id)
                if (
                    image is None
                    or image.id != placement.image_id
                    or image.generation != placement.image_generation
                ):
                    raise GraphicsError(
                        "Native image changed during the frame snapshot"
                    )
                rgba = _pixels(image, placement, raster.local)
                if next_id > self.MAX_WIRE_ID:
                    raise GraphicsLimitError("Outer image namespace exhausted")
                asset = _Asset(next_id, len(rgba), generation)
                next_id += 1
                uploads.append(
                    _upload(
                        asset.wire_id, raster.local.width, raster.local.height, rgba
                    )
                )
            assets[key] = _Asset(asset.wire_id, asset.size, generation)

        placement_id = 1
        commands: list[str] = []
        for key, placement, bounds, rect, z in pieces:
            asset = assets[key]
            fragment_key = (key, placement.id, rect, z)
            previous = self._fragments.get(fragment_key)
            if previous is not None and previous.image_id == asset.wire_id:
                wire_placement = previous.placement_id
            else:
                # IDs are per image; avoid collisions with retained old fragments.
                old_ids = {
                    fragment.placement_id
                    for fragment in self._fragments.values()
                    if fragment.image_id == asset.wire_id
                }
                while placement_id in old_ids:
                    placement_id += 1
                wire_placement = placement_id
                placement_id += 1
            fragment = _Fragment(
                asset.wire_id,
                wire_placement,
                rect,
                rect.x - bounds.x,
                rect.y - bounds.y,
                z,
            )
            fragments[fragment_key] = fragment
            commands.append(f"\x1b[{rect.y // ch + 1};{rect.x // cw + 1}H")
            commands.append(
                _command(
                    a="p",
                    i=asset.wire_id,
                    p=wire_placement,
                    x=fragment.source_x,
                    y=fragment.source_y,
                    w=rect.width,
                    h=rect.height,
                    X=rect.x % cw,
                    Y=rect.y % ch,
                    z=z,
                    C=1,
                    q=1,
                )
            )
        deletions = [_delete(image_id) for image_id in sorted(retired)]
        for key, fragment in self._fragments.items():
            if key not in fragments and fragment.image_id not in retired:
                deletions.append(_delete(fragment.image_id, fragment.placement_id))
        self._assets, self._fragments = assets, fragments
        self._retired.clear()
        self._generation = generation
        self._next_id = next_id
        after = "\x1b7" + "".join(commands) + "\x1b8" if commands else ""
        return GraphicsPlan("".join(deletions + uploads), after)


class RasterPresenter:
    """Compose one bounded raster for selected output without image resource IDs.

    These protocols need pixel retransmission after text rendering. Encoded data
    is cached until the image scene changes; ECH removes old coverage and asks
    PTK to repaint its text. Source assets still belong exclusively to the VT.
    Native parsing of OSC 1337 does not replace this outer-host presentation.
    """

    def __init__(
        self,
        protocol: str,
        *,
        max_bytes=MAX_CACHE_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
        background=None,
    ):
        """Select a verified wire protocol and keep all derived storage bounded."""
        if protocol not in ("iterm", "sixel"):
            raise GraphicsError("Unknown raster image protocol")
        if max_bytes <= 0 or max_image_bytes <= 0:
            raise GraphicsError("Graphics limits must be positive")
        self.protocol = protocol
        self.max_bytes = max_bytes
        self.max_image_bytes = min(max_bytes, max_image_bytes)
        self.background = background
        self._scene = None
        self._coverage = None
        self._wire = ""

    def scene_key(self, frame, *, origin=(0, 0), clip=None, occluders=()) -> tuple:
        """Only cells under a placement can affect its acceptance or alpha blend."""
        geometry = _image_scene(frame, origin, clip, occluders)
        cells = getattr(frame, "cells", ())
        if not cells or not frame.images:
            return geometry, self.background, ()
        if len(frame.images) > MAX_IMAGES or len(cells) != frame.rows:
            return geometry, self.background, cells
        viewport = _viewport(frame, origin, clip)
        regions = set()
        for p in frame.images:
            if viewport is None or (self.protocol != "sixel" and p.layer != 1):
                continue
            bounds = Rect(origin[0] + p.x, origin[1] + p.y, p.width, p.height)
            visible = viewport.intersect(bounds)
            if visible is not None:
                cw, ch = frame.cell_width, frame.cell_height
                regions.add(
                    (
                        (visible.x - origin[0]) // cw,
                        (visible.right - origin[0] + cw - 1) // cw,
                        (visible.y - origin[1]) // ch,
                        (visible.bottom - origin[1] + ch - 1) // ch,
                    )
                )
        # Never retain more cell references than the viewport, even when many
        # placements overlap. A dense scene can reuse the native row tuples.
        area = sum(
            (right - left) * (bottom - top) for left, right, top, bottom in regions
        )
        if area >= frame.cols * frame.rows:
            return geometry, self.background, cells
        spans = []
        for left, right, top, bottom in sorted(regions):
            for row in cells[top:bottom]:
                # Native rows omit wide tails. Select every grapheme whose
                # half-open cell interval overlaps this placement.
                start = bisect_right(row, left, key=lambda cell: cell.x + cell.width)
                end = bisect_left(row, right, lo=start, key=lambda cell: cell.x)
                spans.append(row[start:end])
        return geometry, self.background, tuple(spans)

    def _erase(self, coverage):
        if coverage is None:
            return ""
        if self.protocol == "sixel":
            # ECH need not erase SIXEL pixels (Zellij demonstrates this). The
            # Application owns this alternate screen; ED2 plus repaint retires
            # image pixels and reconstructs all native text/popup contents.
            return "\x1b7\x1b[0m\x1b[2J\x1b8"
        return (
            "\x1b7\x1b[0m"
            + "".join(
                f"\x1b[{row + 1};{coverage.x + 1}H\x1b[{coverage.width}X"
                for row in range(coverage.y, coverage.bottom)
            )
            + "\x1b8"
        )

    def close(self) -> str:
        """Erase only the cells occupied by this presenter's last image canvas."""
        wire = self._erase(self._coverage)
        self._scene = self._coverage = None
        self._wire = ""
        return wire

    def invalidate(self, image_id: int) -> bool:
        """Legacy protocols never own Kitty IDs or consume their error replies."""
        return False

    @staticmethod
    def _blank_cell(cell):
        return (
            cell.text in ("", " ")
            and not cell.underline
            and not cell.overline
            and "strike" not in cell.style.split()
        )

    @staticmethod
    def _blank(frame, pieces, origin):
        cells = getattr(frame, "cells", ())
        if len(cells) != frame.rows:
            return False
        cw, ch = frame.cell_width, frame.cell_height
        for piece in pieces:
            left = (piece.x - origin[0]) // cw
            right = (piece.right - origin[0] + cw - 1) // cw
            top = (piece.y - origin[1]) // ch
            bottom = (piece.bottom - origin[1] + ch - 1) // ch
            for row in range(top, bottom):
                covered = 0
                for cell in cells[row]:
                    width = min(right, cell.x + cell.width) - max(left, cell.x)
                    if width <= 0:
                        continue
                    if not RasterPresenter._blank_cell(cell):
                        return False
                    covered += width
                if covered != right - left:
                    return False
        return True

    def _flatten_alpha(self, canvas, frame, origin):
        """Resolve partial alpha only over independently known blank cell colors.

        Text glyph pixels belong to the host font renderer. We cannot infer
        them from a codepoint, or replace unknown defaults with invented black.
        Transparent holes remain holes so popup/text restoration is preserved.
        """
        from PIL import Image as Pixels

        alpha = canvas.getchannel("A")
        partial = alpha.point(lambda value: 255 if 0 < value < 255 else 0)
        if partial.getbbox() is None:
            return
        cw, ch = frame.cell_width, frame.cell_height
        for row, cells in enumerate(frame.cells):
            for cell in cells:
                bounds = Rect(
                    origin[0] + cell.x * cw,
                    origin[1] + row * ch,
                    cell.width * cw,
                    ch,
                ).intersect(Rect(0, 0, canvas.width, canvas.height))
                if bounds is None:
                    continue
                box = (bounds.x, bounds.y, bounds.right, bounds.bottom)
                mask = partial.crop(box)
                if mask.getbbox() is None:
                    continue
                background = getattr(cell, "bg", None) or self.background
                if (
                    background is None
                    or not self._blank_cell(cell)
                    or "reverse" in cell.style.split()
                ):
                    raise GraphicsCapabilityError(
                        "SIXEL partial alpha needs a known blank cell background"
                    )
                backdrop = Pixels.new(
                    "RGBA", (bounds.width, bounds.height), (*background, 255)
                )
                backdrop.alpha_composite(canvas.crop(box))
                canvas.paste(backdrop, (bounds.x, bounds.y), mask)
                partial.paste(0, box)
        if partial.getbbox() is not None:
            raise GraphicsCapabilityError(
                "SIXEL partial alpha extends outside known native cells"
            )

    def plan(self, frame, image_lookup, *, origin=(0, 0), clip=None, occluders=()):
        """Build an alpha-composited viewport without changing any native asset."""
        from PIL import Image as Pixels
        from PIL import ImageDraw

        from . import raster

        if len(frame.images) > MAX_IMAGES or len(occluders) > MAX_OCCLUDERS:
            raise GraphicsLimitError("Too many image placements or popup rectangles")
        cw, ch = frame.cell_width, frame.cell_height
        if cw <= 0 or ch <= 0:
            if not frame.images:
                cleared = self.close()
                return GraphicsPlan(cleared, repaint=bool(cleared))
            raise GraphicsError("Image presentation requires negotiated cell pixels")
        scene = self.scene_key(frame, origin=origin, clip=clip, occluders=occluders)
        if scene == self._scene:
            return GraphicsPlan(after_text=self._wire)
        viewport = _viewport(frame, origin, clip)
        visible = []
        for placement in sorted(
            frame.images, key=lambda p: (p.layer, p.z, p.image_id, p.id)
        ):
            if placement.layer not in (0, 1, 2):
                raise GraphicsError("Invalid native image layer")
            bounds = Rect(
                origin[0] + placement.x,
                origin[1] + placement.y,
                placement.width,
                placement.height,
            )
            overlap = bounds.intersect(viewport) if viewport is not None else None
            if overlap is None:
                continue
            pieces = (overlap,)
            for occluder in occluders:
                pieces = tuple(
                    part for piece in pieces for part in piece.subtract(occluder)
                )
                if len(pieces) > MAX_FRAGMENTS:
                    raise GraphicsLimitError("Too many visible image fragments")
            if not pieces:
                continue
            if placement.layer == 0 or (
                placement.layer == 1 and not self._blank(frame, pieces, origin)
            ):
                raise GraphicsCapabilityError(
                    f"{self.protocol} output cannot preserve below-text image layers"
                )
            visible.append((placement, bounds, overlap))
        if not visible:
            cleared = self.close()
            self._scene = scene
            return GraphicsPlan(cleared, repaint=bool(cleared))
        left = min(item[2].x for item in visible) // cw * cw
        top = min(item[2].y for item in visible) // ch * ch
        if self.protocol == "sixel":
            # DECSDM is acquired by HostBroker. In display mode the raster is
            # anchored at the outer page origin, with no text scrolling. Empty
            # leading pixels stay transparent, including text and popup areas.
            left = top = 0
        right = max(item[2].right for item in visible)
        bottom = max(item[2].bottom for item in visible)
        width, height = right - left, bottom - top
        if width * height * 4 > self.max_image_bytes:
            raise GraphicsLimitError("Composited raster exceeds the image byte limit")
        canvas = Pixels.new("RGBA", (width, height))
        for placement, bounds, overlap in visible:
            image = image_lookup(placement.image_id)
            if image is None or image.generation != placement.image_generation:
                raise GraphicsError(
                    "Native image generation changed during presentation"
                )
            local = Rect(
                overlap.x - bounds.x,
                overlap.y - bounds.y,
                overlap.width,
                overlap.height,
            )
            pixels = _pixels(image, placement, local)
            part = Pixels.frombytes("RGBA", (overlap.width, overlap.height), pixels)
            canvas.alpha_composite(part, (overlap.x - left, overlap.y - top))
        draw = ImageDraw.Draw(canvas)
        canvas_bounds = Rect(left, top, width, height)
        for occluder in occluders:
            covered = canvas_bounds.intersect(occluder)
            if covered is not None:
                draw.rectangle(
                    (
                        covered.x - left,
                        covered.y - top,
                        covered.right - left - 1,
                        covered.bottom - top - 1,
                    ),
                    fill=(0, 0, 0, 0),
                )
        if self.protocol == "sixel":
            from . import sixel

            self._flatten_alpha(canvas, frame, origin)
            encoded = sixel.encode(canvas, self.max_bytes)
        else:
            cursor = f"\x1b[{top // ch + 1};{left // cw + 1}H"
            encoded = cursor + raster.iterm(canvas, self.max_bytes)
        wire = "\x1b7" + encoded + "\x1b8"
        if len(wire) > self.max_bytes:
            raise GraphicsLimitError("Composited raster exceeds the wire byte limit")
        old = self._erase(self._coverage)
        self._scene = scene
        self._coverage = Rect(
            left // cw, top // ch, (width + cw - 1) // cw, (height + ch - 1) // ch
        )
        self._wire = wire
        return GraphicsPlan(old, wire, repaint=bool(old))


def create_presenter(protocol: str | None, *, background=None) -> Presenter:
    """Instantiate the broker's selected output independently of native input providers.

    This is the only selection point. A replacement can retire a backend and
    its encoder once the Presenter wire/occlusion/cleanup tests pass unchanged.
    """
    if protocol is None:
        return DisabledPresenter()
    if protocol == "kitty":
        return GraphicsPresenter()
    if protocol in ("iterm", "sixel"):
        return RasterPresenter(protocol, background=background)
    raise GraphicsError("Unknown image output protocol")
