"""End-to-end Linux PTY test with isolated HOME, runtime files and environment."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import sys
import tempfile
import termios
import time
import unittest

import psutil


class ShellPTYTests(unittest.TestCase):
    def test_large_environment_start_command_and_exit(self):
        self.run_cli('bash')

    def test_zsh_cli(self):
        self.run_cli('zsh')

    def test_csh_cli(self):
        self.run_cli('csh')

    def test_tcsh_cli(self):
        self.run_cli('tcsh')

    def test_sh_cli(self):
        self.run_cli('sh')

    def run_cli(self, name):
        shell = os.environ.get('ISH_TEST_' + name.upper()) or shutil.which(name)
        if not shell:
            self.skipTest(name + ' not installed')
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ish-p1-pty-') as directory:
            scratch = Path(directory)
            (scratch / 'tmp').mkdir()
            if name == 'zsh':
                modules = list(Path(shell).parent.parent.glob('lib/*/zsh/*/zsh/parameter.so'))
                if modules:
                    (scratch / '.zshrc').write_text('module_path=("' + str(modules[0].parent.parent) + '" $module_path)\n')
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(root)
                env = {
                    'HOME': directory, 'XDG_DATA_HOME': str(scratch / 'data'),
                    'TMPDIR': str(scratch / 'tmp'), 'TERM': 'xterm-256color',
                    'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin',
                    'PYTHONPATH': str(root / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    # Larger than a FIFO buffer, including formerly ambiguous bytes.
                    'P1_LARGE': 'x' * 65536 + '\nline\x01\x04\x1e\x1f',
                }
                os.execve(sys.executable, [sys.executable, '-B', '-m', 'ish.main', shell], env)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
            received = bytearray()
            reaped = False

            def until(predicate, timeout=8):
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    if predicate():
                        return True
                    if not select.select([fd], [], [], .05)[0]:
                        continue
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    received.extend(chunk)
                    if b'\x1b[6n' in chunk:
                        os.write(fd, b'\x1b[1;1R')
                return predicate()

            try:
                self.assertTrue(until(lambda: b'\x1b[?2004h' in received), repr(bytes(received[-1000:])))
                received.clear()
                os.write(fd, b"printf 'P1_%s\\n' OK\r")
                self.assertTrue(until(lambda: b'P1_OK\r\n' in received), repr(bytes(received[-1000:])))
                self.assertTrue(until(lambda: received.find(b'\x1b[?2004h', received.find(b'P1_OK\r\n')) >= 0))
                os.write(fd, b'ish_exit\r')
                until(lambda: False, 2)
                waited, status = os.waitpid(pid, os.WNOHANG)
                reaped = bool(waited)
                self.assertTrue(reaped, 'ish did not exit')
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                self.assertEqual(list((scratch / 'tmp').iterdir()), [])
            finally:
                if not reaped:
                    try:
                        proc = psutil.Process(pid)
                        for child in reversed(proc.children(recursive=True)):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                        proc.kill()
                    except psutil.NoSuchProcess:
                        pass
                    os.waitpid(pid, 0)
                os.close(fd)


if __name__ == '__main__':
    unittest.main()
