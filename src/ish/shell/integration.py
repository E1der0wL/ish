"""Write session-specific shell scripts and build the C typeahead forwarding tool.

Runtime files live in a caller-supplied directory; script templates are defined in
scripts.
"""

from __future__ import annotations

import os
import textwrap
import traceback
from pathlib import Path

from ish.config import config
from ish.lang import i18n
from ish.log import get_logger

from .adapters import ADAPTERS
from .constants import (
    AFTER_CONTINUATION,
    AFTER_PROMPT,
    BEFORE_CONTINUATION,
    BEFORE_PROMPT,
    COMMAND_DONE,
    COMMAND_START,
    FORWARD_BINARY,
    FORWARD_SOURCE,
    bytes_to_shell_escape,
)
from .protocol import EOT, RS, SOH, US, VERSION
from .scripts import make_scripts

__all__ = [
    "BEFORE_PROMPT",
    "AFTER_PROMPT",
    "BEFORE_CONTINUATION",
    "AFTER_CONTINUATION",
    "COMMAND_START",
    "COMMAND_DONE",
    "SOH",
    "RS",
    "US",
    "EOT",
    "SHELL_INTEGRATION_MAP",
    "install_scripts",
    "build_binary",
]


WIRE_VERSION = VERSION.decode("ascii")


OSC_BEFORE_PROMPT = bytes_to_shell_escape(BEFORE_PROMPT)
OSC_AFTER_PROMPT = bytes_to_shell_escape(AFTER_PROMPT)
OSC_BEFORE_CONTINUATION = bytes_to_shell_escape(BEFORE_CONTINUATION)
OSC_AFTER_CONTINUATION = bytes_to_shell_escape(AFTER_CONTINUATION)
OSC_COMMAND_START = bytes_to_shell_escape(COMMAND_START)
OSC_COMMAND_DONE = bytes_to_shell_escape(COMMAND_DONE)


BIN_SOH = bytes_to_shell_escape(SOH)
BIN_RS = bytes_to_shell_escape(RS)
BIN_US = bytes_to_shell_escape(US)
BIN_EOT = bytes_to_shell_escape(EOT)


ISH_FORWARD = str(config.XDG_DATA_HOME / FORWARD_BINARY)


SHELL_INTEGRATION = make_scripts(config.XDG_DATA_HOME)
SHELL_INTEGRATION_MAP = {name: adapter.script for name, adapter in ADAPTERS.items()}


TTY_FORWARD: str = textwrap.dedent(r"""
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>

static int write_all(int fd, const unsigned char *buf, size_t size) {
    while (size > 0) {
        ssize_t n = write(fd, buf, size);
        if (n > 0) { buf += n; size -= (size_t)n; continue; }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd p = { .fd = fd, .events = POLLOUT };
            int ready;
            do { ready = poll(&p, 1, -1); } while (ready < 0 && errno == EINTR);
            if (ready > 0 && !(p.revents & (POLLERR | POLLHUP | POLLNVAL))) continue;
        }
        return -1;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 2) return 1;
    struct termios saved, raw;
    if (tcgetattr(STDIN_FILENO, &saved) != 0) return 1;
    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    if (flags < 0) return 1;
    int pipe_fd = open(argv[1], O_WRONLY | O_NONBLOCK);
    if (pipe_fd < 0) return 1;
    signal(SIGPIPE, SIG_IGN);
    raw = saved;
    raw.c_lflag &= ~ICANON;
    raw.c_cc[VMIN] = 0;
    raw.c_cc[VTIME] = 0;
    int result = 0;
    if (tcsetattr(STDIN_FILENO, TCSANOW, &raw) != 0) { close(pipe_fd); return 1; }
    if (fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK) < 0) { result = 1; goto cleanup; }
    unsigned char buf[4096];
    for (;;) {
        ssize_t n = read(STDIN_FILENO, buf, sizeof(buf));
        if (n > 0) {
            if (write_all(pipe_fd, buf, (size_t)n) < 0) { result = 1; break; }
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else {
            if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) result = 1;
            break;
        }
    }
cleanup:
    tcsetattr(STDIN_FILENO, TCSANOW, &saved);
    fcntl(STDIN_FILENO, F_SETFL, flags);
    close(pipe_fd);
    return result;
}
""")


def install_scripts(*, directory=None, signals=None, forward_path=None) -> Path:
    """Write integration scripts with session identifiers and forwarding paths.

    Use UTF-8, LF line endings, and mode 0644. The caller owns directory cleanup.
    """
    from .constants import SessionSignals

    ish_xdg_home: Path = directory or config.XDG_DATA_HOME
    ish_xdg_home.mkdir(parents=True, exist_ok=True)
    for name, content in make_scripts(
        ish_xdg_home,
        signals=signals or SessionSignals(),
        forward_path=forward_path or Path(ISH_FORWARD),
    ).items():
        script_path = ish_xdg_home / name
        with open(script_path, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        os.chmod(script_path, 0o644)
    return ish_xdg_home


def build_binary(*, directory=None) -> bool:
    """Build the forwarding tool with GCC and report success.

    Log build diagnostics and remove temporary C source. The host must permit execution
    through both file permissions and the directory's mount policy, including noexec.
    """
    logger = get_logger()
    ish_xdg_home: Path = directory or config.XDG_DATA_HOME
    ish_xdg_home.mkdir(parents=True, exist_ok=True)
    src_path = ish_xdg_home / FORWARD_SOURCE
    bin_path = (
        ish_xdg_home / FORWARD_BINARY if directory is not None else Path(ISH_FORWARD)
    )
    import subprocess

    try:
        src_path.write_text(TTY_FORWARD, encoding="utf-8")
        cmd = ["gcc", "-O3", "-o", str(bin_path), str(src_path)]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            logger.error(
                "gcc failed (exit %s): %s",
                result.returncode,
                result.stderr.decode("utf-8", errors="replace").strip(),
            )
            return False
        os.chmod(bin_path, 0o755)
        return True
    except FileNotFoundError as exc:
        logger.error("Could not build ish_forward: %s. Ensure gcc is installed.", exc)
        return False
    except Exception:
        logger.error(i18n.get("error", error=traceback.format_exc()))
        return False
    finally:
        src_path.unlink(missing_ok=True)
