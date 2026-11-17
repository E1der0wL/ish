"""Check exclusive supplements with independent bytes, native VT and real PTYs."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from prompt_toolkit.layout import Window

from ish.terminal.engine import EngineInfo
from ish.terminal.extensions import ProtocolRegistry, ProtocolResult
from ish.terminal.inputs import InputPipeline
from ish.terminal.plugins import Plugins
from ish.terminal.protocol import Control
from ish.terminal.session import Session
from ish.terminal.vt import Terminal

DEADLINE = 5
PREFIX = b"7400;"
WIRE = b"\x1b]7400;sample\x07"


class Adapter:
    """A producer of explicit results; it cannot access a terminal or writer."""

    def __init__(self, result=None):
        """Prepare an independent expected output and lifecycle observations."""
        self.result = result if result is not None else ProtocolResult(native=b"X")
        self.seen = []
        self.cancellations = self.closes = 0

    def handle(self, control):
        """Record one complete input and return the caller's explicit result."""
        self.seen.append(control)
        return self.result

    def cancel(self):
        """Observe transfer cancellation separately from resource release."""
        self.cancellations += 1

    def close(self):
        """Count actual resource release attempts."""
        self.closes += 1


class Sink:
    """Observe bytes only; native ownership is separately checked against C below."""

    def __init__(self):
        """Begin with no native bytes delivered."""
        self.data = bytearray()

    def feed_raw(self, data):
        """Collect exactly the bytes reaching the native boundary."""
        self.data.extend(data)

    def is_unknown_osc(self, prefix):
        """Use one explicit fixture capability, not the registration validator."""
        return prefix == PREFIX


