"""Compare fixed native workloads through ish and version-pinned Zellij PTYs."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import traceback
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src")]

from comparison_terminal import (  # noqa: E402
    ComparedTerminal,
    ResourceSampler,
    identity,
)


def save(path, data):
    """Keep complete stage evidence and never silently overwrite an earlier run."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def contracts(terminal):
    """Assert independent expectations before comparing the two implementations."""
    root = terminal.root
    terminal.require(lambda: (root / "core.json").exists(), timeout=45)
    terminal.require(lambda: terminal.frame_has("ABxyCDEF"))
    first = terminal.snapshot()
    reply = json.loads((root / "cursor.json").read_text())["reply"]
    if reply != b"\x1b[6;10R".hex():
        raise AssertionError(("Cursor query", reply))
    if first["cells"]["3,0"]["fg"] != "7b2d43" or not first["cells"]["3,0"]["bold"]:
        raise AssertionError(("Basic SGR", first["cells"]["3,0"]))
    (root / "core-next").touch()
    terminal.require(lambda: terminal.frame_has("UNICODE_READY"))
    unicode = terminal.snapshot()
    expected_unicode = unicodedata.normalize("NFC", "한글 café é 🚀")
    actual_unicode = unicodedata.normalize("NFC", unicode["lines"][0].strip())
    differences = []
    if actual_unicode != expected_unicode:
        differences.append(
            {
                "contract": "unicode-graphemes",
                "expected": expected_unicode,
                "actual": actual_unicode,
            }
        )
    (root / "unicode-next").touch()
    terminal.require(lambda: terminal.frame_has("ALTERNATE_READY"))
    alternate = terminal.snapshot()
    (root / "alternate-next").touch()
    terminal.require(lambda: terminal.frame_has("RETURN_READY"))
    returned = terminal.snapshot()
    if not terminal.frame_has("UNICODE_READY"):
        raise AssertionError(("Primary restoration", returned))
    (root / "return-next").touch()
    terminal.require(lambda: terminal.frame_has("INPUT_READY"))
    payload = b"\x1b[A\x1b[200~" + "한글\nsecond".encode() + b"\x1b[201~\x1bx|done|"
    terminal.send(payload)
    terminal.require(lambda: (root / "input.json").exists())
    received = bytes.fromhex(json.loads((root / "input.json").read_text())["hex"])
    expected = b"\x1bOA" + payload[3:]
    if received != expected:
        raise AssertionError(("Final consumer", expected, received))
    terminal.require(lambda: terminal.frame_has("RESIZE_READY"))
    terminal.resize(31, 108)
    # Query rendering at the new geometry before allowing the child to observe ioctl.
    terminal.require(
        lambda: (
            (root / "resize-signal.json").exists()
            and json.loads((root / "resize-signal.json").read_text())["size"]
            == [108, 31]
        )
    )
    (root / "resize-next").touch()
    terminal.require(lambda: (root / "size.json").exists())
    size = json.loads((root / "size.json").read_text())["size"]
    if size != [108, 31]:
        raise AssertionError(("Native PTY size", size))
    terminal.require(lambda: terminal.frame_has("DONE_READY"))
    terminal.finish()
    return {
        "differences": differences,
        "cursor": reply,
        "input": received.hex(),
        "size": size,
        "frames": {
            "core": first,
            "unicode": unicode,
            "alternate": alternate,
            "returned": returned,
        },
    }


def benchmark(terminal):
    """Measure observed rendering and byte roundtrips rather than write() alone."""
    terminal.require(lambda: terminal.frame_has("BENCH_READY"), timeout=45)
    start = time.monotonic()
    terminal.send(b"s")
    terminal.require(lambda: terminal.frame_has("BENCH_OUTPUT_DONE"), timeout=90)
    elapsed = time.monotonic() - start
    terminal.require(lambda: (terminal.root / "produced.json").exists())
    produced = json.loads((terminal.root / "produced.json").read_text())
    latencies = []
    for index in range(30):
        probe = f"{index:02d}|"
        start = time.monotonic()
        terminal.send(probe)
        terminal.require(lambda probe=probe: terminal.frame_has("ECHO_" + probe))
        latencies.append((time.monotonic() - start) * 1000)
    terminal.require(lambda: terminal.frame_has("BENCH_DONE"))
    terminal.finish()
    return {
        "producer": produced,
        "observed_output_seconds": elapsed,
        "latency_ms": latencies,
        "latency_median_ms": statistics.median(latencies),
        "latency_max_ms": max(latencies),
        "output_wire_bytes": len(terminal.output),
    }


