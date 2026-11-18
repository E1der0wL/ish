"""Verify host negotiation against literal wire replies and real PTK input."""

import asyncio
import io
import json
import os
import tempfile
import termios
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_input, create_pipe_input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys
from prompt_toolkit.output.vt100 import Vt100_Output

from ish.terminal.host import (
    HostAppearance,
    HostBroker,
    HostInput,
    HostParser,
    HostProfile,
    StructuredKey,
    decode_key,
    scrub_environment,
)
from ish.terminal.mouse import decode_mouse
from ish.terminal.vt import Terminal


class ColorDepthProbeTests(unittest.TestCase):
    """Observe color depth through complete startup requests and the PTK parser."""

    def setUp(self):
        """Capture output packets without creating a second terminal reader."""
        self.writes, self.keys = [], []
        self.host = HostBroker(on_color_probe=self.writes.append)
        self.parser = HostParser(Vt100Parser(self.keys.append), self.host)
        self.query = self.host.begin_probe(80, 24)

    def test_indexed_host_and_quantified_rgb(self):
        """An eight-bit channel report can coexist with 256 indexed colors."""
        self.assertIn("\x1bP+q524742\x1b\\\x1bP+q436f\x1b\\", self.query)
        self.parser.feed("a\x1bP1+r436F=323536\x1b\\\x1bP1+r524742=382f")
        self.parser.feed("382f38\x1b\\\x1bP1$r0m\x1b\\b")
        self.assertEqual("".join(key.data for key in self.keys), "ab")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.host.finish_probe().color_depth, 24)

    def test_nonprinting_sgr_readback_before_return_to_shell(self):
        """The sampling packet already restores the host's original attributes."""
        self.parser.feed("\x1bP0+r524742\x1b\\")
        with patch("secrets.token_bytes", return_value=b"\x13\x57\xaf"):
            self.parser.feed("\x1bP1$r0;1;3;31;44m\x1b\\")
        self.assertEqual(
            self.writes,
            ["\x1b[0;38;2;19;87;175m\x1bP$qm\x1b\\\x1b[0;0;1;3;31;44m"],
        )
        self.parser.feed("x\x1bP1$r0;38:2::19:87:175m\x1b\\y")
        self.assertEqual(self.host.finish_probe().color_depth, 24)
        self.host.change_attributes()
        self.assertEqual(self.host.restore_attributes(), "\x1b[0;1;3;31;44m")
        self.assertEqual("".join(key.data for key in self.keys), "xy")

    def test_indexed_approximation_mismatch_and_late_rgb_preserve_observation(self):
        """A quantized, unrelated or expired reply cannot upgrade 16 colors."""
        self.parser.feed("\x1bP1+r436f=3136\x1b\\")
        with patch("secrets.token_bytes", return_value=b"\x13\x57\xaf"):
            self.parser.feed("\x1bP1$r0m\x1b\\")
        self.parser.feed("\x1bP1$r0;38:5:68m\x1b\\\x1bP1$r0;38:2::19:87:176m\x1b\\")
        self.assertEqual(self.host.finish_probe().color_depth, 4)
        self.parser.feed("a\x1bP1$r0;38:2::19:87:175m\x1b\\\x1bP1+r524742=38\x1b\\z")
        self.assertEqual(self.host.profile.color_depth, 4)
        self.assertEqual("".join(key.data for key in self.keys), "az")

    def test_unknown_or_cancelled_probe_preserves_initial_style(self):
        """A failed SGR snapshot or cancellation leaves host attributes untouched."""
        self.parser.feed("\x1bP1+r524742\x1b\\\x1bP0$r0m\x1b\\\x1bP1$r0m\x1b\\")
        self.assertEqual(self.writes, [])
        self.assertIsNone(self.host.finish_probe().color_depth)
        cancelled = HostBroker(on_color_probe=self.writes.append)
        cancelled.begin_probe(80, 24)
        cancelled.cancel_probe()
        cancelled.control("\x1bP1$r0;31m\x1b\\")
        cancelled.control("\x1bP1+r436f=323536\x1b\\")
        self.assertIsNone(cancelled.profile.color_depth)
        self.assertEqual(self.writes, [])

    def test_failed_quantified_rgb_does_not_claim_direct_color(self):
        """Lower channel widths cannot grant 24-bit output."""
        self.parser.feed("\x1bP1+r524742=352f362f35\x1b\\\x1bP0+r436f\x1b\\")
        self.assertIsNone(self.host.finish_probe().color_depth)
        self.assertEqual(self.writes, [])