class ExtensionTests(unittest.TestCase):
    """Check protocol ownership independently of engine implementation."""

    def pipeline(self, adapter, *, reply=None):
        """Register the test namespace without creating any native state."""
        sink = Sink()
        pipeline = InputPipeline(sink, EngineInfo("test", "test", 1, ()), reply=reply)
        self.addCleanup(pipeline.close)
        pipeline.register(
            "example", kind="osc", prefixes=(PREFIX,), factory=lambda: adapter
        )
        return sink, pipeline

    def test_fragmented_utf8_and_controls_are_delivered_exactly_once(self):
        """Complete records retain order among byte-split Unicode text."""
        adapter = Adapter(ProtocolResult(native=b"\x1b[31mX\x1b[0m"))
        sink, pipeline = self.pipeline(adapter)
        wire = "前".encode() + WIRE + "後".encode()
        for value in wire:
            pipeline.feed(bytes((value,)))
        self.assertEqual(sink.data, "前".encode() + b"\x1b[31mX\x1b[0m" + "後".encode())
        self.assertEqual(adapter.seen, [Control("osc", b"7400;sample", WIRE)])

    def test_overlapping_names_and_prefixes_are_rejected_before_factory(self):
        """Ambiguous registrations cannot allocate a second live handler."""
        registry = ProtocolRegistry(write=lambda _: None)
        self.addCleanup(registry.close)
        registry.register("first", kind="osc", prefixes=(PREFIX,), factory=Adapter)
        for name, prefixes in (
            ("first", (b"7500;",)),
            ("second", (b"7400;sub=",)),
            ("third", (b"74",)),
        ):
            factory = Mock(return_value=Adapter())
            with self.assertRaises(ValueError):
                registry.register(name, kind="osc", prefixes=prefixes, factory=factory)
            factory.assert_not_called()

    def test_duplicate_prefixes_inside_one_registration_are_rejected(self):
        """One registration cannot claim the same selector more than once."""
        registry = ProtocolRegistry(write=lambda _: None)
        for prefixes in ((PREFIX, PREFIX), (PREFIX, b"7400;child=")):
            with self.assertRaises(ValueError):
                registry.register(
                    "duplicate", kind="osc", prefixes=prefixes, factory=Adapter
                )

    def test_factory_cannot_recursively_claim_its_pending_namespace(self):
        """A failed factory reservation is removed without publishing two owners."""
        registry = ProtocolRegistry(write=lambda _: None)
        self.addCleanup(registry.close)

        def nested():
            registry.register("inner", kind="osc", prefixes=(PREFIX,), factory=Adapter)
            return Adapter()

        with self.assertRaises(ValueError):
            registry.register("outer", kind="osc", prefixes=(PREFIX,), factory=nested)
        self.assertEqual(registry.routes, ())
        registry.register("final", kind="osc", prefixes=(PREFIX,), factory=Adapter)
        self.assertEqual(registry.routes, (("final", "adapter"),))

    def test_registration_is_sealed_by_even_an_incomplete_first_record(self):
        """Partial input cannot change its eventual consumer mid-record."""
        _, pipeline = self.pipeline(Adapter())
        pipeline.feed(b"\x1b]")
        with self.assertRaises(RuntimeError):
            pipeline.register("later", kind="osc", prefixes=(PREFIX,), factory=Adapter)

    def test_empty_feed_preserves_registration_and_closed_session_errors(self):
        """Only an actual first byte seals ownership; closing still rejects empty input."""
        sink = Sink()
        adapter = Adapter()
        pipeline = InputPipeline(sink, EngineInfo("test", "test", 1, ()))
        self.addCleanup(pipeline.close)
        pipeline.feed(b"")
        pipeline.register(
            "example", kind="osc", prefixes=(PREFIX,), factory=lambda: adapter
        )
        pipeline.feed(b"A" + WIRE + b"B")
        self.assertEqual(sink.data, b"AXB")
        self.assertEqual(adapter.seen, [Control("osc", b"7400;sample", WIRE)])
        pipeline.close()
        with self.assertRaises(RuntimeError):
            pipeline.feed(b"")

    def test_cancel_and_eof_discard_partial_payload_and_cleanup_once(self):
        """Cancellation reaches the native parser, never an extension handler."""
        for cancel in (b"\x18", b"\x1a"):
            with self.subTest(cancel=cancel):
                adapter = Adapter()
                sink, pipeline = self.pipeline(adapter)
                pipeline.feed(b"A\x1b]7400;partial" + cancel + b"B")
                pipeline.feed(b"\x1b]7400;unfinished")
                pipeline.close()
                pipeline.close()
                self.assertEqual(sink.data, b"A\x1b]7400;partial" + cancel + b"B")
                self.assertEqual(adapter.seen, [])
                self.assertEqual(adapter.closes, 1)
                self.assertEqual(adapter.cancellations, 2)

    def test_ris_cancels_transfer_before_subsequent_dispatch(self):
        """The native reset keeps its wire position and clears transfer state."""
        adapter = Adapter()
        sink, pipeline = self.pipeline(adapter)
        pipeline.feed(WIRE + b"\x1bc" + WIRE)
        self.assertEqual(sink.data, b"X\x1bcX")
        self.assertEqual(adapter.cancellations, 1)
        self.assertEqual(len(adapter.seen), 2)

    def test_failure_consumes_current_and_later_owned_controls_without_replay(self):
        """A failed codec cannot leak its payload into a replacement parser."""
        adapter = Adapter()
        adapter.handle = Mock(side_effect=ValueError("codec failed"))
        sink, pipeline = self.pipeline(adapter)
        pipeline.feed(b"A" + WIRE + b"B" + WIRE + b"C")
        self.assertEqual(sink.data, b"ABC")
        self.assertEqual(adapter.handle.call_count, 1)
        self.assertEqual(adapter.closes, 1)
        self.assertIn("codec failed", pipeline.protocol_errors[0])
        self.assertEqual(pipeline.protocol_failures, (("example", 1),))

    def test_invalid_result_and_missing_reply_writer_do_not_partially_commit(self):
        """Validate the entire result before issuing even its first native byte."""
        for result in (
            None,
            b"wrong",
            ProtocolResult(native="wrong"),
            ProtocolResult(native=b"\x1b]2;partial"),
            ProtocolResult(native=b"\xe7\x95"),
            ProtocolResult(native=b"X", reply=b"reply"),
        ):
            adapter = Adapter()
            adapter.result = result
            sink, pipeline = self.pipeline(adapter)
            pipeline.feed(b"A" + WIRE + b"B")
            self.assertEqual(sink.data, b"AB")
            self.assertEqual(adapter.closes, 1)

    def test_async_handlers_are_rejected_without_running_or_leaking_coroutines(self):
        """Protocol interpretation cannot yield across later PTY output."""
        adapter = Adapter()

        async def handle(_):
            raise AssertionError("must not execute asynchronous protocol handler")

        adapter.handle = handle
        sink, pipeline = self.pipeline(adapter)
        pipeline.feed(WIRE)
        self.assertEqual(sink.data, b"")
        self.assertEqual(adapter.closes, 1)

    def test_native_promotion_never_instantiates_or_imports_supplement(self):
        """A reviewed native capability bypasses construction of its old codec."""
        sink = Sink()
        sink.is_unknown_osc = Mock(return_value=False)
        pipeline = InputPipeline(
            sink,
            EngineInfo("test", "test", 1, (), (("example", "osc", (PREFIX,)),)),
        )
        self.addCleanup(pipeline.close)
        factory = Mock(side_effect=AssertionError("retired module was loaded"))
        self.assertEqual(
            pipeline.register(
                "example", kind="osc", prefixes=(PREFIX,), factory=factory
            ),
            "native",
        )
        pipeline.feed(b"A" + WIRE + b"B")
        self.assertEqual(sink.data, b"A" + WIRE + b"B")
        factory.assert_not_called()

    def test_native_promotion_requires_exact_namespace_not_just_a_name(self):
        """An unrelated native feature cannot silently retire an OSC codec."""
        sink = Sink()
        pipeline = InputPipeline(
            sink,
            EngineInfo("test", "test", 1, ("kitty",), (("example", "osc", (PREFIX,)),)),
        )
        self.addCleanup(pipeline.close)
        factory = Mock(return_value=Adapter())
        for name, prefixes in (("kitty", (PREFIX,)), ("example", (b"7401;",))):
            with self.assertRaises(ValueError):
                pipeline.register(name, kind="osc", prefixes=prefixes, factory=factory)
        factory.assert_not_called()

    def test_native_writer_failure_closes_pipeline_and_cannot_be_replayed(self):
        """A partially failed writer is fatal rather than another codec attempt."""
        adapter = Adapter()
        sink, pipeline = self.pipeline(adapter)

        def fail(data):
            sink.data.extend(data[:1])
            raise OSError("native writer failed")

        pipeline._protocols._write = fail
        with self.assertRaises(OSError):
            pipeline.feed(WIRE)
        with self.assertRaises(RuntimeError):
            pipeline.feed(WIRE)
        self.assertEqual(sink.data, b"X")
        self.assertEqual(len(adapter.seen), 1)
        self.assertEqual(adapter.closes, 1)

    def test_cleanup_failure_does_not_skip_other_adapters(self):
        """Every allocated codec is released even when one cleanup raises."""
        registry = ProtocolRegistry(write=lambda _: None)
        first, second = Adapter(), Adapter()
        second.close = Mock(side_effect=ValueError("cleanup failed"))
        registry.register(
            "first", kind="osc", prefixes=(PREFIX,), factory=lambda: first
        )
        registry.register(
            "second", kind="osc", prefixes=(b"7401;",), factory=lambda: second
        )
        registry.close()
        registry.close()
        self.assertEqual(first.closes, 1)
        self.assertEqual(second.close.call_count, 1)
        self.assertIn("cleanup failed", registry.errors[0])