def applications(terminal):
    """Verify editor writes, pager search, process UI, picker and Python stdin."""
    root = terminal.root
    for name in ("vim", "nvim"):
        terminal.require(
            lambda name=name: (root / (name + "-ready")).exists(), timeout=45
        )
        terminal.require(lambda: terminal.frame_has("NATIVE_EDITOR"))
        terminal.send("gg0i한글 \x1b:wq\r")
        terminal.require(lambda name=name: (root / (name + "-result.json")).exists())
        text = json.loads((root / (name + "-result.json")).read_text())["text"]
        if text != "한글 NATIVE_EDITOR\n":
            raise AssertionError((name, text))
    terminal.require(lambda: terminal.frame_has("LESS_0000"))
    terminal.send("/TARGET_LESS_RESULT\r")
    terminal.require(lambda: terminal.frame_has("TARGET_LESS_RESULT"))
    terminal.send("q")
    terminal.require(lambda: (root / "less-result.json").exists())
    terminal.require(lambda: terminal.frame_has("Mem") and terminal.frame_has("Tasks"))
    terminal.resize(30, 110)
    terminal.send("q")
    terminal.require(lambda: (root / "htop-result.json").exists())
    terminal.require(lambda: terminal.frame_has("FZF_READY:"))
    terminal.send("needle\r")
    terminal.require(lambda: (root / "fzf-result.json").exists())
    selected = json.loads((root / "fzf-result.json").read_text())["text"]
    if selected != "needle 한글\n":
        raise AssertionError(("fzf", selected))
    terminal.require(lambda: terminal.frame_has(">>>"))
    terminal.send(
        "from pathlib import Path; Path('python.txt').write_text('PYTHON_한글')\r"
    )
    terminal.require(lambda: (root / "python.txt").exists())
    if (root / "python.txt").read_text() != "PYTHON_한글":
        raise AssertionError("Python native input was changed")
    terminal.send("exit()\r")
    terminal.require(lambda: terminal.frame_has("APPS_DONE"))
    terminal.finish(b"q\r")
    return {
        "versions": json.loads((root / "versions.json").read_text()),
        "exits": json.loads((root / "apps-result.json").read_text()),
        "selected": selected,
    }


def stress(terminal):
    """Measure ten acknowledged 20,000-line bursts, including retained history."""
    terminal.require(lambda: terminal.frame_has("STRESS_READY"), timeout=45)
    terminal.send("s")
    durations = []
    start = time.monotonic()
    for batch in range(10):
        terminal.require(
            lambda batch=batch: terminal.frame_has(f"STRESS_BATCH_{batch:02d}"),
            timeout=90,
        )
        durations.append(time.monotonic() - start)
        terminal.send("n")
        start = time.monotonic()
    terminal.require(lambda: terminal.frame_has("STRESS_DONE"))
    result = json.loads((terminal.root / "stress.json").read_text())
    if result["lines"] != 200000 or result["bytes"] != 23400000:
        raise AssertionError(result)
    terminal.finish()
    return {
        "producer": result,
        "burst_seconds": durations,
        "wire_bytes": len(terminal.output),
    }