class HostProbeTests(unittest.TestCase):
    """Probe success requires both the exact acknowledgement and usable geometry."""

    def setUp(self):
        """Use one known wire identity without deriving expected replies from code."""
        self.broker = HostBroker()
        self.keys = []
        self.parser = HostParser(Vt100Parser(self.keys.append), self.broker)
        self.query = self.broker.begin_probe(100, 30, query_id=2147515049)

    def feed(self, *fragments):
        """Exercise arbitrary reader chunks and PTK's normal Escape flush."""
        for fragment in fragments:
            self.parser.feed(fragment)
        self.parser.flush()

    def test_probe_queries_have_wire_identity_and_start_conservatively(self):
        """No optional capability is enabled by sending a query."""
        self.assertEqual(
            self.query,
            "\x1bP$q q\x1b\\\x1b[16t\x1b[14t\x1b[?2026$p\x1b[?5522$p\x1b[?2031$p\x1b[?996n\x1b[?u\x1b[>q"
            "\x1b]10;?\x1b\\\x1b]11;?\x1b\\\x1b]12;?\x1b\\\x1b]21;cursor=?\x1b\\"
            + "".join(f"\x1b]4;{index};?\x1b\\" for index in range(256))
            + "\x1b_Ga=q,i=2147515049,s=1,v=1,f=24,t=d;AAAA\x1b\\"
            + "".join(
                f"\x1b[?{mode}$p"
                for mode in (
                    1,
                    6,
                    7,
                    12,
                    25,
                    80,
                    1000,
                    1002,
                    1003,
                    1004,
                    1005,
                    1006,
                    1015,
                    1016,
                    2004,
                )
            )
            + "\x1bP$qm\x1b\\\x1b[c",
        )
        self.assertEqual(self.broker.profile, HostProfile())
        with self.assertRaises(FrozenInstanceError):
            self.broker.profile.kitty_graphics = True

    def test_probe_ids_cannot_collide_with_presenter_image_ids(self):
        """Reserve the upper graphics-ID half for negotiation, including tests."""
        with self.assertRaises(ValueError):
            HostBroker().begin_probe(80, 24, query_id=2**31 - 1)

    def test_matching_replies_enable_only_verified_capabilities(self):
        """One matching acknowledgement and geometry permit graphics at commit."""
        self.feed(
            "\x1b[6;20;10t",
            "\x1b[4;600;1000t",
            "\x1b[?2026;2$y",
            "\x1b[?5522;2$y",
            "\x1b[?0u",
            "\x1b[?1016;2$y",
            "\x1b[?62;4;6;22c",
            "\x1bP>|kitty(0.50.0)\x1b\\",
            "\x1b_Gi=2147515049;OK\x1b\\",
        )
        self.assertFalse(self.broker.ready.is_set())  # Appearance is still unknown.
        self.assertFalse(self.broker.profile.kitty_graphics)
        profile = self.broker.finish_probe()
        self.assertEqual(
            profile,
            HostProfile(
                kitty_graphics=True,
                hyperlinks=True,
                synchronized_output=True,
                cell_width=10,
                cell_height=20,
                pixel_width=1000,
                pixel_height=600,
                enhanced_keyboard=True,
                extended_styles=True,
                pixel_mouse=True,
                clipboard_protocol=True,
                image_protocol="kitty",
                overline=True,
            ),
        )
        self.assertEqual(self.keys, [])

    def test_pixel_report_can_establish_exact_cell_size(self):
        """An exactly divisible viewport report establishes physical cell dimensions."""
        self.feed("\x1b[4;600;1000t\x1b_Gi=2147515049;OK\x1b\\")
        profile = self.broker.finish_probe()
        self.assertTrue(profile.kitty_graphics)
        self.assertEqual((profile.cell_width, profile.cell_height), (10, 20))

    def test_fractional_or_missing_geometry_does_not_enable_graphics(self):
        """Ambiguous cell dimensions cannot authorize image placement."""
        self.feed("\x1b[4;601;1000t\x1b_Gi=2147515049;OK\x1b\\")
        profile = self.broker.finish_probe()
        self.assertFalse(profile.kitty_graphics)
        self.assertFalse(profile.has_cell_size)

    def test_wrong_duplicate_and_rejected_ack_cannot_enable_graphics(self):
        """An unrelated identity or a later duplicate cannot replace rejection."""
        self.feed(
            "\x1b[6;20;10t",
            "\x1b_Gi=2147515048;OK\x1b\\",
            "\x1b_Gi=2147515049;EINVAL:unsupported\x1b\\",
            "\x1b_Gi=2147515049;OK\x1b\\",
        )
        self.assertFalse(self.broker.finish_probe().kitty_graphics)
        self.assertEqual(self.keys, [])

    def test_late_responses_cannot_upgrade_or_leak_after_fallback(self):
        """Late responses remain control traffic after negotiation ends."""
        initial = self.broker.finish_probe()
        self.feed(
            "a\x1b[6;20;10t",
            "\x1b_Gi=2147515049;OK\x1b\\",
            "\x1b[?2026;1$yb",
        )
        self.assertIs(self.broker.finish_probe(), initial)
        self.assertEqual("".join(key.data for key in self.keys), "ab")
        with self.assertRaises(RuntimeError):
            self.broker.begin_probe(100, 30, query_id=2147515050)

    def test_cancelled_probe_keeps_a_conservative_profile(self):
        """Cancellation revokes observations without changing subsequent input."""
        self.feed("\x1b[6;20;10t\x1b_Gi=2147515049;OK\x1b\\")
        self.broker.cancel_probe()
        self.feed("\x1b[?2026;1$y\x1b_Gi=2147515049;OK\x1b\\")
        self.assertEqual(self.broker.finish_probe(), HostProfile())
        self.assertTrue(self.broker.ready.is_set())

    def test_every_reply_split_preserves_adjacent_keys(self):
        """Partial controls survive flush after their control introducer is known."""
        replies = (
            "\x1b[6;20;10t",
            "\x1b_Gi=2147515049;OK\x1b\\",
            "\x1b[?2026;2$y",
            "\x1b]10;rgb:0000/0000/0000\x07",
            "\x1bP1+r544e=787465726d\x1b\\",
            "\x1b[?62;22c",
        )
        for reply in replies:
            for split in range(1, len(reply)):
                with self.subTest(reply=repr(reply), split=split):
                    keys = []
                    parser = HostParser(Vt100Parser(keys.append), HostBroker())
                    parser.feed("A" + reply[:split])
                    # A terminal reply may span PTK's Escape flush interval.
                    if split > 1:
                        parser.flush()
                    parser.feed(reply[split:] + "B")
                    parser.flush()
                    self.assertEqual("".join(key.data for key in keys), "AB")

    def test_paste_payload_is_not_classified_as_terminal_replies(self):
        """Query-shaped text inside an explicit paste remains one paste payload."""
        payload = "\x1b[6;20;10t\x1b_Gi=2147515049;OK\x1b\\\n한글"
        wire = "\x1b[200~" + payload + "\x1b[201~x"
        for character in wire:
            self.parser.feed(character)
        self.parser.flush()
        self.assertEqual(len(self.keys), 2)
        self.assertEqual(self.keys[0].key, Keys.BracketedPaste)
        self.assertEqual(self.keys[0].data, payload)
        self.assertEqual(self.keys[1].data, "x")
        self.assertFalse(self.broker.finish_probe().kitty_graphics)

    def test_escape_before_reply_keeps_its_introducer_across_every_split(self):
        """Popup cancellation adjacent to a response never turns its tail into keys."""
        for reply in (
            "\x1b[6;20;10t",
            "\x1b[4;480;800t",
            "\x1b_Gi=2147515049;OK\x1b\\",
            "\x9b6;20;10t",
            "\x9fGi=2147515049;OK\x9c",
        ):
            wire = "\x1b" + reply
            for split in range(len(wire) + 1):
                with self.subTest(reply=repr(reply), split=split):
                    keys = []
                    parser = HostParser(Vt100Parser(keys.append), HostBroker())
                    parser.feed("a" + wire[:split])
                    # A solitary, legacy response ESC still has the known
                    # timing ambiguity. Once framed, the reply survives flush.
                    if split != 2 or reply.startswith("\x9b"):
                        parser.flush()
                    parser.feed(wire[split:] + "b")
                    parser.flush()
                    self.assertEqual(
                        [(key.key, key.data) for key in keys],
                        [("a", "a"), (Keys.Escape, "\x1b"), ("b", "b")],
                    )

    def test_legacy_alt_arrow_and_repeated_escape_keep_key_identity(self):
        """Preserving the second introducer also preserves conventional Meta keys."""
        wire = "\x1b\x1b[A\x1bx\x1b\x1b"
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                keys = []
                parser = HostParser(Vt100Parser(keys.append), HostBroker())
                parser.feed(wire[:split])
                parser.feed(wire[split:])
                parser.flush()
                self.assertEqual(
                    [key.key for key in keys],
                    [Keys.Escape, Keys.Up, Keys.Escape, "x", Keys.Escape, Keys.Escape],
                )

    def test_mouse_arrow_cpr_alt_and_lone_escape_keep_ptk_semantics(self):
        """Existing keyboard, mouse and PTK cursor-response paths are preserved."""
        self.feed("\x1b[A\x1b[<0;3;5M\x1b[2;9R\x1bx")
        self.assertEqual(
            [key.key for key in self.keys],
            [Keys.Up, Keys.Vt100MouseEvent, Keys.CPRResponse, Keys.Escape, "x"],
        )
        self.keys.clear()
        self.feed("\x1b")
        self.assertEqual([key.key for key in self.keys], [Keys.Escape])

    def test_conventional_modified_function_keys_need_no_enhanced_handshake(self):
        """Known CSI keys retain identities even when the host has no Kitty mode."""
        self.broker.finish_probe()
        self.feed("\x1b[13;2~\x1b[13;5~\x1b[1;3P")
        self.assertFalse(self.broker.input_modes_active)
        self.assertEqual(
            [key.structured_key for key in self.keys],
            [
                StructuredKey("f3", 57366, 1),
                StructuredKey("f3", 57366, 2),
                StructuredKey("f1", 57364, 4),
                StructuredKey("f1", 57364, 4),
            ],
        )
        received = []
        self.broker.on_key = lambda event: received.append(event) or True
        for key in self.keys:
            self.assertTrue(self.broker.handle_key(key))
        self.assertEqual(
            received,
            [
                StructuredKey("f3", 57366, 1),
                StructuredKey("f3", 57366, 2),
                StructuredKey("f1", 57364, 4),
            ],
        )

    def test_legacy_key_support_does_not_guess_cpr_or_enable_extended_events(self):
        """Ambiguous R replies stay CPR, and unnegotiated CSI-u is control traffic."""
        self.broker.finish_probe()
        self.feed("\x1b[1;2R\x1b[97;5u\x1b[13;5:3~")
        self.assertEqual([key.key for key in self.keys], [Keys.CPRResponse])
        self.assertFalse(hasattr(self.keys[0], "structured_key"))

    def test_focus_and_graphics_errors_have_distinct_control_consumers(self):
        """Errors and focus events reach handlers, never the command input buffer."""
        focus, errors = [], []
        self.broker.on_focus = focus.append
        self.broker.on_graphics_error = lambda identity, message: errors.append(
            (identity, message)
        )
        self.broker.finish_probe()
        self.feed(
            "\x1b[I\x1b[O",
            "\x1b_Gi=900,p=2;ENOENT:missing image\x1b\\",
            "\x1b_Gi=900;OK\x1b\\",
            "\x1b_Gi=2147515049;EINVAL:late probe\x1b\\",
        )
        self.assertEqual(focus, [])
        self.assertEqual(errors, [(900, "ENOENT:missing image")])
        self.assertEqual([key.key for key in self.keys], [Keys.Ignore, Keys.Ignore])
        for keypress in self.keys:
            self.assertTrue(self.broker.handle_control(keypress))
            self.assertTrue(self.broker.handle_control(keypress))
        self.assertEqual(focus, [True, False])

    def test_focus_controls_remain_literal_inside_paste(self):
        """Pasted focus-shaped data does not change the application's focus."""
        focus = []
        self.broker.on_focus = focus.append
        self.feed("\x1b[200~a\x1b[Ob\x1b[I\x1b[201~")
        self.assertEqual(len(self.keys), 1)
        self.assertEqual(self.keys[0].key, Keys.BracketedPaste)
        self.assertEqual(self.keys[0].data, "a\x1b[Ob\x1b[I")
        self.assertFalse(self.broker.handle_control(self.keys[0]))
        self.assertEqual(focus, [])

    def test_unadvertised_enhanced_key_reports_do_not_become_parameter_text(self):
        """Unsupported enhanced-key reports cannot turn into literal shell text."""
        self.feed("a\x1b[97;5:1ub\x1b[?1uc")
        self.assertEqual("".join(key.data for key in self.keys), "abc")

    def test_oversized_control_is_discarded_through_split_terminator(self):
        """A bounded parser discards the entire oversized control payload."""
        self.feed("\x1b]52;c;" + "x" * HostParser.MAX_CONTROL)
        self.feed("more\x1b")
        self.feed("\\after")
        self.assertEqual("".join(key.data for key in self.keys), "after")

    def test_host_identity_scrub_preserves_user_environment(self):
        """Only inherited outer-terminal identities are removed from a copy."""
        original = {
            "PATH": "/bin",
            "HOME": "/home/test",
            "KITTY_WINDOW_ID": "12",
            "WEZTERM_PANE": "4",
            "TMUX": "/tmp/tmux,12,0",
            "WT_SESSION": "host-session",
        }
        self.assertEqual(
            scrub_environment(original), {"PATH": "/bin", "HOME": "/home/test"}
        )
        self.assertEqual(original["KITTY_WINDOW_ID"], "12")

    def test_c1_controls_are_consumed_but_remain_literal_in_paste(self):
        """Both control encodings share the broker without rewriting paste data."""
        self.feed("\x9b6;20;10t\x9fGi=2147515049;OK\x9c")
        self.assertTrue(self.broker.finish_probe().kitty_graphics)
        self.assertEqual(self.keys, [])
        self.feed("\x1b[200~\x9b6;20;10t\x1b[201~")
        self.assertEqual(self.keys[0].key, Keys.BracketedPaste)
        self.assertEqual(self.keys[0].data, "\x9b6;20;10t")

    def test_link_policy_is_independent_of_host_identity(self):
        """Link output defaults on, while an explicit off survives host identification."""
        self.feed("\x1bP>|kitty(0.50.0)\x1b\\")
        self.assertTrue(self.broker.finish_probe().hyperlinks)
        other = HostBroker(features={"hyperlinks": "off"})
        other.begin_probe(80, 24)
        other.control("\x1bP>|XTerm(400)\x1b\\")
        self.assertFalse(other.finish_probe().hyperlinks)
        other.control("\x1bP>|kitty(0.50.0)\x1b\\")
        self.assertFalse(other.profile.hyperlinks)

    def test_clipboard_protocol_requires_report_and_rejects_late_upgrade(self):
        """Only a current positive DECRQM response authorizes Kitty transactions."""
        self.feed("\x1b[?5522;2$y")
        self.assertTrue(self.broker.finish_probe().clipboard_protocol)
        other = HostBroker()
        other.begin_probe(80, 24)
        other.finish_probe()
        other.control("\x1b[?5522;2$y")
        self.assertFalse(other.profile.clipboard_protocol)

    def test_host_policy_does_not_replace_protocol_observations(self):
        """A version cannot replace keyboard, graphics or clipboard support replies."""
        self.feed("\x1bP>|kitty(0.50.0)\x1b\\\x1b[6;20;10t")
        profile = self.broker.finish_probe()
        self.assertEqual(self.broker.adapter.name, "kitty")
        self.assertFalse(profile.enhanced_keyboard)
        self.assertFalse(profile.kitty_graphics)
        self.assertFalse(profile.clipboard_protocol)
        self.assertIsNone(profile.image_protocol)

        other = HostBroker()
        other.begin_probe(100, 30, query_id=2147515049)
        other.control("\x1bP>|unverified-terminal\x1b\\")
        other.control("\x1b[6;20;10t")
        other.control("\x1b_Gi=2147515049;OK\x1b\\")
        other.control("\x1b[?5522;2$y")
        profile = other.finish_probe()
        self.assertEqual(other.adapter.name, "unknown")
        self.assertTrue(profile.kitty_graphics)
        self.assertTrue(profile.clipboard_protocol)
        self.assertEqual(profile.image_protocol, "kitty")
        self.assertFalse(profile.enhanced_keyboard)
        self.assertTrue(profile.extended_styles)

    def test_host_names_only_select_quirks_and_never_image_protocols(self):
        """Version changes cannot replace feature policy and independent replies."""
        for version, name, image in (
            ("XTerm(407)", "xterm", "kitty"),
            ("XTerm(408)", "unknown", "kitty"),
            ("WezTerm 20240203-110809-5046fc22", "wezterm", "kitty"),
            ("WezTerm 20240203-110809-5046fc23", "unknown", "kitty"),
        ):
            with self.subTest(version=version):
                broker = HostBroker()
                broker.begin_probe(100, 30, query_id=2147515049)
                broker.control(f"\x1bP>|{version}\x1b\\")
                broker.control("\x1b[6;20;10t")
                broker.control("\x1b_Gi=2147515049;OK\x1b\\")
                profile = broker.finish_probe()
                self.assertEqual(broker.adapter.name, name)
                self.assertEqual(profile.image_protocol, image)

    def test_duplicate_and_cancelled_versions_cannot_change_host_policy(self):
        """Only the first in-flight identity counts; cancellation discards policy."""
        self.feed(
            "\x1bP>|XTerm(407)\x1b\\",
            "\x1bP>|WezTerm 20240203-110809-5046fc22\x1b\\",
            "\x1b[6;20;10t",
        )
        self.assertEqual(self.broker.adapter.name, "xterm")
        self.assertIsNone(self.broker.finish_probe().image_protocol)
        other = HostBroker()
        other.begin_probe(100, 30)
        other.control("\x1bP>|kitty(0.50.0)\x1b\\")
        other.cancel_probe()
        other.control("\x1bP>|XTerm(407)\x1b\\")
        other.refresh_geometry(100, 30, 1000, 600)
        self.assertEqual(other.adapter.name, "unknown")
        self.assertFalse(other.profile.enhanced_keyboard)
        self.assertIsNone(other.profile.image_protocol)

    def test_verified_xterm_remains_text_only_even_with_sixel_reports(self):
        """Retired SIXEL reports cannot activate any graphics backend."""
        self.feed(
            "\x1bP>|XTerm(407)\x1b\\\x1b[?64;1;2;4;6;9;15;17;18;21;22;28c\x1b[6;20;10t\x1b[?1;0;16S"
        )
        self.assertIsNone(self.broker.finish_probe().image_protocol)
        self.assertFalse(self.broker.profile.kitty_graphics)
        self.assertIsNone(self.broker.refresh_geometry(101, 30, 0, 0).image_protocol)
        self.assertIsNone(
            self.broker.refresh_geometry(101, 30, 1010, 600).image_protocol
        )

    def test_xterm_border_pixels_at_startup_do_not_revoke_explicit_size_reports(self):
        """Observed xterm ioctl border pixels cannot silence its correct CSI reports."""
        broker = HostBroker()
        broker.refresh_geometry(80, 24, 804, 484)
        broker.begin_probe(80, 24)
        broker.control("\x1b[6;20;10t")
        broker.control("\x1b[4;480;800t")
        broker.control("\x1b[?64;1;2;4;6;9;15;17;18;21;22;28c")
        broker.control("\x1bP>|XTerm(407)\x1b\\")
        broker.control("\x1b[?1;0;16S")
        self.assertIsNone(broker.finish_probe().image_protocol)
        self.assertEqual(
            (broker.profile.cell_width, broker.profile.cell_height), (10, 20)
        )
        self.assertIsNone(broker.refresh_geometry(80, 24, 804, 484).image_protocol)
        resized = broker.refresh_geometry(90, 25, 904, 504)
        self.assertIsNone(resized.image_protocol)
        self.assertEqual((resized.cell_width, resized.cell_height), (10, 20))
        font_changed = broker.refresh_geometry(90, 25, 1084, 604)
        self.assertEqual((font_changed.cell_width, font_changed.cell_height), (12, 24))
        self.assertIsNone(broker.refresh_geometry(90, 25, 1085, 604).image_protocol)

    def test_xterm_inset_calibration_rejects_ambiguous_or_stale_geometry(self):
        """Scrollbar-shaped, oversized and old-generation differences stay untrusted."""
        for kernel, resize in (
            ((818, 484), False),
            ((870, 550), False),
            ((804, 484), True),
        ):
            broker = HostBroker()
            broker.refresh_geometry(80, 24, *kernel)
            broker.begin_probe(80, 24)
            if resize:
                broker.refresh_geometry(81, 24, 814, 484)
            broker.control("\x1b[6;20;10t")
            broker.control("\x1b[4;480;800t")
            broker.control("\x1b[?64;4c")
            broker.control("\x1bP>|XTerm(407)\x1b\\")
            broker.control("\x1b[?1;0;16S")
            broker.finish_probe()
            profile = broker.refresh_geometry(90, 25, 904, 504)
            self.assertEqual((profile.cell_width, profile.cell_height), (0, 0))

    def test_xterm_inset_policy_is_not_applied_to_other_identified_hosts(self):
        """A similar kernel/query difference cannot authorize a generic host correction."""
        broker = HostBroker(features={"iterm_graphics": "on"})
        broker.refresh_geometry(80, 24, 804, 484)
        broker.begin_probe(80, 24)
        broker.control("\x1b[6;20;10t")
        broker.control("\x1b[4;480;800t")
        broker.control("\x1bP>|WezTerm 20240203-110809-5046fc22\x1b\\")
        self.assertEqual(broker.finish_probe().image_protocol, "iterm")
        self.assertIsNone(broker.refresh_geometry(90, 25, 904, 504).image_protocol)

    def test_xterm_graphics_stays_disabled_for_all_feature_reports(self):
        """Version names alone cannot authorize an unverified graphics adapter."""
        for version, attributes in (
            ("XTerm(408)", "?64;4c"),
            ("XTerm(407)", "?64;1;6c"),
        ):
            broker = HostBroker()
            broker.begin_probe(80, 24)
            broker.control("\x1bP>|" + version + "\x1b\\")
            broker.control("\x1b[" + attributes)
            broker.control("\x1b[6;20;10t")
            self.assertIsNone(broker.finish_probe().image_protocol)

    def test_explicit_iterm_selection_wins_without_a_host_name(self):
        """One measured image backend is selected, without fabricating a Kitty ACK."""
        broker = HostBroker(features={"iterm_graphics": "on"})
        broker.begin_probe(100, 30, query_id=2147515049)
        broker.control("\x1b[6;22;10t")
        broker.control("\x1b_Gi=2147515049;OK\x1b\\")
        self.assertEqual(broker.finish_probe().image_protocol, "iterm")
        self.assertFalse(broker.profile.kitty_graphics)
        self.assertEqual(broker.feature_status["kitty_graphics"].support, "supported")

    def test_retired_graphics_query_is_not_sent_and_late_reports_are_consumed(self):
        """No SIXEL negotiation remains, including accidental keyboard leakage."""
        self.assertNotIn("\x1b[?1;", self.query)
        self.feed("a\x1b[?1;0;256Sb")
        self.assertEqual([key.data for key in self.keys], ["a", "b"])

    def test_kernel_geometry_refresh_replaces_snapshots_and_never_guesses(self):
        """Unmeasured resize revokes placement, while exact kernel pixels restore it."""
        self.feed("\x1b[6;20;10t\x1b_Gi=2147515049;OK\x1b\\")
        original = self.broker.finish_probe()
        self.assertTrue(original.kitty_graphics)
        unavailable = self.broker.refresh_geometry(120, 40, 0, 0)
        self.assertFalse(unavailable.kitty_graphics)
        self.assertEqual((unavailable.cell_width, unavailable.cell_height), (0, 0))
        self.assertEqual(
            self.broker.graphics_reason, "cell-size-unavailable-after-resize"
        )
        self.assertTrue(original.kitty_graphics)
        restored = self.broker.refresh_geometry(120, 40, 1440, 960)
        self.assertTrue(restored.kitty_graphics)
        self.assertEqual((restored.cell_width, restored.cell_height), (12, 24))
        self.assertEqual(self.broker.graphics_reason, "")

    def test_startup_zero_pixel_ioctl_preserves_query_fallback(self):
        """A missing initial kernel size does not invalidate explicit terminal reports."""
        self.broker.refresh_geometry(100, 30, 0, 0)
        self.feed("\x1b[6;20;10t\x1b_Gi=2147515049;OK\x1b\\")
        profile = self.broker.finish_probe()
        self.assertTrue(profile.kitty_graphics)
        self.assertIs(self.broker.refresh_geometry(100, 30, 0, 0), profile)

    def test_resizing_during_probe_rejects_old_identity_free_size_reports(self):
        """An old size reply cannot supply geometry for a newly resized viewport."""
        self.broker.refresh_geometry(120, 40, 0, 0)
        self.feed("\x1b[6;20;10t\x1b[4;600;1000t\x1b_Gi=2147515049;OK\x1b\\")
        self.assertFalse(self.broker.finish_probe().kitty_graphics)

    def test_pixel_geometry_change_and_loss_are_observed_even_at_same_grid_size(self):
        """Font metrics are measured afresh instead of being scaled from old cells."""
        self.feed("\x1b_Gi=2147515049;OK\x1b\\")
        self.broker.refresh_geometry(100, 30, 1000, 600)
        self.assertTrue(self.broker.finish_probe().kitty_graphics)
        resized = self.broker.refresh_geometry(100, 30, 1200, 720)
        self.assertEqual((resized.cell_width, resized.cell_height), (12, 24))
        unavailable = self.broker.refresh_geometry(100, 30, 1201, 720)
        self.assertFalse(unavailable.kitty_graphics)

    def test_kernel_geometry_cannot_reauthorize_a_cancelled_probe(self):
        """Real geometry does not substitute for a cancelled graphics acknowledgement."""
        self.feed("\x1b_Gi=2147515049;OK\x1b\\")
        self.broker.cancel_probe()
        profile = self.broker.refresh_geometry(100, 30, 1000, 600)
        self.assertFalse(profile.kitty_graphics)
        self.assertEqual(self.broker.graphics_reason, "host-probe-cancelled")