class NativeExtensionTests(unittest.IsolatedAsyncioTestCase):
    """Use the actual parser, plugin loader, native grid and controlling PTY."""

    async def asyncSetUp(self):
        """Create the pinned engine with independently captured PTY replies."""
        self.replies = []
        self.terminal = Terminal(20, 4, self.replies.append)
        self.addCleanup(self.terminal.close)

    async def test_native_parser_protects_all_known_and_malformed_namespaces(self):
        """Malformed known OSC commands are never treated as missing protocols."""
        factory = Mock(return_value=Adapter())
        for prefix in (
            b"0;",
            b"2;",
            b"4;",
            b"8;",
            b"10;",
            b"52;",
            b"133;",
            b"1337;",
            b"5522;",
            b"7501;",
        ):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                self.terminal.register_protocol(
                    "override", kind="osc", prefixes=(prefix,), factory=factory
                )
        for kind, prefix in (
            ("dcs", PREFIX),
            ("csi", PREFIX),
            ("osc", b"07400;"),
            ("osc", b"7400;sub="),
            ("osc", b"7400"),
            ("osc", b"9" * 300 + b";"),
        ):
            with self.subTest(kind=kind, prefix=prefix), self.assertRaises(ValueError):
                self.terminal.register_protocol(
                    "invalid", kind=kind, prefixes=(prefix,), factory=factory
                )
        factory.assert_not_called()
        self.assertTrue(self.terminal.is_unknown_osc(PREFIX))

    async def test_rejected_scheduled_results_cannot_outlive_their_route(self):
        """Factory/handler/lifecycle Tasks are cancelled rather than left running."""
        for phase in ("factory", "handle", "cancel", "close", "invalid_cleanup"):
            with self.subTest(phase=phase):
                effects, tasks = [], []

                async def later(effects=effects):
                    await asyncio.sleep(0)
                    effects.append("late side effect")

                def scheduled(*_, later=later, tasks=tasks):
                    task = asyncio.create_task(later())
                    tasks.append(task)
                    return task

                registry = ProtocolRegistry(write=lambda _: None)
                adapter = Adapter()
                if phase == "factory":
                    with self.assertRaises(TypeError):
                        registry.register(
                            "example", kind="osc", prefixes=(PREFIX,), factory=scheduled
                        )
                elif phase == "invalid_cleanup":
                    invalid = SimpleNamespace(close=scheduled)
                    with self.assertRaises(TypeError):
                        registry.register(
                            "example",
                            kind="osc",
                            prefixes=(PREFIX,),
                            factory=lambda invalid=invalid: invalid,
                        )
                else:
                    setattr(adapter, phase, scheduled)
                    registry.register(
                        "example",
                        kind="osc",
                        prefixes=(PREFIX,),
                        factory=lambda adapter=adapter: adapter,
                    )
                    if phase == "handle":
                        registry.handle(Control("osc", b"7400;sample", WIRE))
                    elif phase == "cancel":
                        registry.cancel()
                registry.close()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.assertTrue(tasks)
                self.assertTrue(all(task.cancelled() for task in tasks))
                self.assertEqual(effects, [])

    async def test_conversion_and_reply_preserve_native_query_order_and_grid(self):
        """The real VT observes transformed text before replying at its cursor."""
        adapter = Adapter(
            ProtocolResult(native=b"\x1b[31mX\x1b[0m\x1b[6n", reply=b"CUSTOM")
        )
        self.terminal.register_protocol(
            "example", kind="osc", prefixes=(PREFIX,), factory=lambda: adapter
        )
        for value in b"A" + WIRE + b"B\x1b[6n":
            self.terminal.feed(bytes((value,)))
        row = self.terminal.frame().cells[0]
        self.assertEqual("".join(cell.text for cell in row)[:3], "AXB")
        self.assertEqual(b"".join(self.replies), b"\x1b[1;3RCUSTOM\x1b[1;4R")
        self.assertEqual(self.terminal.protocol_errors, ())

    async def test_existing_plugin_manager_loads_protocol_without_ui(self):
        """An external Python codec adds neither surfaces nor reserved keys."""
        with tempfile.TemporaryDirectory(prefix="ish-codec-plugin-") as directory:
            root = Path(directory)
            (root / "supplement").mkdir()
            (root / "supplement" / "__init__.py").write_text(
                "PLUGIN_META = {'version': '1.0'}\n"
                "from ish.terminal.extensions import ProtocolResult\n"
                "TERMINAL_API = 1\n"
                "class Codec:\n"
                "    def handle(self, control): return ProtocolResult(native=b'PLUGIN')\n"
                "    def cancel(self): pass\n"
                "    def close(self): pass\n"
                "def setup(api):\n"
                "    api.register_protocol('example', kind='osc', prefixes=(b'7400;',), factory=Codec)\n",
                encoding="utf-8",
            )
            plugins = Plugins(SimpleNamespace(vt=self.terminal), Window())
            self.addAsyncCleanup(plugins.close)
            plugins.load(("supplement",), directory=root, library_dir=root / "lib")
            self.terminal.feed(WIRE)
            self.assertEqual(
                "".join(cell.text for cell in self.terminal.frame().cells[0])[:6],
                "PLUGIN",
            )
            self.assertEqual(plugins.popups, [])
            self.assertEqual(plugins.bindings.bindings, [])
            self.assertFalse(plugins.mouse_required)

    async def test_plugin_search_uses_native_results_without_reserving_ui(self):
        """Search/select APIs reuse the native history and leave UI opt-in empty."""
        self.terminal.feed(b"first needle\r\nsecond needle\r\n")
        app = SimpleNamespace(invalidate=Mock())
        plugins = Plugins(SimpleNamespace(vt=self.terminal, app=app), Window())
        self.addAsyncCleanup(plugins.close)
        self.assertEqual(await plugins.search("needle"), 2)
        self.assertEqual(plugins.select_match(), 0)
        self.assertEqual(self.terminal.selection_text(), "needle")
        self.assertEqual(plugins.popups, [])
        self.assertEqual(plugins.bindings.bindings, [])

    async def test_later_plugin_search_cancels_only_older_waiter(self):
        """A delayed search cannot consume the steps of its replacement."""
        native = SimpleNamespace(
            set_search=Mock(), search_step=Mock(return_value=(False, 0))
        )
        plugins = Plugins(
            SimpleNamespace(vt=native, app=SimpleNamespace(invalidate=Mock())), Window()
        )
        self.addAsyncCleanup(plugins.close)
        old = asyncio.create_task(plugins.search("old"))
        await asyncio.sleep(0)
        native.search_step.return_value = (True, 3)
        self.assertEqual(await plugins.search("new"), 3)
        with self.assertRaises(asyncio.CancelledError):
            await old
        self.assertEqual(native.set_search.call_args_list[-1].args, ("new",))

    async def test_real_pty_reply_consumer_writes_exact_file(self):
        """The child consumes the protocol response through its original stdin."""
        with tempfile.TemporaryDirectory(prefix="ish-codec-pty-") as directory:
            result = Path(directory) / "reply.bin"
            finished = asyncio.Event()
            errors, exits = [], []
            script = (
                "import os, pathlib, tty\n"
                "tty.setraw(0)\n"
                "os.write(1, b'\\x1b]7400;sample\\x07')\n"
                "value = b''\n"
                "while len(value) < 8: value += os.read(0, 8-len(value))\n"
                "pathlib.Path('reply.bin').write_bytes(value)\n"
                "os.write(1, b'DONE')\n"
            )

            def exited(code):
                exits.append(code)
                finished.set()

            session = Session(
                [sys.executable, "-u", "-c", script],
                directory,
                self.terminal.feed,
                exited,
                errors.append,
            )
            self.addAsyncCleanup(session.close)
            terminal = Terminal(20, 4, session.send)
            self.addCleanup(terminal.close)
            session.output = terminal.feed
            terminal.register_protocol(
                "example",
                kind="osc",
                prefixes=(PREFIX,),
                factory=lambda: Adapter(
                    ProtocolResult(native=b"EXT", reply=b"ACCEPTED")
                ),
            )
            session.start(20, 4)
            await asyncio.wait_for(finished.wait(), DEADLINE)
            self.assertEqual(result.read_bytes(), b"ACCEPTED")
            self.assertEqual(exits, [0])
            self.assertEqual(errors, [])
            self.assertEqual(
                "".join(cell.text for cell in terminal.frame().cells[0])[:7], "EXTDONE"
            )


if __name__ == "__main__":
    unittest.main()
