"""Exercise raster input and popup composition against an actual outer terminal.

Protocol/file checks are automatic. --hold exposes each rendered phase for
separate visual inspection; it never labels byte evidence as pixel evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import sys
import termios
import time
import traceback
import tty
from pathlib import Path

from terminal_host import GATE_SECONDS, HostTerminal, record
from terminal_restore import observe
from terminal_scenarios import Scenario
from test_terminal_cli import STARTUP_DEADLINE
from test_terminal_image_protocol import png

PIXELS = 80
RED = bytes((255, 0, 0, 255))
BLUE = bytes((0, 0, 255, 255))

ASSET_PLUGIN = r"""from dataclasses import asdict
import hashlib
import json
from pathlib import Path

PLUGIN_META = {"version": "1", "requirements": [], "dependencies": []}
TERMINAL_API = 1

def setup(api):
    root = Path(__file__).parents[2]
    scene = None
    missing = set()
    serial = 0
    def rendered(_):
        nonlocal scene, serial
        request_path = root / 'presentation-probe.json'
        try:
            request = json.loads(request_path.read_text())
        except FileNotFoundError:
            return
        if request['scene'] != scene:
            scene = request['scene']
            missing.clear()
        owner = api.owner
        frame = owner.vt.frame()
        identities = set(request.get('ids', ()))
        identities.update(image.image_id for image in frame.images)
        assets = []
        for identity in sorted(identities):
            exists = owner.vt.image_exists(identity)
            if not exists and identity in request.get('ids', ()):
                missing.add(identity)
            image = owner.vt.image(identity) if exists else None
            assets.append({
                'id': identity, 'exists': exists,
                'width': image.width if image else None,
                'height': image.height if image else None,
                'sha256': hashlib.sha256(image.rgba).hexdigest() if image else None,
            })
        serial += 1
        result = {
            'token': request['token'], 'scene': scene, 'serial': serial,
            'assets': assets, 'lost_ids': sorted(missing),
            'placements': [asdict(image) for image in frame.images],
            'host': asdict(owner.profile),
            'geometry_pending': owner.host.geometry_pending,
            'graphics_enabled': owner.vt.graphics_enabled,
            'cols': frame.cols, 'rows': frame.rows,
        }
        temporary = root / 'presentation-assets.next'
        temporary.write_text(json.dumps(result))
        temporary.replace(root / 'presentation-assets.json')
    api.owner.app.after_render += rendered
    def close():
        api.owner.app.after_render -= rendered
    return close
"""

READER = """import json
import os
import select
import signal
from pathlib import Path

sizes = []
resized = True
def changed(*_):
    global resized
    resized = True
signal.signal(signal.SIGWINCH, changed)
os.write(1, bytes.fromhex({wire!r}))
Path('presentation-ready').write_text('ready')
data = bytearray()
followup = {followup!r}
while not data.endswith(b'\\n'):
    if resized:
        resized = False
        size = os.get_terminal_size(0)
        sizes.append([size.columns, size.lines])
        temporary = Path('presentation-sizes.next')
        temporary.write_text(json.dumps(sizes))
        temporary.replace('presentation-sizes.json')
    if followup and Path('presentation-write').exists():
        os.write(1, bytes.fromhex(followup))
        followup = ''
        Path('presentation-written').write_text('written')
    if select.select([0], [], [], 0.05)[0]:
        part = os.read(0, 4096)
        if not part:
            raise EOFError('Native input ended before confirmation')
        data.extend(part)