@unittest.skipUnless(os.name == "posix", "The live adapter uses PTK's POSIX reader")
class HostInputTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the existing PTK descriptor, parser, and attachment together."""

    async def test_temporary_handoff_uses_initial_termios_not_cooked_defaults(self):
        """Non-default echo/flow/output settings survive suspend and final close."""
        master, slave = os.openpty()
        try:
            original = termios.tcgetattr(slave)
            original[0] &= ~termios.IXON
            original[1] &= ~termios.OPOST
            original[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, original)
            with os.fdopen(os.dup(slave), "r") as stream:
                source = create_input(stdin=stream)
                wrapped = HostInput(source, HostBroker())
                try:
                    with wrapped.raw_mode():
                        raw = termios.tcgetattr(slave)
                        self.assertNotEqual(raw, original)
                        with wrapped.cooked_mode():
                            self.assertEqual(termios.tcgetattr(slave), original)
                        self.assertEqual(termios.tcgetattr(slave), raw)
                    self.assertEqual(termios.tcgetattr(slave), original)
                finally:
                    wrapped.close()
        finally:
            os.close(master)
            os.close(slave)

    async def test_one_reader_delivers_keys_and_consumes_fragmented_control(self):
        """The real descriptor reader consumes responses before PTK key delivery."""
        with create_pipe_input() as source:
            broker = HostBroker()
            broker.begin_probe(80, 24, query_id=2147483701)
            input = HostInput(source, broker)
            self.assertEqual(input.fileno(), source.fileno())
            self.assertEqual(input.typeahead_hash(), source.typeahead_hash())
            received, changed = [], asyncio.Event()

            def read_input():
                received.extend(input.read_keys())
                changed.set()

            with input.raw_mode(), input.attach(read_input):
                source.send_text("a\x1b_Gi=2147483701;")
                await asyncio.wait_for(changed.wait(), 1)
                changed.clear()
                self.assertEqual(input.flush_keys(), [])
                source.send_text("OK\x1b\\\x1b[6;16;8tb")
                await asyncio.wait_for(changed.wait(), 1)
            self.assertEqual("".join(key.data for key in received), "ab")
            self.assertTrue(broker.finish_probe().kitty_graphics)
            with input.cooked_mode(), input.detach():
                pass

    async def test_real_reader_preserves_paste_as_one_event(self):
        """Reader fragmentation does not turn pasted escapes into host controls."""
        with create_pipe_input() as source:
            broker = HostBroker()
            input = HostInput(source, broker)
            source.send_text("\x1b[200~one\x1b]10;query-text\x07")
            self.assertEqual(input.read_keys(), [])
            source.send_text("\ntwo\x1b[201~")
            keys = input.read_keys()
            self.assertEqual(len(keys), 1)
            self.assertEqual(keys[0].key, Keys.BracketedPaste)
            self.assertEqual(keys[0].data, "one\x1b]10;query-text\x07\ntwo")

    async def test_structured_keys_share_real_reader_order_with_plain_text(self):
        """The callback runs at dispatch, after earlier ordinary keys in the same read."""
        with create_pipe_input() as source:
            delivered = []
            broker = HostBroker(
                on_key=lambda key: delivered.append((key.key, key.action)) or True
            )
            broker.begin_probe(80, 24)
            broker.control("\x1bP>|kitty(0.50.0)\x1b\\")
            broker.control("\x1b[?0u")
            broker.control("\x1b[?1016;2$y")
            broker.finish_probe()
            broker.enter_input_modes()
            input = HostInput(source, broker)
            source.send_text("a\x1b[99;5:1ub\x1b[99;5:3uc")
            keys = input.read_keys()
            self.assertEqual(delivered, [])
            for keypress in keys:
                if not broker.handle_key(keypress):
                    delivered.append(keypress.data)
            self.assertEqual(
                delivered, ["a", ("c", "press"), "b", ("c", "release"), "c"]
            )

    async def test_focus_shares_real_reader_order_with_keys_and_mouse(self):
        """A focus loss cannot overtake an earlier press in the same OS read."""
        with create_pipe_input() as source:
            delivered = []
            broker = HostBroker(
                on_key=lambda key: delivered.append((key.key, key.action)) or True,
                on_focus=lambda focused: delivered.append(("focus", focused)),
            )
            broker.begin_probe(80, 24)
            broker.control("\x1bP>|kitty(0.50.0)\x1b\\")
            broker.control("\x1b[?0u")
            broker.control("\x1b[?1016;2$y")
            broker.finish_probe()
            broker.enter_input_modes()
            input = HostInput(source, broker)
            source.send_text("a\x1b[99;5:1u\x1b[<0;3;2M\x1b[")
            first = input.read_keys()
            self.assertEqual(input.flush_keys(), [])
            source.send_text("Ob\x1b[I\x1b[99;5:3uc")
            keys = first + input.read_keys()
            self.assertEqual(delivered, [])
            for keypress in keys:
                if not broker.handle_control(keypress) and not broker.handle_key(
                    keypress
                ):
                    delivered.append(keypress.data)
            self.assertEqual(
                delivered,
                [
                    "a",
                    ("c", "press"),
                    "\x1b[<0;3;2M",
                    ("focus", False),
                    "b",
                    ("focus", True),
                    ("c", "release"),
                    "c",
                ],
            )

    async def test_clipboard_reply_waits_behind_earlier_keys_in_the_same_read(self):
        """Control replies that produce child input share the normal input queue."""
        with create_pipe_input() as source:
            delivered = []
            broker = HostBroker(
                on_clipboard_reply=lambda sequence: delivered.append("reply") or True
            )
            input = HostInput(source, broker)
            source.send_text("a\x1b]5522;type=read:status=DONE:id=old\x1b\\b")
            keys = input.read_keys()
            self.assertEqual(delivered, [])
            for key in keys:
                if not broker.handle_control(key):
                    delivered.append(key.data)
            self.assertEqual(delivered, ["a", "reply", "b"])

    async def test_real_reader_preserves_pixel_metadata_and_ptk_cell_coordinates(self):
        """Mouse metadata is queued with surrounding input instead of dispatched inline."""
        with create_pipe_input() as source:
            broker = HostBroker()
            broker.begin_probe(100, 30)
            broker.control("\x1bP>|kitty(0.50.0)\x1b\\")
            broker.control("\x1b[?0u")
            broker.control("\x1b[?1016;2$y")
            broker.control("\x1b[6;20;10t")
            broker.finish_probe()
            broker.enter_input_modes()
            input = HostInput(source, broker)
            source.send_text("a\x1b[<0;947;")
            first = input.read_keys()
            self.assertEqual([key.data for key in first], ["a"])
            source.send_text("433Mb")
            remaining = input.read_keys()
            self.assertEqual([key.data for key in remaining], ["\x1b[<0;95;22M", "b"])
            self.assertEqual(remaining[0].pixel_position, (947, 433))

    async def test_host_pixel_wire_round_trips_through_real_ptk_and_native_encoder(
        self,
    ):
        """Forward physical pixels exactly rather than synthesizing a cell centre."""
        with (
            create_pipe_input() as source,
            Terminal(100, 30, lambda _: None) as terminal,
        ):
            broker = HostBroker()
            broker.begin_probe(100, 30)
            broker.control("\x1bP>|kitty(0.50.0)\x1b\\")
            broker.control("\x1b[?0u")
            broker.control("\x1b[?1016;2$y")
            broker.control("\x1b[6;20;10t")
            broker.finish_probe()
            broker.enter_input_modes()
            input = HostInput(source, broker)
            terminal.resize(100, 30, cell_width=10, cell_height=20)
            terminal.feed(b"\x1b[?1003h\x1b[?1016h")
            source.send_text("\x1b[<48;947;433M")
            keypress = input.read_keys()[0]
            event = decode_mouse(keypress.data)
            encoded = terminal.encode_mouse(
                event.action,
                event.button,
                event.x,
                event.y,
                event.modifiers,
                pixel_position=keypress.pixel_position,
            )
            self.assertEqual(encoded, b"\x1b[<48;947;433M")


@unittest.skipUnless(os.name == "posix", "The native renderer uses POSIX VT input")
class HostRendererTests(unittest.IsolatedAsyncioTestCase):
    """Validate input-mode restoration against PTK's actual screen lifecycle."""

    async def test_input_modes_restore_before_leaving_alternate_screen(self):
        """A screen-local keyboard stack must be popped on its original screen."""
        from ish.terminal.app import TerminalApp

        stream = io.StringIO()
        output = Vt100_Output(
            stream,
            lambda: Size(rows=6, columns=12),
            term="xterm-256color",
            enable_cpr=False,
        )
        with create_pipe_input() as source:
            with create_app_session(input=source, output=output):
                with patch("ish.terminal.app.create_input", return_value=source):
                    owner = TerminalApp(
                        "/bin/sh", Path(__file__).resolve().parents[1], mouse="on"
                    )
            try:
                owner.host.begin_probe(12, 6)
                owner.host.control("\x1bP>|kitty(0.50.0)\x1b\\")
                owner.host.control("\x1b[?0u")
                owner.host.control("\x1b[?1016;2$y")
                owner.host.control("\x1b[6;20;10t")
                owner.host.finish_probe()
                owner.presentation_ready = True
                with set_app(owner.app):
                    owner.app.renderer.render(owner.app, owner.app.layout)
                owner.app.renderer.enter_input_modes()
                output.flush()
                start = stream.tell()
                owner.app.renderer.reset()
                output.flush()
                reset_wire = stream.getvalue()[start:]
                for sequence in ("\x1b[<u", "\x1b[?1016l", "\x1b[?1004l"):
                    # Pixel reset exits our encoding before screen departure;
                    # the observed initial reset is restored after PTK cleanup.
                    self.assertEqual(
                        reset_wire.count(sequence),
                        2 if sequence == "\x1b[?1016l" else 1,
                    )
                    self.assertLess(
                        reset_wire.index(sequence), reset_wire.index("\x1b[?1049l")
                    )
                owner.app.renderer.leave_input_modes()
                output.flush()
                self.assertEqual(stream.getvalue()[start:].count("\x1b[<u"), 1)
                self.assertFalse(owner.host.input_modes_active)
                self.assertFalse(owner.host.pixel_mouse_active)
            finally:
                await owner.plugins.close()
                await owner.session.close()
                owner.vt.close()


