"""Check feature choices against independent wire replies and output decisions."""

from __future__ import annotations

import unittest

from ish.terminal.features import merge, parse_assignment, validate
from ish.terminal.host import HostBroker

QUERY_ID = 2147515049


class FeaturePolicyTests(unittest.TestCase):
    """Keep preferences, observed support and usable output distinguishable."""

    def broker(self, **features):
        """Open one actual broker probe with a deterministic correlation ID."""
        broker = HostBroker(features=features)
        broker.begin_probe(80, 24, query_id=QUERY_ID)
        return broker

    def test_auto_is_rejected_without_an_implemented_support_query(self):
        """No terminal-name default may quietly become automatic detection."""
        queried = (
            "kitty_graphics",
            "sixel_graphics",
            "synchronized_output",
            "enhanced_keyboard",
            "pixel_mouse",
            "clipboard_protocol",
            "scheme_notifications",
        )
        unqueried = (
            "hyperlinks",
            "extended_styles",
            "overline",
            "cursor_color",
            "iterm_graphics",
            "desktop_notifications",
        )
        self.assertEqual(set(validate()), set(queried + unqueried))
        for name in queried:
            self.assertEqual(validate({name: "auto"})[name], "auto")
        for name in unqueried:
            with self.subTest(name=name):
                for choice in ("on", "off"):
                    self.assertEqual(validate({name: choice})[name], choice)
                with self.assertRaises(ValueError):
                    validate({name: "auto"})
        for value in ({"made_up": "on"}, {"hyperlinks": True}, [], "on"):
            with self.assertRaises(ValueError):
                validate(value)

    def test_cli_overrides_only_named_features_and_copies_settings(self):
        """Later CLI assignments do not erase independent saved preferences."""
        saved = {"hyperlinks": "off", "enhanced_keyboard": "off"}
        selected = merge(
            saved,
            [
                parse_assignment("enhanced_keyboard=on"),
                parse_assignment("enhanced_keyboard=auto"),
            ],
        )
        self.assertEqual(saved, {"hyperlinks": "off", "enhanced_keyboard": "off"})
        self.assertEqual(selected["hyperlinks"], "off")
        self.assertEqual(selected["enhanced_keyboard"], "auto")
        for invalid in ("hyperlinks", "=on", "hyperlinks=", "hyperlinks=auto"):
            with self.assertRaises(ValueError):
                parse_assignment(invalid)

    def test_sixel_requires_attributes_geometry_and_restorable_mode(self):
        """DA1 attribute four is evidence; a host brand or terminal class is not."""
        for response, supported in (("\x1b[?61;4;6c", True), ("\x1b[?4c", False)):
            with self.subTest(response=response):
                broker = self.broker()
                broker.control(response)
                broker.control("\x1b[6;20;10t")
                broker.control("\x1b[?80;2$y")
                profile = broker.finish_probe()
                self.assertEqual(profile.sixel_graphics, supported)
                self.assertEqual(profile.image_protocol, "sixel" if supported else None)
        forced = self.broker(sixel_graphics="on")
        forced.control("\x1b[6;20;10t")
        self.assertFalse(forced.finish_probe().sixel_graphics)
        self.assertEqual(
            forced.feature_status["sixel_graphics"].reason, "initial-mode-unavailable"
        )
        self.assertEqual(forced.graphics_reason, "initial-mode-unavailable")

    def test_sixel_owns_one_output_backend_and_restores_original_mode(self):
        """A fallback must not draw twice or leave the host in page mode."""
        for initial in (1, 2):
            broker = self.broker()
            broker.control("\x1b[?61;4c")
            broker.control("\x1b[6;20;10t")
            broker.control(f"\x1b[?80;{initial}$y")
            broker.finish_probe()
            self.assertEqual(broker.enter_metadata_modes(), "\x1b[?80h")
            self.assertEqual(
                broker.restore_modes(), "\x1b[?80" + ("h" if initial == 1 else "l")
            )
            self.assertEqual(broker.restore_modes(), "")
        preferred = self.broker()
        for reply in (
            "\x1b[?61;4c",
            "\x1b[6;20;10t",
            "\x1b[?80;2$y",
            "\x1b_Gi=2147515049;OK\x1b\\",
        ):
            preferred.control(reply)
        self.assertEqual(preferred.finish_probe().image_protocol, "kitty")
        state = preferred.feature_status["sixel_graphics"]
        self.assertEqual(
            (state.support, state.active, state.reason),
            ("supported", False, "other-image-protocol-selected"),
        )

    def test_sixel_late_duplicate_and_cancelled_replies_do_not_upgrade(self):
        """Identity-free DA1 is accepted only once in the initial probe."""
        broker = self.broker()
        broker.control("\x1b[?61;6c")
        broker.control("\x1b[?61;4c")
        broker.control("\x1b[6;20;10t")
        broker.control("\x1b[?80;2$y")
        self.assertFalse(broker.finish_probe().sixel_graphics)
        broker.control("\x1b[?61;4c")
        self.assertEqual(broker.feature_status["sixel_graphics"].support, "unsupported")
        cancelled = self.broker(sixel_graphics="on")
        cancelled.cancel_probe()
        for reply in ("\x1b[?61;4c", "\x1b[?80;2$y", "\x1b[6;20;10t"):
            cancelled.control(reply)
        self.assertFalse(cancelled.profile.sixel_graphics)
        self.assertEqual(cancelled.feature_status["sixel_graphics"].support, "unknown")

    def test_sixel_geometry_loss_revokes_output_without_losing_evidence(self):
        """A resize cannot reuse stale cell measurements for raster positioning."""
        broker = self.broker()
        for reply in ("\x1b[?61;4c", "\x1b[?80;2$y", "\x1b[6;20;10t"):
            broker.control(reply)
        self.assertTrue(broker.finish_probe().sixel_graphics)
        self.assertFalse(broker.refresh_geometry(90, 25, 0, 0).sixel_graphics)
        self.assertEqual(broker.feature_status["sixel_graphics"].support, "supported")
        self.assertTrue(broker.refresh_geometry(90, 25, 900, 500).sixel_graphics)

    def test_on_and_off_do_not_rewrite_observed_support(self):
        """Explicit use and positive protocol evidence are different facts."""
        broker = self.broker(synchronized_output="on", clipboard_protocol="off")
        broker.control("\x1b[?2026;0$y")
        broker.control("\x1b[?5522;2$y")
        profile = broker.finish_probe()
        self.assertTrue(profile.synchronized_output)
        self.assertFalse(profile.clipboard_protocol)
        status = broker.feature_status
        self.assertEqual(
            (
                status["synchronized_output"].policy,
                status["synchronized_output"].support,
            ),
            ("on", "unsupported"),
        )
        self.assertEqual(
            (status["clipboard_protocol"].policy, status["clipboard_protocol"].support),
            ("off", "supported"),
        )
        self.assertEqual(status["hyperlinks"].support, "unqueried")
        self.assertEqual(status["enhanced_keyboard"].support, "unknown")

    def test_auto_uses_anonymous_replies_and_never_a_version_name(self):
        """A compatible unnamed terminal works; a famous name grants nothing."""
        broker = self.broker()
        broker.control("\x1b[?0u")
        broker.control("\x1b[?1016;2$y")
        broker.control("\x1b[6;20;10t")
        broker.control("\x1b_Gi=2147515049;OK\x1b\\")
        profile = broker.finish_probe()
        self.assertEqual(broker.adapter.name, "unknown")
        self.assertTrue(profile.enhanced_keyboard)
        self.assertTrue(profile.pixel_mouse)
        self.assertEqual(profile.image_protocol, "kitty")
        named = self.broker()
        named.control("\x1bP>|kitty(0.40.0)\x1b\\")
        named.control("\x1b[6;20;10t")
        profile = named.finish_probe()
        self.assertFalse(profile.enhanced_keyboard)
        self.assertFalse(profile.pixel_mouse)
        self.assertIsNone(profile.image_protocol)

    def test_off_survives_geometry_loss_and_recovery(self):
        """A resize may restore geometry, never revoke the user's off choice."""
        broker = self.broker(kitty_graphics="off", pixel_mouse="off")
        broker.control("\x1b_Gi=2147515049;OK\x1b\\")
        broker.control("\x1b[?1016;2$y")
        broker.control("\x1b[6;20;10t")
        broker.finish_probe()
        for size in ((80, 24, 800, 480), (81, 24, 0, 0), (81, 24, 810, 480)):
            with self.subTest(size=size):
                profile = broker.refresh_geometry(*size)
                self.assertFalse(profile.kitty_graphics)
                self.assertFalse(profile.pixel_mouse)
                self.assertIsNone(profile.image_protocol)
                self.assertEqual(
                    broker.feature_status["kitty_graphics"].support, "supported"
                )

    def test_forced_images_still_require_geometry_and_one_output_backend(self):
        """Explicit output protocol choice cannot invent pixels or an ACK."""
        broker = self.broker(kitty_graphics="on", iterm_graphics="on")
        self.assertIsNone(broker.finish_probe().image_protocol)
        profile = broker.refresh_geometry(80, 24, 800, 480)
        self.assertEqual(profile.image_protocol, "iterm")
        self.assertFalse(profile.kitty_graphics)
        self.assertEqual(broker.feature_status["kitty_graphics"].support, "unknown")
        self.assertEqual(broker.feature_status["iterm_graphics"].support, "unqueried")
        self.assertIsNone(broker.refresh_geometry(81, 24, 0, 0).image_protocol)

    def test_cursor_on_without_restorable_initial_color_does_not_write(self):
        """An explicit preference never replaces the required original state."""
        broker = self.broker(cursor_color="on")
        self.assertFalse(broker.finish_probe().cursor_color_control)
        self.assertEqual(broker.set_cursor_color((1, 2, 3)), "")
        self.assertFalse(broker.feature_status["cursor_color"].active)
        self.assertEqual(broker.feature_status["cursor_color"].support, "unqueried")

    def test_late_duplicate_and_cancelled_replies_cannot_upgrade_auto(self):
        """The first correlated reply wins and closed probes stay closed."""
        broker = self.broker()
        broker.control("\x1b[?1016;0$y")
        broker.control("\x1b[?1016;2$y")
        broker.control("\x1b_Gi=2147515050;OK\x1b\\")
        broker.control("\x1b[6;20;10t")
        broker.finish_probe()
        broker.control("\x1b[?3u")
        broker.control("\x1b_Gi=2147515049;OK\x1b\\")
        self.assertEqual(broker.feature_status["pixel_mouse"].support, "unsupported")
        self.assertEqual(broker.feature_status["enhanced_keyboard"].support, "unknown")
        self.assertIsNone(broker.profile.image_protocol)
        cancelled = self.broker(enhanced_keyboard="on", kitty_graphics="on")
        cancelled.cancel_probe()
        cancelled.control("\x1b[?3u")
        cancelled.refresh_geometry(80, 24, 800, 480)
        self.assertFalse(cancelled.profile.enhanced_keyboard)
        self.assertIsNone(cancelled.profile.image_protocol)