Path('presentation-input').write_bytes(data)
"""


class Assets:
    """Observe native assets without adding product diagnostics or a new renderer."""

    PLACEMENT_FIELDS = (
        "id",
        "image_id",
        "x",
        "y",
        "width",
        "height",
        "source_x",
        "source_y",
        "source_width",
        "source_height",
        "layer",
        "z",
    )

    @staticmethod
    def install(directory):
        """Add an explicit test-only readonly plugin beside the existing popup fixture."""
        plugin = directory / "home/plugins/presentation_probe"
        plugin.mkdir(parents=True)
        (plugin / "presentation_probe.py").write_text(ASSET_PLUGIN, encoding="utf-8")

    def __init__(self, terminal):
        """Keep request identity and expected image IDs local to this fixture."""
        self.terminal = terminal
        self.serial = 0
        self.scene = None
        self.ids = []

    def request(self, scene):
        """Publish a token before the next native output or layout change."""
        if scene != self.scene:
            self.ids = []
            self.scene = scene
        self.serial += 1
        record(
            self.terminal.root,
            "presentation-probe.json",
            {"scene": scene, "token": self.serial, "ids": self.ids},
        )
        return self.serial

    def snapshot(self, ready=None):
        """Require this request's rendered snapshot, not a previous scene's file."""
        path = self.terminal.root / "presentation-assets.json"
        snapshot = None

        def available():
            nonlocal snapshot
            current = read_record(path)
            if (
                current is not None
                and current["token"] == self.serial
                and current["placements"]
                and (ready is None or ready(current))
            ):
                snapshot = current
                return True
            return False

        self.terminal.require(available)
        return snapshot

    def expected_pixels(self, scene, profile):
        """Keep independent known raster expectations outside native observations."""
        if scene == "sixel-transparent":
            cw, ch = profile["cell_width"], profile["cell_height"]
            width, height = ((160 + cw - 1) // cw) * cw, ((60 + ch - 1) // ch) * ch
            pixels = (RED * 80 + bytes(4) * (width - 80)) * 60
            pixels += bytes(width * 4) * (height - 60)
            hashes = {hashlib.sha256(pixels).hexdigest()}
        elif scene == "kitty-animation":
            width = height = PIXELS
            hashes = {
                hashlib.sha256(color * PIXELS**2).hexdigest() for color in (RED, BLUE)
            }
        else:
            cw, ch = profile["cell_width"], profile["cell_height"]
            width, height = (
                ((PIXELS + cw - 1) // cw) * cw,
                ((PIXELS + ch - 1) // ch) * ch,
            )
            row = BLUE * PIXELS + bytes(4) * (width - PIXELS)
            pixels = row * PIXELS + bytes(width * 4) * (height - PIXELS)
            hashes = {hashlib.sha256(pixels).hexdigest()}
        return width, height, hashes

    def asset_hash(self, asset):
        """Compare original bytes when the fixture defines every color channel."""
        return asset["sha256"]

    def validate(self, scene, initial=None, *, snapshot=None):
        """Check independent pixel expectations and the same logical native placements."""
        if snapshot is None:
            snapshot = self.snapshot()
        if (
            snapshot["lost_ids"]
            or not snapshot["graphics_enabled"]
            or snapshot["geometry_pending"]
        ):
            raise AssertionError(
                ("Image lifetime or geometry readiness failed", snapshot)
            )
        assets = snapshot["assets"]
        if len(assets) != 1 or not assets[0]["exists"]:
            raise AssertionError(("Expected one retained native asset", snapshot))
        asset = assets[0]
        width, height, hashes = self.expected_pixels(scene, snapshot["host"])
        if (asset["width"], asset["height"]) != (
            width,
            height,
        ) or self.asset_hash(asset) not in hashes:
            raise AssertionError(
                ("Native raster content differs", asset, [width, height], hashes)
            )
        identities = sorted({image["image_id"] for image in snapshot["placements"]})
        if identities != [asset["id"]]:
            raise AssertionError(("Placement lost its asset", snapshot))
        if initial is not None:
            expected = sorted(
                tuple(image[key] for key in self.PLACEMENT_FIELDS)
                for image in initial["placements"]
            )
            actual = sorted(
                tuple(image[key] for key in self.PLACEMENT_FIELDS)
                for image in snapshot["placements"]
            )
            if actual != expected:
                raise AssertionError(
                    (
                        "Native placement changed across the host resize",
                        expected,
                        actual,
                    )
                )
        self.ids = identities
        return snapshot


class OverwriteAssets(Assets):
    """Check native image cell removal separately from unchanged source pixels."""

    COLUMNS = 16
    ROWS = 4
    ORIGIN = (34, 15)
    CUT = (1, 1, 4, 2)

    def __init__(self, terminal, profile):
        """Retain the measured initial cell geometry, not another terminal grid."""
        super().__init__(terminal)
        self.cell_width = profile["cell_width"]
        self.cell_height = profile["cell_height"]
        self.written = False

    def pixels(self):
        """Specify one removed narrow cell and both halves of the wide cell."""
        width = self.COLUMNS * self.cell_width
        height = self.ROWS * self.cell_height
        pixels = bytearray(RED * (width * height))
        if self.written:
            left, top, right, bottom = self.CUT
            for y in range(top * self.cell_height, bottom * self.cell_height):
                start = (y * width + left * self.cell_width) * 4
                end = (y * width + right * self.cell_width) * 4
                pixels[start:end] = bytes(end - start)
        return width, height, bytes(pixels)

    def expected_pixels(self, scene, profile):
        """Text writes and resize leave the original native RGBA asset untouched."""
        width = self.COLUMNS * self.cell_width
        height = self.ROWS * self.cell_height
        pixels = RED * (width * height)
        return width, height, {hashlib.sha256(pixels).hexdigest()}

    def has_expected_coverage(self, snapshot):
        """Require the exact known rectangle minus the written character cells."""
        width, height, pixels = self.pixels()
        left = self.ORIGIN[0] * self.cell_width
        top = self.ORIGIN[1] * self.cell_height
        actual = bytearray(width * height)
        for placement in snapshot["placements"]:
            x, y = placement["x"] - left, placement["y"] - top
            w, h = placement["width"], placement["height"]
            if not (0 <= x < x + w <= width and 0 <= y < y + h <= height):
                return False
            if (
                placement["source_x"],
                placement["source_y"],
                placement["source_width"],
                placement["source_height"],
            ) != (x, y, w, h):
                return False
            for row in range(y, y + h):
                start = row * width + x
                actual[start : start + w] = bytes((255,)) * w
        return actual == pixels[3::4]

    def validate(self, scene, initial=None, *, snapshot=None):
        """Keep source pixels, placement crops and final emitted image distinct."""
        snapshot = super().validate(scene, initial, snapshot=snapshot)
        if not self.has_expected_coverage(snapshot):
            raise AssertionError(
                ("Native image cells differ from the written region", snapshot)
            )
        return snapshot

    def wire(self, start):
        """Decode the actual outer SIXEL independently of the product decoder."""
        from test_terminal_sixel import read_pixels

        width, height, pixels = self.pixels()
        x = self.ORIGIN[0] * self.cell_width
        y = self.ORIGIN[1] * self.cell_height
        observed = None

        def matches():
            nonlocal observed
            packets = re.findall(
                rb'\x1bP0;1q"1;1;\d+;\d+.*?\x1b\\',
                bytes(self.terminal.output[start:]),
                re.S,
            )
            if not packets:
                return False
            image = read_pixels(packets[-1].decode("ascii"))
            actual = image.crop((x, y, x + width, y + height)).tobytes()
            observed = {
                "packets": len(packets),
                "canvas": list(image.size),
                "source": [width, height],
                "expected_sha256": hashlib.sha256(pixels).hexdigest(),
                "actual_sha256": hashlib.sha256(actual).hexdigest(),
            }
            return actual == pixels

        try:
            self.terminal.require(matches)
        except AssertionError as error:
            raise AssertionError(
                ("Host SIXEL overwrite pixels differ", observed)
            ) from error
        return observed


def read_record(path):
    """Read one atomic publication; DrvFS replacement may briefly hide its path."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def resize(terminal, assets, directory, result, name, phase, initial):
    """Wait for an actual host viewport change and its explicit controller acknowledgement."""
    before = [terminal.cols, terminal.rows]
    token = assets.request(name)
    stage = {
        "stage": len(result["phases"]) + 1,
        "kind": "resize",
        "name": name,
        "phase": phase,
        "before": before,
        "token": token,
    }
    result["phases"].append(stage)
    gate = directory / f"continue-{stage['stage']}"
    terminal.resize_gate = gate
    record(directory, "stage.json", stage)
    try:
        terminal.require(gate.exists, GATE_SECONDS)
        terminal.resize_pending = True
        terminal.sync_size()
        terminal.require(lambda: [terminal.cols, terminal.rows] != before)
    finally:
        terminal.resize_gate = None
    if terminal.cols < 64 or terminal.rows < 24:
        raise AssertionError(
            "Presentation fixture needs at least 64 columns and 24 rows after resize"
        )
    snapshot = assets.snapshot(
        lambda current: (
            not current["geometry_pending"]
            and current["host"]["image_protocol"] == "sixel"
            and [current["cols"], current["rows"]] == [terminal.cols, terminal.rows - 1]
        )
    )
    current = assets.validate(name, initial, snapshot=snapshot)
    profile = current["host"]
    if (profile["pixel_width"], profile["pixel_height"]) != (
        terminal.cols * profile["cell_width"],
        terminal.rows * profile["cell_height"],
    ):
        raise AssertionError(
            ("Host pixel geometry did not follow the actual viewport", current)
        )
    native_sizes = terminal.root / "presentation-sizes.json"
    terminal.require(
        lambda: (
            (sizes := read_record(native_sizes)) is not None
            and sizes[-1] == [current["cols"], current["rows"]]
        )
    )
    stage.update(
        after=[terminal.cols, terminal.rows],
        host=profile,
        native_sizes=json.loads(native_sizes.read_text()),
    )
    record(
        directory,
        f"{name}-{phase}-resize.json",
        {"stage": stage, "assets": current, "state": terminal.state()},
    )
    record(directory, "result.json", result)
    return current