class HostPixelMouseTests(unittest.TestCase):
    """Keep host pixels exact while exposing physical cell positions to PTK widgets."""

    def setUp(self):
        """Observe keyboard/pixel support and independently reported 10 by 20 cells."""
        self.broker = HostBroker()
        self.broker.begin_probe(100, 30)
        self.broker.control("\x1bP>|kitty(0.50.0)\x1b\\")
        self.broker.control("\x1b[?0u")
        self.broker.control("\x1b[?1016;2$y")
        self.broker.control("\x1b[6;20;10t")
        self.broker.finish_probe()
        self.keys = []
        self.parser = HostParser(Vt100Parser(self.keys.append), self.broker)

    def test_pixel_mode_is_enabled_after_cell_mode_and_reset_explicitly(self):
        """Pixel format is an independent capability with explicit setup and cleanup."""
        self.assertTrue(self.broker.profile.pixel_mouse)
        self.assertEqual(self.broker.enter_input_modes(), "\x1b[>3u\x1b[?1016h")
        self.assertEqual(self.broker.enter_input_modes(), "")
        self.assertEqual(self.broker.leave_input_modes(), "\x1b[?1016l\x1b[<u")

    def test_pixel_edges_and_original_positions_are_not_rounded_for_the_child(self):
        """Literal wire pixels map to expected one-based PTK cell reports."""
        self.broker.enter_input_modes()
        cases = (
            ("\x1b[<0;1;1M", "\x1b[<0;1;1M", (1, 1)),
            ("\x1b[<0;10;20M", "\x1b[<0;1;1M", (10, 20)),
            ("\x1b[<0;11;21M", "\x1b[<0;2;2M", (11, 21)),
            ("\x1b[<48;947;433M", "\x1b[<48;95;22M", (947, 433)),
            ("\x1b[<0;947;433m", "\x1b[<0;95;22m", (947, 433)),
            ("\x1b[<64;947;433M", "\x1b[<64;95;22M", (947, 433)),
        )
        for wire, cell_wire, position in cases:
            with self.subTest(wire=wire):
                self.parser.feed(wire)
                key = self.keys.pop()
                self.assertEqual(key.key, Keys.Vt100MouseEvent)
                self.assertEqual(key.data, cell_wire)
                self.assertEqual(key.pixel_position, position)

    def test_normalized_mouse_matches_existing_popup_coordinate_contract(self):
        """The standard mouse decoder sees cells while the original pixels stay attached."""
        self.broker.enter_input_modes()
        self.parser.feed("\x1b[<16;947;433M")
        event = decode_mouse(self.keys[0].data)
        self.assertEqual((event.x, event.y, event.modifiers), (94, 21, 2))
        self.assertEqual(self.keys[0].pixel_position, (947, 433))

    def test_geometry_loss_drops_pending_pixel_events_and_restores_cell_encoding(self):
        """No guessed geometry is used between resize detection and mode restoration."""
        self.broker.enter_input_modes()
        self.broker.refresh_geometry(120, 40, 0, 0)
        self.assertFalse(self.broker.profile.pixel_mouse)
        self.parser.feed("\x1b[<0;947;433M")
        self.assertEqual(self.keys, [])
        self.assertEqual(self.broker.enter_input_modes(), "\x1b[?1016l\x1b[?1006h")
        self.parser.feed("\x1b[<0;4;8M")
        self.assertEqual(self.keys[0].data, "\x1b[<0;4;8M")
        self.assertFalse(hasattr(self.keys[0], "pixel_position"))

    def test_invalid_pixel_reports_and_paste_never_trigger_mouse_actions(self):
        """Zero coordinates, malformed buttons and mouse-shaped paste stay separate."""
        self.broker.enter_input_modes()
        self.parser.feed("\x1b[<0;0;1M\x1b[<255;1;1M")
        self.assertEqual(self.keys, [])
        self.parser.feed("\x1b[200~\x1b[<0;947;433M\x1b[201~")
        self.assertEqual(self.keys[0].key, Keys.BracketedPaste)
        self.assertEqual(self.keys[0].data, "\x1b[<0;947;433M")
        self.assertFalse(hasattr(self.keys[0], "pixel_position"))


