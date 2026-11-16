"""Verify shared logging configuration through output streams and actual files."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ish.config import config
from ish.lang import i18n
from ish.log import get_logger, register_logger, session_logging


class LoggingTests(unittest.TestCase):
    """Keep terminal diagnostics deferred while preserving the existing logger API."""

    def setUp(self):
        """Use a distinct logger and temporary paths for each observable scenario."""
        self.name = "ish-log-test-" + uuid.uuid4().hex
        self.logger = logging.getLogger(self.name)
        self.temporary = tempfile.TemporaryDirectory(prefix="ish-log-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        """Close only handlers and files created by this test."""
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()
        logging.Logger.manager.loggerDict.pop(self.name, None)
        self.temporary.cleanup()

    def test_existing_registration_remains_idempotent(self):
        """Legacy repeated registration retains one handler and its initial level."""
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            register_logger(self.name, logging.INFO)
            register_logger(self.name, logging.ERROR)
            get_logger(self.name).info("legacy message")
        self.assertEqual(output.getvalue(), "[INFO] legacy message\n")

    def test_explicit_level_overrides_earlier_lazy_initialization(self):
        """get_logger's legacy DEBUG default cannot override a requested level."""
        original = io.StringIO()
        deferred = io.StringIO()
        with contextlib.redirect_stderr(original):
            get_logger(self.name)
        with contextlib.redirect_stderr(deferred):
            with session_logging(name=self.name, level="WARNING"):
                get_logger(self.name).debug("hidden debug")
                get_logger(self.name).warning("visible warning")
                self.assertEqual(original.getvalue(), "")
                self.assertEqual(deferred.getvalue(), "")
            self.logger.debug("restored legacy logger")
        self.assertEqual(deferred.getvalue(), "[WARNING] visible warning\n")
        self.assertEqual(original.getvalue(), "[DEBUG] restored legacy logger\n")

    def test_file_records_are_immediate_utf8_and_console_is_deferred(self):
        """A live record reaches the log file without writing into terminal output."""
        path = self.root / "nested/session.log"
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with session_logging(name=self.name, level="INFO", path=path) as log:
                log.logger.debug("filtered")
                log.logger.info("한글 diagnostic")
                contents = path.read_text(encoding="utf-8")
                self.assertIn("INFO", contents)
                self.assertIn("한글 diagnostic", contents)
                self.assertNotIn("filtered", contents)
                self.assertEqual(output.getvalue(), "")
        self.assertEqual(output.getvalue(), "[INFO] 한글 diagnostic\n")

    def test_file_logging_appends_across_sessions(self):
        """Starting a new session preserves the previous file's records."""
        path = self.root / "session.log"
        with contextlib.redirect_stderr(io.StringIO()):
            for message in ("first", "second"):
                with session_logging(name=self.name, path=path) as log:
                    log.logger.warning(message)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("first"))
        self.assertTrue(lines[1].endswith("second"))

    def test_safe_flush_is_once_and_later_records_stay_deferred(self):
        """A startup flush does not duplicate records or enable live stderr writes."""
        startup = io.StringIO()
        shutdown = io.StringIO()
        with contextlib.redirect_stderr(shutdown):
            with session_logging(name=self.name) as log:
                log.logger.warning("before terminal")
                log.flush(startup)
                log.flush(startup)
                log.logger.error("during terminal")
                self.assertEqual(shutdown.getvalue(), "")
        self.assertEqual(startup.getvalue(), "[WARNING] before terminal\n")
        self.assertEqual(shutdown.getvalue(), "[ERROR] during terminal\n")

    def test_exception_restores_existing_handler_and_level(self):
        """Terminal failures still restore the logger and emit deferred diagnostics."""
        existing = io.StringIO()
        deferred = io.StringIO()
        with contextlib.redirect_stderr(existing):
            register_logger(self.name, logging.INFO)
        self.logger.propagate = True
        with contextlib.redirect_stderr(deferred):
            with self.assertRaisesRegex(RuntimeError, "session failed"):
                with session_logging(name=self.name, level="ERROR") as log:
                    log.logger.error("error before restoration")
                    raise RuntimeError("session failed")
        self.logger.info("after restoration")
        self.assertEqual(deferred.getvalue(), "[ERROR] error before restoration\n")
        self.assertEqual(existing.getvalue(), "[INFO] after restoration\n")
        self.assertTrue(self.logger.propagate)

    def test_file_open_failure_does_not_replace_existing_handlers(self):
        """A bad log destination fails before altering existing logging behavior."""
        output = io.StringIO()
        path = self.root / "directory"
        path.mkdir()
        with contextlib.redirect_stderr(output):
            register_logger(self.name, logging.WARNING)
            with self.assertRaises(OSError):
                with session_logging(name=self.name, level="DEBUG", path=path):
                    self.fail("invalid file destination was accepted")
            self.logger.warning("still registered")
        self.assertEqual(output.getvalue(), "[WARNING] still registered\n")

    def test_default_session_creates_no_persistent_log_file(self):
        """Config paths are not populated unless the caller chooses a file."""
        previous = config._ish_home
        config.ISH_HOME = self.root / "unused-home"
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with session_logging(name=self.name) as log:
                    log.logger.warning("memory only")
            self.assertFalse(config.ISH_HOME.exists())
        finally:
            config._ish_home = previous

    def test_large_deferred_output_preserves_every_record(self):
        """Spilling a long session must not drop records or leak early console bytes."""
        output = io.StringIO()
        message = "x" * 1024
        with contextlib.redirect_stderr(output):
            with session_logging(name=self.name, level="DEBUG") as log:
                for number in range(200):
                    log.logger.debug("%s %s", number, message)
                self.assertEqual(output.getvalue(), "")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[0], "[DEBUG] 0 " + message)
        self.assertEqual(lines[-1], "[DEBUG] 199 " + message)

    def test_propagation_cannot_write_to_root_during_session(self):
        """An unrelated root console handler must not receive live session records."""
        output = io.StringIO()
        parent_output = io.StringIO()
        parent_handler = logging.StreamHandler(parent_output)
        root_logger = logging.getLogger()
        root_logger.addHandler(parent_handler)
        try:
            with contextlib.redirect_stderr(output):
                with session_logging(name=self.name) as log:
                    log.logger.warning("isolated record")
                    self.assertEqual(parent_output.getvalue(), "")
                    self.assertEqual(output.getvalue(), "")
            self.assertEqual(output.getvalue(), "[WARNING] isolated record\n")
            self.assertEqual(parent_output.getvalue(), "")
        finally:
            root_logger.removeHandler(parent_handler)
            parent_handler.close()

    def test_invalid_level_has_no_file_or_logging_side_effect(self):
        """Reject unsupported levels before creating paths or changing handlers."""
        path = self.root / "unused/log.txt"
        with self.assertRaises(ValueError):
            with session_logging(name=self.name, level="TRACE", path=path):
                self.fail("invalid level was accepted")
        self.assertFalse(path.parent.exists())
        self.assertEqual(self.logger.handlers, [])

    def test_language_pack_controls_logging_failures_without_live_console_output(self):
        """Use custom translated diagnostics while preserving raw exception details."""
        messages = dict(i18n.message)
        self.addCleanup(setattr, i18n, "message", messages)
        i18n.update(
            {
                "log_level_error": "잘못된 수준: {value}",
                "log_output_error": "저장 실패: {error}",
            }
        )
        with self.assertRaisesRegex(ValueError, "잘못된 수준: TRACE"):
            with session_logging(name=self.name, level="TRACE"):
                self.fail("invalid level was accepted")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with session_logging(
                name=self.name, path=self.root / "translated.log"
            ) as log:
                handler = next(
                    item
                    for item in log.logger.handlers
                    if isinstance(item, logging.FileHandler)
                )
                with patch.object(
                    handler.stream, "write", side_effect=OSError("disk full")
                ):
                    log.logger.error("plugin detail")
                self.assertEqual(output.getvalue(), "")
        self.assertIn("저장 실패: disk full", output.getvalue())
        self.assertIn("plugin detail", output.getvalue())
        self.assertNotIn("Log output failed", output.getvalue())

    def test_runtime_file_failure_does_not_write_into_live_terminal(self):
        """A failed file write is deferred instead of stdlib's stderr traceback."""
        output = io.StringIO()
        path = self.root / "session.log"
        with contextlib.redirect_stderr(output):
            with session_logging(name=self.name, path=path) as log:
                file_handler = next(
                    handler
                    for handler in log.logger.handlers
                    if isinstance(handler, logging.FileHandler)
                )
                with patch.object(
                    file_handler.stream, "write", side_effect=OSError("disk full")
                ):
                    log.logger.error("kept diagnostic")
                self.assertEqual(output.getvalue(), "")
        self.assertIn("[ERROR] kept diagnostic\n", output.getvalue())
        self.assertIn("Log output failed: disk full", output.getvalue())
        self.assertNotIn("Logging error", output.getvalue())

    @unittest.skipUnless(os.name == "posix", "requires POSIX special files")
    def test_special_file_rejection_preserves_existing_logger(self):
        """A device cannot bypass deferred output or replace legacy handlers."""
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            register_logger(self.name, logging.WARNING)
            with self.assertRaisesRegex(OSError, "regular file"):
                with session_logging(name=self.name, path="/dev/null"):
                    self.fail("device log destination was accepted")
            self.logger.warning("still registered")
        self.assertEqual(output.getvalue(), "[WARNING] still registered\n")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_fifo_destination_is_rejected_without_waiting_for_reader(self):
        """An unopened FIFO must fail promptly instead of blocking CLI startup."""
        path = self.root / "log-fifo"
        os.mkfifo(path)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys\nfrom ish.log import session_logging\n"
                "try:\n    with session_logging(path=sys.argv[1]): pass\n"
                "except OSError:\n    print('rejected')\n"
                "else:\n    raise AssertionError('FIFO was accepted')\n",
                str(path),
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "rejected\n")
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(os.name == "posix", "requires unprivileged symlinks")
    def test_symlink_to_regular_log_file_remains_supported(self):
        """Descriptor validation permits an ordinary log symlink and preserves append."""
        destination = self.root / "actual.log"
        destination.write_text("existing\n", encoding="utf-8")
        link = self.root / "linked.log"
        link.symlink_to(destination)
        with contextlib.redirect_stderr(io.StringIO()):
            with session_logging(name=self.name, path=link) as log:
                log.logger.warning("linked diagnostic")
        contents = destination.read_text(encoding="utf-8")
        self.assertTrue(contents.startswith("existing\n"))
        self.assertTrue(contents.endswith("linked diagnostic\n"))
        self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