def graphics(header, data=b""):
    """Build independent Kitty test records, without calling an ish encoder."""
    return b"\x1b_G" + header.encode() + b";" + base64.b64encode(data) + b"\x1b\\"


def scenes():
    """Use exact solid colors and transparent holes with known dimensions."""
    sixel = (
        b'\x1bP0;1q"1;1;160;60#1;2;100;0;0' + b"-".join([b"!80~!80?"] * 10) + b"\x1b\\"
    )
    kitty = graphics("a=T,f=32,i=42,s=80,v=80,C=1,q=2", RED * PIXELS**2)
    animated = (
        kitty
        + graphics("a=f,f=32,i=42,s=80,v=80,z=400,q=2", BLUE * PIXELS**2)
        + graphics("a=a,i=42,r=1,z=400,q=2")
        + graphics("a=a,i=42,s=3,q=2")
    )
    iterm = (
        b"\x1b]1337;File=inline=1;width=80px;height=80px:"
        + base64.b64encode(png(80, 80, BLUE))
        + b"\x1b\\"
    )
    return {"sixel-transparent": sixel, "kitty-animation": animated, "iterm-png": iterm}


def popup_overlap(state, profile):
    """Require a real intersection, not merely an open popup elsewhere on screen."""
    cw, ch = profile["cell_width"], profile["cell_height"]
    if not any(
        image["x"] < (x + width) * cw
        and x * cw < image["x"] + image["width"]
        and image["y"] < (y + height) * ch
        and y * ch < image["y"] + image["height"]
        for image in state["images"]
        for x, y, width, height in state["popups"]
    ):
        raise AssertionError("The popup did not actually overlap an image")