class HostKeyboardTests(unittest.TestCase):
    """Map a bounded enhanced-key subset without bypassing PTK's ordered queue."""

    def setUp(self):
        """Enable the protocol only after its literal status reply."""
        self.broker = HostBroker()
        self.broker.begin_probe(80, 24)
        self.broker.control("\x1bP>|kitty(0.50.0)\x1b\\")
        self.broker.control("\x1b[?0u")
        self.broker.control("\x1b[?1016;2$y")
        self.broker.finish_probe()
        self.keys = []
        self.parser = HostParser(Vt100Parser(self.keys.append), self.broker)

    def test_mode_push_and_pop_are_explicit_balanced_and_host_gated(self):
        """Only the caller writes a supported mode change and each push has one pop."""
        self.assertEqual(self.broker.enter_input_modes(), "\x1b[>3u")
        self.assertEqual(self.broker.enter_input_modes(), "")
        self.assertEqual(self.broker.leave_input_modes(), "\x1b[<u")
        self.assertEqual(self.broker.leave_input_modes(), "")
        unknown = HostBroker()
        unknown.finish_probe()
        self.assertEqual(unknown.enter_input_modes(), "")
        self.assertFalse(unknown.profile.enhanced_keyboard)
        self.assertTrue(unknown.profile.extended_styles)

    def test_queued_keys_keep_the_release_contract_observed_at_receipt(self):
        """Negotiation between receipt and PTK dispatch cannot change a key's source."""
        received = []
        self.broker.on_key = lambda key: received.append(key) or True
        self.parser.feed("\x1b[A")
        self.broker.enter_input_modes()
        self.broker.handle_key(self.keys.pop())
        self.parser.feed("\x1b[A")
        self.broker.leave_input_modes()
        self.broker.handle_key(self.keys.pop())
        self.assertEqual(
            received,
            [
                StructuredKey("up", 57352),
                StructuredKey("up", 57352, release_events=True),
            ],
        )

    def test_wire_modifiers_actions_and_alternates_remain_explicit(self):
        """Normalize modifier bits while retaining observed logical key alternatives."""
        self.assertEqual(
            decode_key("\x1b[97:65:113;6:2;65u"),
            StructuredKey("a", 97, 3, "repeat", "A", 65, 113),
        )
        self.assertEqual(
            decode_key("\x1b[13;5:3~"),
            StructuredKey("f3", 57366, 2, "release"),
        )

    def test_popup_fallback_preserves_control_shift_and_named_key_meaning(self):
        """Popup widgets see existing PTK editing keys and ignore release events."""
        self.broker.enter_input_modes()
        self.parser.feed("\x1b[99;5u\x1b[9;2u\x1b[1;6:2B\x1b[1;6:3B")
        self.assertEqual(
            [key.key for key in self.keys],
            [Keys.ControlC, Keys.BackTab, Keys.ControlShiftDown, Keys.Ignore],
        )
        self.assertEqual(self.keys[0].data, "\x03")
        self.assertEqual(self.keys[2].structured_key.action, "repeat")

    def test_keypad_identity_survives_ordinary_popup_editing_fallback(self):
        """The child retains physical keypad identities while popup widgets use PTK keys."""
        self.broker.enter_input_modes()
        self.parser.feed("\x1b[57414u\x1b[57417;5:2u")
        self.assertEqual(
            [key.key for key in self.keys], [Keys.ControlM, Keys.ControlLeft]
        )
        self.assertEqual(self.keys[0].data, "\r")
        self.assertEqual(
            self.keys[0].structured_key,
            StructuredKey("kpenter", 57414, release_events=True),
        )
        self.assertEqual(
            self.keys[1].structured_key,
            StructuredKey("kpleft", 57417, 2, "repeat", release_events=True),
        )

    def test_alt_fallback_is_one_native_dispatch_and_two_legacy_popup_keys(self):
        """A legacy Escape prefix cannot deliver the structured native key twice."""
        self.broker.enter_input_modes()
        self.parser.feed("\x1b[1;3:1A")
        self.assertEqual([key.key for key in self.keys], [Keys.Escape, Keys.Up])
        received = []
        self.broker.on_key = lambda key: received.append(key) or True
        self.assertEqual(
            [self.broker.handle_key(key) for key in self.keys], [True, True]
        )
        self.assertEqual(received, [StructuredKey("up", 57352, 4, release_events=True)])

    def test_ui_consumption_claims_all_tokens_of_one_structured_alt_event(self):
        """An Escape shortcut cannot leak its Alt sibling after changing focus."""
        self.broker.enter_input_modes()
        delivered = []
        self.broker.on_key = lambda event: delivered.append(event) or True
        self.parser.feed("\x1b[120;3u")
        self.assertEqual([key.key for key in self.keys], [Keys.Escape, "x"])
        self.assertFalse(any(self.broker.key_consumed(key) for key in self.keys))
        self.assertTrue(self.broker.consume_key(self.keys[0]))
        self.assertTrue(all(self.broker.key_consumed(key) for key in self.keys))
        self.assertTrue(self.broker.handle_key(self.keys[1]))
        self.assertEqual(delivered, [])
        self.parser.feed("y")
        self.assertFalse(self.broker.key_consumed(self.keys[-1]))
        self.assertFalse(self.broker.consume_key(self.keys[-1]))

    def test_unknown_extended_reports_are_consumed_without_fake_typing(self):
        """Unsupported keys, invalid Unicode and unhandled modifiers cannot leak numbers."""
        self.broker.enter_input_modes()
        self.parser.feed(
            "a\x1b[57428u\x1b[97;9u\x1b[55296u\x1b[1114112u"
            "\x1b[97;1:4u\x1b[27;5;99~\x1b[1;2:3Xb"
        )
        self.assertEqual("".join(key.data for key in self.keys), "ab")

    def test_identified_key_mode_preserves_even_escape_only_reply_fragments(self):
        """Disambiguated Escape permits withholding a lone protocol introducer."""
        self.broker.enter_input_modes()
        self.parser.feed("\x1b")
        self.parser.flush()
        self.assertEqual(self.keys, [])
        self.parser.feed("_Gi=42;OK\x1b\\\x1b[27u")
        self.assertEqual([key.key for key in self.keys], [Keys.Escape])
        self.assertEqual(self.keys[0].structured_key.key, "escape")

    def test_fragmented_enhanced_keys_keep_actions_without_inline_callbacks(self):
        """Every control split yields one ordered event rather than partial key text."""
        received = []
        self.broker.on_key = lambda key: received.append(key) or True
        self.broker.enter_input_modes()
        for character in "\x1b[97;5:2u":
            self.parser.feed(character)
            self.parser.flush()
        self.assertEqual(received, [])
        self.assertEqual(len(self.keys), 1)
        self.assertTrue(self.broker.handle_key(self.keys[0]))
        self.assertEqual(
            received, [StructuredKey("a", 97, 2, "repeat", release_events=True)]
        )

    def test_pasted_csi_u_and_ordinary_unicode_remain_unstructured_text(self):
        """A paste does not become physical events even when enhanced mode is enabled."""
        self.broker.enter_input_modes()
        self.parser.feed("한\x1b[200~\x1b[99;5u\x1b[201~글")
        self.assertEqual(
            [key.key for key in self.keys], ["한", Keys.BracketedPaste, "글"]
        )
        self.assertEqual(self.keys[1].data, "\x1b[99;5u")
        self.assertTrue(all(not hasattr(key, "structured_key") for key in self.keys))


