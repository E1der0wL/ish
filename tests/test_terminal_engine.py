"""Reject incompatible native artifacts before assigning protocol ownership."""

import ctypes
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ish.terminal.engine import EngineError, check_library

ROOT = Path(__file__).resolve().parents[1]


def artifact(*, abi=1, headers=b"1.0.1", version=b"1.0.1", missing=None):
    """Expose independently chosen metadata without emulating terminal state."""
    return SimpleNamespace(
        ish_vt_abi=Mock(return_value=abi),
        ish_vt_headers=Mock(return_value=headers),
        ish_vt_version=Mock(return_value=version),
        ish_vt_missing_feature=Mock(return_value=missing),
        ish_vt_new=Mock(
            side_effect=AssertionError("state allocated before validation")
        ),
    )


class EngineTests(unittest.TestCase):
    """Only the reviewed ABI, engine version and native build are usable."""

    def test_profile_describes_native_inputs_without_claiming_host_output(self):
        """Engine input support does not imply an iTerm decoder or a host backend."""
        library = artifact()
        info = check_library(library)
        self.assertEqual(info.bridge_abi, 1)
        self.assertEqual(info.header_version, "1.0.1")
        self.assertEqual(info.version, "1.0.1")
        self.assertEqual(info.native_inputs, ("kitty",))
        library.ish_vt_new.assert_not_called()

    def test_abi_rejection_precedes_other_calls(self):
        """No metadata or state ABI is invoked after the bridge number differs."""
        library = artifact(abi=9)
        with self.assertRaisesRegex(EngineError, "ABI 9.*required ABI 1"):
            check_library(library)
        for name in (
            "ish_vt_headers",
            "ish_vt_version",
            "ish_vt_missing_feature",
            "ish_vt_new",
        ):
            getattr(library, name).assert_not_called()

    def test_old_bridge_without_metadata_has_actionable_error(self):
        """An existing old shared object must request a rebuild, not crash later."""
        with self.assertRaisesRegex(EngineError, "ish_vt_abi.*Rebuild"):
            check_library(SimpleNamespace())

    def test_unreviewed_version_and_header_mismatch_are_not_auto_upgrades(self):
        """Matching future versions are still unreviewed; mixed artifacts also fail."""
        for headers, version in (
            (b"1.0.1", b"1.1.0"),
            (b"1.1.0", b"1.0.1"),
            (b"1.1.0", b"1.1.0"),
            (b"1.0.1", b"1.0.1-dev+123456"),
        ):
            with self.subTest(headers=headers, version=version):
                library = artifact(headers=headers, version=version)
                with self.assertRaisesRegex(EngineError, "Unverified libghostty-vt"):
                    check_library(library)
                library.ish_vt_missing_feature.assert_not_called()
                library.ish_vt_new.assert_not_called()

    def test_invalid_version_is_rejected_before_native_state(self):
        """Missing or non-ASCII metadata cannot select a provider."""
        for version in (None, b"", b"\xff"):
            with self.subTest(version=version):
                library = artifact(version=version)
                with self.assertRaisesRegex(EngineError, "Invalid native version"):
                    check_library(library)
                library.ish_vt_new.assert_not_called()

    def test_required_build_features_cannot_be_silently_disabled(self):
        """The present bridge needs rendering and image APIs even in text hosts."""
        for feature in (b"kitty_graphics", b"render_state", b"selection"):
            with self.subTest(feature=feature):
                library = artifact(missing=feature)
                with self.assertRaisesRegex(EngineError, feature.decode()):
                    check_library(library)
                library.ish_vt_new.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "The built bridge requires Linux/WSL")
    def test_actual_bridge_reports_its_loaded_engine_and_required_features(self):
        """Read the compiled boundary, without deriving expectations from its constants."""
        library = ctypes.CDLL(
            os.environ.get(
                "ISH_GHOSTTY_LIB", ROOT / ".cache/ghostty-vt/install/lib/libish_vt.so"
            )
        )
        info = check_library(library)
        self.assertEqual(
            (info.bridge_abi, info.header_version, info.version, info.native_inputs),
            (1, "1.0.1", "1.0.1", ("kitty",)),
        )


class BridgeBuildTests(unittest.TestCase):
    """A failed rebuild cannot replace a working native artifact."""

    @classmethod
    def setUpClass(cls):
        """Load the build entry point without running its CLI."""
        spec = importlib.util.spec_from_file_location(
            "build_ghostty_vt", ROOT / "tools/build_ghostty_vt.py"
        )
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)

    def test_compile_failure_preserves_existing_artifact(self):
        """Compiler errors leave the original bytes and no partial artifact."""
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            output = prefix / "lib" / "libish_vt.so"
            output.parent.mkdir()
            output.write_bytes(b"working artifact")
            with (
                patch.object(
                    self.builder.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(1, "cc"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                self.builder.build_bridge(prefix)
            self.assertEqual(output.read_bytes(), b"working artifact")
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_unverified_build_preserves_existing_artifact(self):
        """Compilation alone cannot publish an unreviewed runtime version."""
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            output = prefix / "lib" / "libish_vt.so"
            output.parent.mkdir()
            output.write_bytes(b"working artifact")
            with (
                patch.object(self.builder.subprocess, "run"),
                patch.object(
                    self.builder.ctypes, "CDLL", return_value=artifact(version=b"1.1.0")
                ),
                self.assertRaisesRegex(EngineError, "Unverified libghostty-vt"),
            ):
                self.builder.build_bridge(prefix)
            self.assertEqual(output.read_bytes(), b"working artifact")
            self.assertEqual(list(output.parent.iterdir()), [output])


if __name__ == "__main__":
    unittest.main()
