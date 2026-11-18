"""Reject incompatible native artifacts before assigning protocol ownership."""

import ctypes
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ish.lang.i18n import I18N
from ish.terminal.engine import EngineError, check_library

ROOT = Path(__file__).resolve().parents[1]


def artifact(*, abi=7, headers=b"1.0.1", version=b"1.0.1", missing=None):
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
        self.assertEqual(info.bridge_abi, 7)
        self.assertEqual(info.header_version, "1.0.1")
        self.assertEqual(info.version, "1.0.1")
        self.assertEqual(info.native_inputs, ("kitty",))
        library.ish_vt_new.assert_not_called()

    def test_owned_native_failures_use_the_selected_language(self):
        """Rebuild guidance is translated while ABI values and identifiers survive."""
        with tempfile.TemporaryDirectory(prefix="ish-engine-language-") as directory:
            language = I18N()
            language.base_path = Path(directory) / "absent"
            language.load("ko")
            cases = (
                (SimpleNamespace(), "메타데이터 진입점 ish_vt_abi"),
                (artifact(abi=3), "필요한 ABI 7"),
                (artifact(version=b"\xff"), "native 버전이 올바르지"),
                (artifact(headers=b"1.1.0"), "검증되지 않은 libghostty-vt"),
                (artifact(missing=b"kitty_graphics"), "필요한 기능 kitty_graphics"),
            )
            with patch("ish.terminal.engine.i18n", language):
                for library, expected in cases:
                    with (
                        self.subTest(expected=expected),
                        self.assertRaises(EngineError) as raised,
                    ):
                        check_library(library)
                    self.assertIn(expected, str(raised.exception))
                    self.assertIn(
                        "tools/build_ghostty_vt.py로 다시 빌드하세요",
                        str(raised.exception),
                    )
            self.assertFalse(language.base_path.exists())

    def test_abi_rejection_precedes_other_calls(self):
        """No metadata or state ABI is invoked after the bridge number differs."""
        for abi in (5, 6, 9):
            with self.subTest(abi=abi):
                library = artifact(abi=abi)
                with self.assertRaisesRegex(EngineError, f"ABI {abi}.*required ABI 7"):
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
        for feature in (
            b"kitty_graphics",
            b"render_state",
            b"selection",
        ):
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
            (7, "1.0.1", "1.0.1", ("kitty",)),
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
                patch.object(self.builder, "verify_install"),
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
                patch.object(self.builder, "verify_install"),
                patch.object(self.builder.subprocess, "run"),
                patch.object(
                    self.builder.ctypes, "CDLL", return_value=artifact(version=b"1.1.0")
                ),
                self.assertRaisesRegex(EngineError, "Unverified libghostty-vt"),
            ):
                self.builder.build_bridge(prefix)
            self.assertEqual(output.read_bytes(), b"working artifact")
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_missing_or_unverified_library_preserves_existing_artifact(self):
        """Absent upstream symbols and a wrong build cannot publish a bridge."""
        for load, error in (
            (
                Mock(side_effect=OSError("undefined symbol: ghostty_terminal_new")),
                OSError,
            ),
            (Mock(return_value=artifact(missing=b"render_state")), EngineError),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                prefix = Path(folder)
                output = prefix / "lib" / "libish_vt.so"
                output.parent.mkdir()
                output.write_bytes(b"working artifact")
                with (
                    patch.object(self.builder, "verify_install"),
                    patch.object(self.builder.subprocess, "run"),
                    patch.object(self.builder.ctypes, "CDLL", load),
                    self.assertRaises(error),
                ):
                    self.builder.build_bridge(prefix)
                self.assertEqual(output.read_bytes(), b"working artifact")
                self.assertEqual(list(output.parent.iterdir()), [output])

    def source_fixture(self, directory):
        """Create an independent upstream archive and an exact cached extraction."""
        source = directory / "libghostty-vt-1.0.1"
        archive = directory / "upstream.tar.gz"
        source.mkdir()
        with tarfile.open(archive, "w:gz") as stream:
            for name in ("first.zig", "src/second.zig"):
                path = source / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"official source\n")
                entry = tarfile.TarInfo(f"{source.name}/{name}")
                entry.size = len(b"official source\n")
                stream.addfile(entry, io.BytesIO(b"official source\n"))
        return source, archive

    def test_official_archive_checksum_rejects_modified_download(self):
        """Corrupted cached and downloaded archives cannot become build inputs."""
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            archive = cache / "source.tar.gz"
            archive.write_bytes(b"corrupt cached archive")
            expected = hashlib.sha256(b"official archive").hexdigest()

            def downloaded(args, **kwargs):
                Path(args[args.index("--output") + 1]).write_bytes(b"modified archive")

            with (
                patch.object(self.builder.subprocess, "run", side_effect=downloaded),
                self.assertRaisesRegex(RuntimeError, "Checksum mismatch"),
            ):
                self.builder.download(
                    ("https://example.invalid/source.tar.gz", expected), cache=cache
                )
            self.assertEqual(archive.read_bytes(), b"corrupt cached archive")

    def test_upstream_source_verification_never_changes_original_bytes(self):
        """Unmodified cached sources remain untouched, including repeat checks."""
        with tempfile.TemporaryDirectory() as folder:
            source, archive = self.source_fixture(Path(folder))
            cache = source / ".zig-cache"
            cache.mkdir()
            (cache / "generated").write_bytes(b"compiler cache")
            with patch.object(
                self.builder.subprocess,
                "run",
                side_effect=AssertionError("Source verification must not run a patch"),
            ):
                self.builder.verify_source(archive, source)
                self.builder.verify_source(archive, source)
            self.assertEqual((source / "first.zig").read_bytes(), b"official source\n")
            self.assertEqual(
                (source / "src/second.zig").read_bytes(), b"official source\n"
            )
            self.assertEqual((cache / "generated").read_bytes(), b"compiler cache")

    def test_modified_missing_or_added_upstream_sources_are_rejected_without_repair(
        self,
    ):
        """Previous patched caches and injected source files never reach compilation."""
        for changed in ("modified", "missing", "added"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                source, archive = self.source_fixture(Path(folder))
                target = source / "src/second.zig"
                if changed == "modified":
                    target.write_bytes(b"patched source\n")
                elif changed == "missing":
                    target.unlink()
                else:
                    (source / "injected.zig").write_bytes(b"new code")
                before = {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                }
                with (
                    patch.object(
                        self.builder.subprocess,
                        "run",
                        side_effect=AssertionError(
                            "Unverified sources reached compiler"
                        ),
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    self.builder.verify_source(archive, source)
                self.assertEqual(
                    {
                        path.relative_to(source): path.read_bytes()
                        for path in source.rglob("*")
                        if path.is_file()
                    },
                    before,
                )

    def test_native_provenance_binds_upstream_archives_recipe_and_artifact(self):
        """Only an unchanged build from the pinned official sources can be reused."""
        for changed in (
            "library",
            "archives",
            "source",
            "zig_version",
            "build_flags",
            "missing",
            "legacy",
            "link",
        ):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                prefix = directory / "install"
                library = prefix / "lib" / "libghostty-vt.so.1.0.1"
                library.parent.mkdir(parents=True)
                library.write_bytes(b"native artifact")
                for name in ("libghostty-vt.so", "libghostty-vt.so.1"):
                    (library.parent / name).symlink_to(library.name)
                stamp = prefix / "ish-native.json"
                with patch.object(
                    self.builder,
                    "DOWNLOADS",
                    (("https://example.invalid/source.tar.gz", "official checksum"),),
                ):
                    self.builder.record_install(prefix)
                    self.assertEqual(
                        json.loads(stamp.read_text()),
                        {
                            "engine": "1.0.1",
                            "source": "upstream",
                            "archives": {"source.tar.gz": "official checksum"},
                            "zig_version": "0.17.1",
                            "build_flags": [
                                "-Doptimize=ReleaseFast",
                                "-Dcpu=baseline",
                                "-Dsimd=false",
                            ],
                            "library_sha256": hashlib.sha256(
                                b"native artifact"
                            ).hexdigest(),
                        },
                    )
                    self.builder.verify_install(prefix)
                    if changed == "library":
                        library.write_bytes(b"different native artifact")
                    elif changed == "missing":
                        stamp.unlink()
                    elif changed == "legacy":
                        stamp.write_text(
                            json.dumps({"engine": "1.0.1", "supplement": {}})
                        )
                    elif changed == "link":
                        other = library.parent / "unverified.so"
                        other.write_bytes(b"different native artifact")
                        alias = library.parent / "libghostty-vt.so.1"
                        alias.unlink()
                        alias.symlink_to(other.name)
                    else:
                        record = json.loads(stamp.read_text())
                        record[changed] = "different build"
                        stamp.write_text(json.dumps(record))
                    with (
                        patch.object(
                            self.builder.subprocess,
                            "run",
                            side_effect=AssertionError("Unverified library compiled"),
                        ),
                        self.assertRaisesRegex(RuntimeError, "provenance mismatch"),
                    ):
                        self.builder.build_bridge(prefix)

    def test_failed_provenance_write_preserves_previous_stamp_and_cleans_temporary(
        self,
    ):
        """A failed new identity must not corrupt the last completed native build."""
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            prefix = directory / "install"
            library = prefix / "lib" / "libghostty-vt.so.1.0.1"
            library.parent.mkdir(parents=True)
            library.write_bytes(b"native artifact")
            for name in ("libghostty-vt.so", "libghostty-vt.so.1"):
                (library.parent / name).symlink_to(library.name)
            stamp = prefix / "ish-native.json"
            stamp.write_bytes(b"previous completed provenance")
            with (
                patch.object(
                    self.builder.json, "dump", side_effect=OSError("disk full")
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                self.builder.record_install(prefix)
            self.assertEqual(stamp.read_bytes(), b"previous completed provenance")
            self.assertEqual(set(prefix.iterdir()), {library.parent, stamp})


if __name__ == "__main__":
    unittest.main()