class HostAppearanceTests(unittest.TestCase):
    """Color observations and lifecycle operations have explicit wire boundaries."""

    def setUp(self):
        """Use the existing parser and dispatch control metadata like Application."""
        self.changes, self.keys = [], []
        self.broker = HostBroker(on_appearance=self.changes.append)
        self.parser = HostParser(Vt100Parser(self.keys.append), self.broker)
        self.broker.begin_probe(80, 24)

    def dispatch(self, wire):
        """Return only ordinary keys after processing queued host controls."""
        self.parser.feed(wire)
        self.parser.flush()
        typed = []
        for key in self.keys:
            if not self.broker.handle_control(key):
                typed.append(key)
        self.keys.clear()
        return typed

    def test_full_palette_and_defaults_are_observed_without_inline_callbacks(self):
        """All 256 response indices retain their actual RGB and missing means unknown."""
        self.parser.feed(
            "\x1b]10;rgb:ffff/8080/0000\x1b\\"
            "\x1b]11;rgb:1/2/f\x07\x1b]12;rgb:abc/000/fff\x07"
            "\x1b[?997;1n"
        )
        self.assertEqual(self.broker.profile.appearance, HostAppearance())
        self.assertEqual(self.changes, [])
        for key in self.keys:
            self.assertTrue(self.broker.handle_control(key))
        self.keys.clear()
        wire = "".join(
            f"\x1b]4;{index};rgb:{index:02x}/00/ff\x1b\\" for index in range(256)
        )
        self.assertEqual(self.dispatch(wire), [])
        profile = self.broker.finish_probe()
        self.assertEqual(profile.appearance.foreground, (255, 128, 0))
        self.assertEqual(profile.appearance.background, (17, 34, 255))
        self.assertEqual(profile.appearance.cursor, (171, 0, 255))
        self.assertEqual(profile.appearance.scheme, "dark")
        self.assertEqual(
            profile.appearance.palette, tuple((index, 0, 255) for index in range(256))
        )
        self.assertIs(profile.appearance, profile.initial_appearance)
        self.assertEqual(self.changes, [])

    def test_invalid_duplicate_and_late_colors_do_not_replace_facts(self):
        """Unrequested indices and invalid records cannot partly mutate a palette."""
        self.dispatch("\x1b]4;3;rgb:11/22/33\x07")
        self.dispatch("\x1b]4;3;rgb:ff/ff/ff;4;rgb:aa/bb/cc\x07")
        self.dispatch("\x1b]4;5;rgb:11/22/33;256;rgb:11/22/33\x07")
        self.dispatch("\x1b]10;not-rgb\x07\x1b]11;rgb:1/2/3;bad\x07")
        initial = self.broker.finish_probe()
        self.assertEqual(initial.appearance.palette[3], (17, 34, 51))
        self.assertEqual(initial.appearance.palette[4], (170, 187, 204))
        self.assertIsNone(initial.appearance.palette[5])
        self.assertIsNone(initial.appearance.foreground)
        self.assertIsNone(initial.appearance.background)
        self.dispatch("\x1b]10;rgb:ff/ff/ff\x07\x1b]4;5;rgb:ff/ff/ff\x07")
        self.assertIs(self.broker.profile, initial)

    def test_queued_old_colors_cannot_cross_probe_or_cancellation_boundary(self):
        """Parsing before a deadline does not authorize processing after it closes."""
        for cancel in (False, True):
            broker, keys = HostBroker(), []
            parser = HostParser(Vt100Parser(keys.append), broker)
            broker.begin_probe(80, 24)
            parser.feed("\x1b]10;rgb:ff/ff/ff\x07\x1b[?997;1n")
            (broker.cancel_probe if cancel else broker.finish_probe)()
            for key in keys:
                self.assertTrue(broker.handle_control(key))
            self.assertEqual(broker.profile.appearance, HostAppearance())

    def test_notifications_invalidate_colors_without_reusing_initial_query(self):
        """A subscribed theme event provides its scheme, not a new RGB snapshot."""
        self.dispatch(
            "\x1bP>|kitty(0.50.0)\x1b\\\x1b[?2031;2$y\x1b[?997;1n"
            "\x1b]10;rgb:11/22/33\x07\x1b]4;0;rgb:00/00/00\x07"
        )
        initial = self.broker.finish_probe().appearance
        self.assertEqual(self.broker.enter_metadata_modes(), "\x1b[?2031h")
        self.assertEqual(self.broker.enter_metadata_modes(), "")
        self.parser.feed("a\x1b[?997;2nb")
        self.assertEqual(self.changes, [])
        order = []
        self.broker.on_appearance = lambda appearance: order.append(appearance.scheme)
        for key in self.keys:
            if not self.broker.handle_control(key):
                order.append(key.data)
        self.keys.clear()
        self.assertEqual(order, ["a", "light", "b"])
        self.assertEqual(self.broker.profile.appearance, HostAppearance(scheme="light"))
        self.assertIs(self.broker.profile.initial_appearance, initial)
        self.dispatch("\x1b]10;rgb:44/55/66\x07")
        self.assertIsNone(self.broker.profile.appearance.foreground)
        self.assertEqual(self.broker.leave_metadata_modes(), "\x1b[?2031l")
        self.assertEqual(self.broker.leave_metadata_modes(), "")
        self.dispatch("\x1b[?997;1n")
        self.assertEqual(self.broker.profile.appearance.scheme, "light")

    def test_missing_initial_scheme_never_turns_a_late_reply_into_an_event(self):
        """A late identity-free initial scheme reply cannot enable notifications."""
        self.dispatch("\x1b[?2031;2$y")
        self.broker.finish_probe()
        self.assertFalse(self.broker.profile.scheme_notifications)
        self.assertEqual(self.broker.enter_metadata_modes(), "")
        self.dispatch("\x1b[?997;1n")
        self.assertEqual(self.changes, [])
        self.assertIsNone(self.broker.profile.appearance.scheme)

    def test_originally_enabled_notification_mode_is_not_disabled_on_exit(self):
        """Owned cleanup restores the prior setting rather than assuming reset."""
        self.dispatch("\x1b[?2031;1$y\x1b[?997;1n")
        self.broker.finish_probe()
        self.assertEqual(self.broker.enter_metadata_modes(), "")
        self.assertEqual(self.broker.leave_metadata_modes(), "")

    def test_dynamic_cursor_is_not_misrepresented_as_fixed_black(self):
        """The richer observed reply supersedes Kitty's legacy black placeholder."""
        self.dispatch(
            "\x1bP>|kitty(0.50.0)\x1b\\\x1b]12;rgb:0000/0000/0000\x07"
            "\x1b]21;cursor=\x1b\\\x1b]12;rgb:ffff/ffff/ffff\x07"
        )
        profile = self.broker.finish_probe()
        self.assertTrue(profile.cursor_color_control)
        self.assertTrue(profile.initial_appearance.cursor_dynamic)
        self.assertIsNone(profile.initial_appearance.cursor)
        self.assertEqual(
            self.broker.set_cursor_color((1, 2, 3)), "\x1b]12;rgb:01/02/03\x1b\\"
        )
        self.assertEqual(self.broker.restore_cursor(), "\x1b]21;cursor=\x1b\\")
        with self.assertRaises(ValueError):
            self.broker.set_cursor_color((1, 2, 256))

    def test_metadata_lifecycle_does_not_query_save_or_restore_titles(self):
        """Title persistence is a shared policy, independent of host capability."""
        for version, overline in (
            ("kitty(0.50.0)", True),
            ("XTerm(407)", True),
            ("WezTerm 20240203-110809-5046fc22", True),
            ("XTerm(999)", True),
        ):
            with self.subTest(version=version):
                broker = HostBroker()
                self.assertNotIn("\x1b[21t", broker.begin_probe(80, 24))
                broker.control(f"\x1bP>|{version}\x1b\\")
                self.assertTrue(broker.control("\x1b]l원래 제목\x1b\\"))
                profile = broker.finish_probe()
                self.assertEqual(profile.overline, overline)
                self.assertEqual(broker.enter_metadata_modes(), "")
                self.assertEqual(broker.leave_metadata_modes(), "")
                self.assertEqual(broker.leave_metadata_modes(), "")

    def test_unsolicited_title_replies_never_become_input_or_restore_output(self):
        """Former title replies are discarded before and after initial observation."""
        self.dispatch("\x1b]lbad\x00title\x07")
        self.dispatch("\x1b]l" + "x" * 1025 + "\x07")
        self.broker.finish_probe()
        self.dispatch("\x1b]llate\x07")
        self.assertEqual(self.keys, [])
        self.assertEqual(self.broker.enter_metadata_modes(), "")
        self.assertEqual(self.broker.leave_metadata_modes(), "")

    def test_initial_appearance_can_complete_probe_without_waiting_for_a_deadline(self):
        """Readiness here is complete optional observations, never shell readiness."""
        self.dispatch(
            "\x1b[6;20;10t\x1b[?2026;0$y\x1b[?5522;0$y\x1b[?2031;0$y"
            "\x1b[?80;0$y\x1b[?62;6c"
            "\x1bP>|XTerm(407)\x1b\\\x1b[?0u"
            + f"\x1b_Gi={self.broker._query_id};EINVAL:unsupported\x1b\\"
            + "\x1b]10;rgb:ff/ff/ff;rgb:00/00/00;rgb:ff/ff/ff\x07"
            + "".join(f"\x1b]4;{index};rgb:00/00/00\x07" for index in range(256))
            + "".join(
                f"\x1b[?{mode};0$y"
                for mode in (
                    1,
                    6,
                    7,
                    12,
                    25,
                    1000,
                    1002,
                    1003,
                    1004,
                    1005,
                    1006,
                    1015,
                    1016,
                    2004,
                )
            )
            + "\x1bP0$rm\x1b\\\x1bP0$r q\x1b\\"
        )
        self.assertTrue(self.broker.ready.is_set())
        self.assertIsNone(self.broker.finish_probe().appearance.scheme)

    def test_cursor_control_requires_observed_restoration_and_known_host(self):
        """An unknown original color does not authorize changing the host cursor."""
        self.dispatch("\x1bP>|XTerm(407)\x1b\\")
        self.broker.finish_probe()
        self.assertFalse(self.broker.profile.cursor_color_control)
        self.assertEqual(self.broker.set_cursor_color((1, 2, 3)), "")
        self.assertEqual(self.broker.restore_cursor(), "")

    def test_kitty_cursor_requires_dynamic_state_observation(self):
        """A missing OSC21 observation must not turn dynamic cursor into fixed black."""
        self.dispatch("\x1bP>|kitty(0.50.0)\x1b\\\x1b]12;rgb:00/00/00\x07")
        profile = self.broker.finish_probe()
        self.assertIsNone(profile.appearance.cursor)
        self.assertIsNone(profile.appearance.cursor_dynamic)
        self.assertFalse(profile.cursor_color_control)
        self.assertEqual(self.broker.set_cursor_color((1, 2, 3)), "")

    def test_control_shaped_paste_and_fragmented_response_have_different_owners(self):
        """Every split preserves adjacent keys; explicit paste remains literal."""
        response = "\x1b]10;rgb:1111/2222/3333\x1b\\"
        for split in range(2, len(response)):
            broker, keys = HostBroker(), []
            broker.begin_probe(80, 24)
            parser = HostParser(Vt100Parser(keys.append), broker)
            parser.feed("a" + response[:split])
            parser.flush()
            parser.feed(response[split:] + "b")
            typed = [key.data for key in keys if not broker.handle_control(key)]
            self.assertEqual(typed, ["a", "b"])
            self.assertEqual(broker.finish_probe().appearance.foreground, (17, 34, 51))
        typed = self.dispatch("\x1b[200~" + response + "\x1b[?997;1n\x1b[201~")
        self.assertEqual(len(typed), 1)
        self.assertEqual(typed[0].key, Keys.BracketedPaste)
        self.assertEqual(self.broker.finish_probe().appearance, HostAppearance())