def visual_phase(terminal, directory, result, phase, hold):
    """Expose explicit visual phases without claiming screenshots from wire evidence."""
    stage = {
        "stage": len(result["phases"]) + 1,
        "name": "sixel-overwrite",
        "kind": "visual",
        "phase": phase,
    }
    result["phases"].append(stage)
    record(directory, "stage.json", stage)
    record(directory, "result.json", result)
    if hold:
        terminal.require((directory / f"overwrite-{phase}-continue").exists, 900)


def overwrite(terminal, directory, result, *, hold, require_resize):
    """Keep native text, persistent image cuts, popup masks and host output distinct."""
    name = "sixel-overwrite"
    assets = OverwriteAssets(terminal, result["host"])
    width, height, _ = assets.pixels()
    bands = []
    for y in range(0, height, 6):
        bits = min(6, height - y)
        bands.append(f"!{width}".encode() + bytes((63 + (1 << bits) - 1,)))
    image = (
        f'\x1bP0;1q"1;1;{width};{height}#1;2;100;0;0'.encode()
        + b"-".join(bands)
        + b"\x1b\\"
    )
    initial_wire = (
        b"\x1b[2J\x1b[Hsixel-overwrite" + "\x1b[17;36HA한\x1b[16;35H".encode() + image
    )
    command = terminal.root / "presentation.py"
    command.write_text(
        READER.format(
            wire=initial_wire.hex(), followup="\x1b[17;36HA한".encode().hex()
        ),
        encoding="utf-8",
    )
    assets.request(name)
    start = len(terminal.output)
    terminal.submit(f"{shlex.quote(sys.executable)} presentation.py")
    terminal.result("presentation-ready", b"ready")
    initial = assets.validate(name)
    record(directory, "overwrite-initial.json", initial)
    record(directory, "overwrite-initial-wire.json", assets.wire(start))
    visual_phase(terminal, directory, result, "initial", hold)

    assets.written = True
    assets.request(name)
    start = len(terminal.output)
    (terminal.root / "presentation-write").touch()
    terminal.result("presentation-written", b"written")
    current = assets.snapshot(assets.has_expected_coverage)
    written = assets.validate(name, snapshot=current)
    record(directory, "overwrite-written.json", written)
    record(directory, "overwrite-written-wire.json", assets.wire(start))
    visual_phase(terminal, directory, result, "written", hold)

    scenario = Scenario(terminal, "bash", True)
    assets.request(name)
    scenario.open_popup()
    terminal.require(lambda: bool(terminal.state().get("popups")))
    covered = terminal.state()
    popup_overlap(covered, result["host"])
    assets.validate(name, written)
    record(directory, "overwrite-covered.json", covered)
    if require_resize:
        resize(terminal, assets, directory, result, name, "covered", written)
    assets.request(name)
    start = len(terminal.output)
    scenario.cancel_popup()
    terminal.require(lambda: not terminal.state().get("popups"))
    restored = assets.validate(name, written)
    record(directory, "overwrite-restored.json", restored)
    record(directory, "overwrite-restored-wire.json", assets.wire(start))
    visual_phase(terminal, directory, result, "restored", hold)
    if require_resize:
        start = len(terminal.output)
        resize(terminal, assets, directory, result, name, "restored", written)
        record(directory, "overwrite-resized-wire.json", assets.wire(start))
    state = terminal.state()
    if state["image_errors"] or state["graphics_error"]:
        raise AssertionError(("Overwrite rendering failed", state))
    terminal.send(b"overwrite-complete\r")
    terminal.result("presentation-input", b"overwrite-complete\n")
    record(
        directory,
        "overwrite-native-input.json",
        {
            "input": (terminal.root / "presentation-input").read_text(),
            "sizes": read_record(terminal.root / "presentation-sizes.json"),
        },
    )
    terminal.require(terminal.at_prompt)
    assets.request(None)
    terminal.submit("printf '\\033[2J\\033[H'")
    terminal.require(lambda: not terminal.state().get("images"))