def images(terminal):
    """Retain real output per operation; this records bytes, not a pixel pass claim."""
    result = {}
    offset = 0
    for name in ("ech", "text", "ed2"):
        for stage in ("READY", "DONE"):
            terminal.require(
                lambda name=name, stage=stage: terminal.frame_has(
                    f"IMAGE_{name}_{stage}"
                ),
                timeout=45,
            )
            terminal.receive()
            if stage == "DONE":
                terminal.resize(24, terminal.cols + 1)
                terminal.require(
                    lambda name=name: terminal.frame_has(f"RESIZED_{name}")
                )
                terminal.receive()
            data = bytes(terminal.output[offset:])
            offset = len(terminal.output)
            path = terminal.root / f"{name}-{stage.lower()}.bin"
            path.write_bytes(data)
            packets = re.findall(rb"\x1bP[0-9;]*q.*?\x1b\\", data, re.DOTALL)
            for index, packet in enumerate(packets):
                (terminal.root / f"{name}-{stage.lower()}-{index}.sixel").write_bytes(
                    packet
                )
            result[name + "-" + stage] = {
                "wire_bytes": len(data),
                "sixel_packets": len(packets),
                "frame": terminal.screen.display,
            }
            terminal.send("n")
    terminal.require(lambda: terminal.frame_has("IMAGES_DONE"))
    terminal.finish()
    return {
        "observations": result,
        "claim": "Raw output observations only; pixel composition must be checked using an independent SIXEL renderer and actual host.",
    }


def main():
    """Run both products under the same outer observer and retain every failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--zellij", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("contracts", "benchmark", "apps", "stress", "images", "all"),
        default="all",
    )
    parser.add_argument("--engine", choices=("ish", "zellij", "both"), default="both")
    parser.add_argument(
        "--apps-dir", type=Path, default=Path.home() / ".cache/ish-comparison/apps"
    )
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    result = {
        "comparator": identity(args.zellij),
        "scope": "scripted controlling PTYs; independent pyte text/style observer; no actual host pixels or physical input",
        "cases": [],
    }
    modes = (
        ("contracts", "apps", "benchmark", "stress")
        if args.mode == "all"
        else (args.mode,)
    )
    for mode in modes:
        for engine in ("ish", "zellij") if args.engine == "both" else (args.engine,):
            case = {"engine": engine, "mode": mode}
            terminal = None
            sampler = None
            try:
                root = directory / f"{engine}-{mode}"
                command = [
                    sys.executable,
                    str(ROOT / "tests/comparison_child.py"),
                    str(root),
                    mode,
                ]
                if mode == "apps":
                    command = [
                        sys.executable,
                        str(ROOT / "tests/comparison_apps.py"),
                        str(root),
                        str(args.apps_dir),
                    ]
                elif mode == "images":
                    command = [sys.executable, str(ROOT / "tests/comparison_images.py")]
                terminal = ComparedTerminal(root, engine, command, zellij=args.zellij)
                with ResourceSampler(terminal) as sampler:
                    case.update(
                        status="passed",
                        result={
                            "contracts": contracts,
                            "benchmark": benchmark,
                            "apps": applications,
                            "images": images,
                            "stress": stress,
                        }[mode](terminal),
                    )
                    if case["result"].get("differences"):
                        case["status"] = "different"
                    elif mode == "images":
                        case["status"] = "observed"
            except Exception:
                case.update(status="failed", error=traceback.format_exc())
            finally:
                if sampler is not None and sampler.samples:
                    case["resources"] = sampler.result()
                if terminal is not None:
                    terminal.close()
            result["cases"].append(case)
            save(directory / "summary.json", result)
            print(
                json.dumps({key: case[key] for key in ("engine", "mode", "status")}),
                flush=True,
            )
    result["ending_identity"] = identity(args.zellij)
    result["source_changed_during_run"] = (
        result["comparator"]["source_sha256"]
        != result["ending_identity"]["source_sha256"]
    )
    states = {case["status"] for case in result["cases"]}
    result["status"] = (
        "failed"
        if "failed" in states
        else (
            "different"
            if "different" in states
            else ("observed" if states == {"observed"} else "passed")
        )
    )
    save(directory / "summary.json", result)
    return int(result["status"] not in {"passed", "observed"})


if __name__ == "__main__":
    raise SystemExit(main())