@unittest.skipUnless(os.name == "posix", "The adapter uses PTK's POSIX reader")
class HostAppearanceReaderTests(unittest.TestCase):
    """Exercise appearance and typing through the actual single pipe input reader."""

    def test_scheme_notifications_keep_reader_order_with_typing_and_paste(self):
        """No async callback can overtake queued text or steal a paste payload."""
        observed = []
        broker = HostBroker(on_appearance=lambda value: observed.append(value.scheme))
        broker.begin_probe(80, 24)
        with create_pipe_input() as pipe:
            source = HostInput(pipe, broker)
            pipe.send_text("\x1b[?2031;2$y\x1b[?997;1n\x1b]10;rgb:12/34")
            for key in source.read_keys():
                self.assertTrue(broker.handle_control(key))
            self.assertEqual(source.flush_keys(), [])
            pipe.send_text("/56\x07")
            for key in source.read_keys():
                self.assertTrue(broker.handle_control(key))
            self.assertEqual(broker.finish_probe().appearance.foreground, (18, 52, 86))
            self.assertEqual(broker.enter_metadata_modes(), "\x1b[?2031h")
            pipe.send_text("a\x1b[?997;")
            keys = source.read_keys()
            self.assertEqual(observed, [])
            pipe.send_text("2nb\x1b[200~\x1b[?997;1n\x1b[201~")
            keys += source.read_keys()
            for key in keys:
                if not broker.handle_control(key):
                    observed.append(key.data)
            self.assertEqual(observed, ["a", "light", "b", "\x1b[?997;1n"])
            pipe.send_text("\x1b[?997;1n")
            stale = source.read_keys()
            broker.leave_metadata_modes()
            broker.enter_metadata_modes()
            for key in stale:
                self.assertTrue(broker.handle_control(key))
            self.assertEqual(broker.profile.appearance.scheme, "light")
            self.assertEqual(observed, ["a", "light", "b", "\x1b[?997;1n"])


class HostHarnessTests(unittest.TestCase):
    """Actual-host evidence must fail on restoration or initialization errors."""

    def test_unrestored_host_cannot_publish_a_passed_result(self):
        """Successful native files cannot hide a failed host terminal restoration."""
        import terminal_host

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw-sizes").write_text("[[80, 24]]")
            terminal = SimpleNamespace(
                status=0,
                root=root,
                sizes=[[80, 24]],
                fd=5,
                attrs=["child saved"],
                saved_host=["host saved"],
                close=Mock(),
            )
            scenario = SimpleNamespace(run=Mock(), files=[])
            with (
                patch.object(terminal_host, "HostTerminal", return_value=terminal),
                patch.object(terminal_host, "Scenario", return_value=scenario),
                patch.object(
                    terminal_host.termios,
                    "tcgetattr",
                    side_effect=lambda fd: (
                        ["host changed"] if fd == 0 else ["child saved"]
                    ),
                ),
            ):
                result = terminal_host.HostRun(root, "bash", False).run()
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["host_termios_restored"])
            self.assertEqual(
                json.loads((root / "result.json").read_text())["status"], "failed"
            )
            terminal.close.assert_called_once()

    def test_host_initialization_error_restores_and_closes_the_created_pty(self):
        """Failure after spawning must clean up even before HostRun owns the object."""
        import terminal_host

        def spawned(owner, root, **kwargs):
            owner.output = bytearray(b"captured output")
            owner.pid = 123

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(terminal_host.HooklessTerminal, "__init__", spawned),
                patch.object(terminal_host.HooklessTerminal, "close") as close,
                patch.object(
                    terminal_host.termios, "tcgetattr", return_value=["saved"]
                ),
                patch.object(
                    terminal_host.termios, "tcgetwinsize", return_value=(24, 80)
                ),
                patch.object(terminal_host.termios, "tcsetattr") as restore,
                patch.object(terminal_host.signal, "signal"),
                patch.object(
                    terminal_host.tty, "setraw", side_effect=OSError("raw mode failed")
                ),
                self.assertRaisesRegex(OSError, "raw mode failed"),
            ):
                terminal_host.HostTerminal(root, shell="bash", plugins=False)
            close.assert_called_once()
            restore.assert_called_once_with(0, terminal_host.termios.TCSANOW, ["saved"])
            self.assertEqual((root / "output.bin").read_bytes(), b"captured output")