def run(directory, *, hold=False, require_resize=False, overwrite_only=False):
    """Observe real host replies and preserve every failed run's evidence."""
    directory.mkdir(parents=True)
    result = {
        "status": "running",
        "pixel_verification": "pending" if hold else "not performed",
        "phases": [],
        "require_resize": require_resize,
        "overwrite_only": overwrite_only,
        "scope": "actual host responses and viewport resize; native assets/placements and final input file; no screenshot or pixel inspection",
    }
    terminal = None
    saved = termios.tcgetattr(0)
    try:
        tty.setraw(0)
        before = observe()
        termios.tcsetattr(0, termios.TCSANOW, saved)
        result["initial_sixel_mode"] = before["modes"].get(80)
        Assets.install(directory)
        terminal = HostTerminal(
            directory,
            shell="bash",
            plugins=True,
            extra_args=("--plugin", "presentation_probe"),
        )
        assets = Assets(terminal)
        terminal.require(terminal.at_prompt, STARTUP_DEADLINE)
        profile = terminal.state()["host"]
        if profile["image_protocol"] != "sixel":
            raise AssertionError(("Expected actual host SIXEL negotiation", profile))
        result["host"] = profile
        scenario = Scenario(terminal, "bash", True)
        if overwrite_only:
            overwrite(
                terminal, directory, result, hold=hold, require_resize=require_resize
            )
        for name, wire in () if overwrite_only else scenes().items():
            assets.request(name)
            command = terminal.root / "presentation.py"
            command.write_text(
                READER.format(
                    followup="",
                    wire=(
                        (
                            b"\x1b[2J\x1b[H"
                            + name.encode()
                            + b"\x1b[15;35HIMAGE_BELOW\x1b[16;35H"
                            + wire
                        ).hex()
                    ),
                ),
                encoding="utf-8",
            )
            terminal.submit(f"{shlex.quote(sys.executable)} presentation.py")
            terminal.require(lambda: bool(terminal.state().get("images")))
            initial = terminal.state()
            if initial["image_errors"] or initial["graphics_error"]:
                raise AssertionError(("Image failed", initial))
            record(directory, name + "-visible.json", initial)
            initial_assets = assets.validate(name)
            record(directory, name + "-assets.json", initial_assets)
            assets.request(name)
            scenario.open_popup()
            terminal.require(lambda: bool(terminal.state().get("popups")))
            covered = terminal.state()
            if covered["graphics_error"]:
                raise AssertionError(("Popup composition failed", covered))
            popup_overlap(covered, profile)
            record(directory, name + "-covered.json", covered)
            assets.validate(name, initial_assets)
            stage = {
                "stage": len(result["phases"]) + 1,
                "name": name,
                "kind": "popup",
                "images": len(covered["images"]),
            }
            result["phases"].append(stage)
            record(directory, "stage.json", stage)
            if hold:
                terminal.require((directory / (name + "-continue")).exists, 900)
            if require_resize:
                resize(
                    terminal, assets, directory, result, name, "covered", initial_assets
                )
            assets.request(name)
            if terminal.state().get("popups"):
                scenario.cancel_popup()
            terminal.require(lambda: not terminal.state().get("popups"))
            restored_assets = assets.validate(name, initial_assets)
            if require_resize:
                restored_assets = resize(
                    terminal,
                    assets,
                    directory,
                    result,
                    name,
                    "restored",
                    initial_assets,
                )
            record(directory, name + "-restored-assets.json", restored_assets)
            record(directory, name + "-restored.json", terminal.state())
            # Observe several animation frames; this delay is not readiness.
            until = time.monotonic() + 1.3
            generations = set()
            while time.monotonic() < until:
                terminal.receive()
                generations.add(
                    json.dumps(terminal.state().get("images"), sort_keys=True)
                )
            if name == "kitty-animation" and len(generations) < 2:
                raise AssertionError(
                    "Native animation did not advance on the actual host"
                )
            terminal.send(b"continue\r")
            terminal.result("presentation-input", b"continue\n")
            record(
                directory,
                name + "-native-input.json",
                {
                    "input": (terminal.root / "presentation-input").read_text(),
                    "sizes": json.loads(
                        (terminal.root / "presentation-sizes.json").read_text()
                    ),
                },
            )
            terminal.require(terminal.at_prompt)
            (terminal.root / "presentation-input").unlink()
            assets.request(None)
            terminal.submit("printf '\\033[2J\\033[H'")
            terminal.require(lambda: not terminal.state().get("images"))
        terminal.finish()
        result["status"] = "passed"
        result["sizes"] = terminal.sizes
        result["outer_termios_restored"] = (
            termios.tcgetattr(terminal.fd) == terminal.attrs
        )
        if not result["outer_termios_restored"]:
            raise AssertionError("Outer PTY state changed after exit")
    except Exception:
        result.update(status="failed", error=traceback.format_exc())
    finally:
        if terminal is not None:
            terminal.close()
            result["host_termios_restored"] = (
                termios.tcgetattr(0) == terminal.saved_host
            )
            tty.setraw(0)
            after = observe()
            result["final_sixel_mode"] = after["modes"].get(80)
            if result.get("initial_sixel_mode") != result["final_sixel_mode"]:
                result.update(
                    status="failed", restore_error="SIXEL display mode changed"
                )
        termios.tcsetattr(0, termios.TCSANOW, saved)
        record(directory, "result.json", result)
    return result


def main():
    """Run only in a real host and keep visual inspection a separate claim."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--hold", action="store_true")
    parser.add_argument(
        "--overwrite-only",
        action="store_true",
        help="Verify persistent SIXEL character overwrite, popup restoration and native stdin.",
    )
    parser.add_argument(
        "--require-resize",
        action="store_true",
        help="Require two actual viewport changes per scene using continue-N gate files or Enter acknowledgement.",
    )
    args = parser.parse_args()
    if not os.isatty(0) or not os.isatty(1):
        parser.error("An actual interactive host terminal is required")
    result = run(
        args.directory.resolve(),
        hold=args.hold,
        require_resize=args.require_resize,
        overwrite_only=args.overwrite_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