class HostFeatureTests(unittest.TestCase):
    """Separate feature choices from replies and mandatory presentation facts."""

    def probe(self, features=None, *replies):
        """Use literal host replies through the actual framed input dispatch."""
        broker = HostBroker(features=features)
        broker.begin_probe(80, 24, query_id=2147515049)
        keys = []
        parser = HostParser(Vt100Parser(keys.append), broker)
        for reply in replies:
            parser.feed(reply)
        parser.flush()
        for key in keys:
            self.assertTrue(broker.handle_control(key))
        broker.finish_probe()
        return broker

    def test_policy_is_copied_and_unqueried_features_never_claim_support(self):
        """Changing the caller's dictionary cannot silently change a running profile."""
        choices = {"hyperlinks": "off"}
        broker = HostBroker(features=choices)
        choices["hyperlinks"] = "on"
        with self.assertRaises(TypeError):
            broker.features["hyperlinks"] = "on"
        broker.begin_probe(80, 24)
        broker.control("\x1bP>|kitty(0.50.0)\x1b\\")
        broker.finish_probe()
        state = broker.feature_status
        self.assertEqual(
            (
                state["hyperlinks"].policy,
                state["hyperlinks"].support,
                state["hyperlinks"].active,
                state["hyperlinks"].reason,
            ),
            ("off", "unqueried", False, "policy-off"),
        )
        self.assertEqual(state["extended_styles"].support, "unqueried")
        self.assertTrue(state["extended_styles"].active)
        self.assertEqual(state["enhanced_keyboard"].support, "unknown")
        self.assertFalse(state["enhanced_keyboard"].active)

    def test_auto_requires_independent_protocol_replies_without_a_host_name(self):
        """Queried features stay unknown until their exact protocol evidence arrives."""
        names = (
            "kitty_graphics",
            "synchronized_output",
            "enhanced_keyboard",
            "pixel_mouse",
            "clipboard_protocol",
            "scheme_notifications",
        )
        unknown = self.probe()
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(unknown.feature_status[name].support, "unknown")
                self.assertFalse(unknown.feature_status[name].active)
        known = self.probe(
            None,
            "\x1b[6;20;10t\x1b_Gi=2147515049;OK\x1b\\",
            "\x1b[?2026;2$y\x1b[?0u\x1b[?1016;2$y\x1b[?5522;2$y",
            "\x1b[?2031;2$y\x1b[?997;1n",
        )
        self.assertEqual(known.adapter.name, "unknown")
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(known.feature_status[name].support, "supported")
                self.assertTrue(known.feature_status[name].active)

    def test_on_retains_negative_evidence_and_does_not_create_geometry(self):
        """A forced protocol is a request, never a forged positive observation."""
        broker = self.probe(
            {
                "kitty_graphics": "on",
                "pixel_mouse": "on",
                "clipboard_protocol": "on",
                "synchronized_output": "on",
            },
            "\x1b_Gi=2147515049;EINVAL:unsupported\x1b\\",
            "\x1b[?1016;0$y\x1b[?5522;4$y\x1b[?2026;0$y",
        )
        for name in (
            "kitty_graphics",
            "pixel_mouse",
            "clipboard_protocol",
            "synchronized_output",
        ):
            self.assertEqual(broker.feature_status[name].support, "unsupported")
        self.assertFalse(broker.profile.kitty_graphics)
        self.assertFalse(broker.profile.pixel_mouse)
        self.assertTrue(broker.profile.clipboard_protocol)
        self.assertTrue(broker.profile.synchronized_output)
        self.assertEqual(broker.graphics_reason, "verified-cell-size-unavailable")
        broker.refresh_geometry(80, 24, 800, 480)
        self.assertTrue(broker.profile.kitty_graphics)
        self.assertTrue(broker.profile.pixel_mouse)
        self.assertEqual(broker.feature_status["kitty_graphics"].support, "unsupported")
        self.assertEqual(broker.graphics_reason, "")

    def test_off_survives_positive_replies_and_geometry_refresh(self):
        """Valid replies and later resize cannot re-enable a disabled feature."""
        names = (
            "hyperlinks",
            "extended_styles",
            "overline",
            "cursor_color",
            "iterm_graphics",
            "kitty_graphics",
            "sixel_graphics",
            "desktop_notifications",
            "synchronized_output",
            "enhanced_keyboard",
            "pixel_mouse",
            "clipboard_protocol",
            "scheme_notifications",
        )
        broker = self.probe(
            dict.fromkeys(names, "off"),
            "\x1b[6;20;10t\x1b_Gi=2147515049;OK\x1b\\",
            "\x1b[?2026;2$y\x1b[?0u\x1b[?1016;2$y\x1b[?5522;2$y",
            "\x1b[?2031;2$y\x1b[?997;1n\x1b]12;rgb:11/22/33\x07",
        )
        for size in ((90, 25, 0, 0), (90, 25, 900, 500)):
            broker.refresh_geometry(*size)
            self.assertTrue(
                all(not state.active for state in broker.feature_status.values())
            )
            self.assertEqual(broker.enter_input_modes(), "")
            self.assertEqual(broker.enter_metadata_modes(), "")
            self.assertEqual(broker.set_cursor_color((1, 2, 3)), "")
            self.assertEqual(broker.graphics_reason, "image-protocols-disabled")
        self.assertEqual(broker.feature_status["pixel_mouse"].support, "supported")

    def test_iterm_requires_explicit_selection_and_measured_cell_size(self):
        """iTerm has no support probe, and cannot replace missing size with a guess."""
        broker = self.probe({"iterm_graphics": "on"})
        self.assertEqual(broker.feature_status["iterm_graphics"].support, "unqueried")
        self.assertFalse(broker.feature_status["iterm_graphics"].active)
        self.assertEqual(broker.graphics_reason, "verified-cell-size-unavailable")
        broker.refresh_geometry(80, 24, 800, 480)
        self.assertEqual(broker.profile.image_protocol, "iterm")
        self.assertFalse(broker.profile.kitty_graphics)
        broker.refresh_geometry(90, 25, 0, 0)
        self.assertIsNone(broker.profile.image_protocol)
        self.assertEqual(broker.graphics_reason, "cell-size-unavailable-after-resize")

    def test_keyboard_query_rejects_unsolicited_invalid_late_and_cancelled_reports(
        self,
    ):
        """Only a valid in-flight status authorizes the existing keyboard push/pop."""
        broker = HostBroker()
        broker.control("\x1b[?0u")
        broker.begin_probe(80, 24)
        for invalid in ("\x1b[?32u", "\x1b[?999u", "\x1b[?-1u", "\x1b[?1;2u"):
            self.assertTrue(broker.control(invalid))
        self.assertFalse(broker.finish_probe().enhanced_keyboard)
        broker.control("\x1b[?0u")
        self.assertFalse(broker.profile.enhanced_keyboard)
        self.assertEqual(broker.feature_status["enhanced_keyboard"].support, "unknown")
        for flags in (0, 1, 3, 31):
            with self.subTest(flags=flags):
                valid = self.probe(None, f"\x1b[?{flags}u")
                self.assertEqual(valid.enter_input_modes(), "\x1b[>3u")
                self.assertEqual(valid.enter_input_modes(), "")
                self.assertEqual(valid.leave_input_modes(), "\x1b[<u")
                self.assertEqual(valid.leave_input_modes(), "")
        cancelled = HostBroker()
        cancelled.begin_probe(80, 24)
        cancelled.cancel_probe()
        cancelled.control("\x1b[?0u")
        self.assertFalse(cancelled.profile.enhanced_keyboard)
        self.assertEqual(cancelled.enter_input_modes(), "")

    def test_keyboard_response_fragments_preserve_real_keys_and_literal_paste(self):
        """Keyboard status never reaches user bindings, including after activation."""
        reply = "\x1b[?0u"
        for split in range(2, len(reply)):
            broker, keys = HostBroker(), []
            broker.begin_probe(80, 24)
            parser = HostParser(Vt100Parser(keys.append), broker)
            parser.feed("a" + reply[:split])
            parser.flush()
            parser.feed(reply[split:] + "b")
            self.assertEqual([key.data for key in keys], ["a", "b"])
            self.assertTrue(broker.finish_probe().enhanced_keyboard)
            broker.enter_input_modes()
            parser.feed("\x1b[?31u" + "\x1b[200~" + reply + "\x1b[201~")
            self.assertEqual([key.data for key in keys], ["a", "b", reply])
            self.assertEqual(keys[-1].key, Keys.BracketedPaste)

    def test_on_cannot_replace_missing_cursor_and_scheme_start_values(self):
        """Restoration and identity-free notification boundaries remain prerequisites."""
        broker = self.probe(
            {"cursor_color": "on", "scheme_notifications": "on"},
            "\x1b[?2031;2$y",
        )
        self.assertEqual(
            broker.feature_status["scheme_notifications"].support, "supported"
        )
        self.assertEqual(
            broker.feature_status["scheme_notifications"].reason,
            "initial-scheme-unavailable",
        )
        self.assertEqual(
            broker.feature_status["cursor_color"].reason, "cursor-state-unavailable"
        )
        self.assertEqual(broker.enter_metadata_modes(), "")
        self.assertEqual(broker.set_cursor_color((1, 2, 3)), "")

    def test_cancelled_probe_keeps_evidence_without_reactivating_features(self):
        """Cancellation stops use without turning an earlier ACK into a rejection."""
        broker = HostBroker(features={"kitty_graphics": "on"})
        broker.begin_probe(80, 24, query_id=2147515049)
        broker.control("\x1b_Gi=2147515049;OK\x1b\\")
        broker.control("\x1b[?0u")
        broker.cancel_probe()
        broker.refresh_geometry(80, 24, 800, 480)
        self.assertEqual(broker.feature_status["kitty_graphics"].support, "supported")
        self.assertEqual(
            broker.feature_status["enhanced_keyboard"].support, "supported"
        )
        self.assertTrue(
            all(not state.active for state in broker.feature_status.values())
        )
        self.assertEqual(broker.graphics_reason, "host-probe-cancelled")
        self.assertEqual(broker.enter_input_modes(), "")


if __name__ == "__main__":
    unittest.main()
