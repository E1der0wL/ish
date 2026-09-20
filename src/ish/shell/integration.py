"""Write session scripts and build native helpers for typeahead and tcsh input.

Runtime files live in a caller-supplied directory; script templates are defined in
scripts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import textwrap
import traceback
from pathlib import Path

from ish.config import config
from ish.lang import i18n
from ish.log import get_logger
from ish.runtime.distribution import bundled_forward_binary

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
from .guard import NativeLibrary
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
    "build_binary_async",
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
#include <stdio.h>
#include <string.h>
#include <time.h>

extern char **environ;

/* Stream base64 without retaining a second copy of the shell environment. */
struct encoder { FILE *out; unsigned char tail[3]; size_t count; };
static void encode(struct encoder *e, const unsigned char *data, size_t size) {
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    while (size--) {
        e->tail[e->count++] = *data++;
        if (e->count == 3) {
            fputc(alphabet[e->tail[0] >> 2], e->out);
            fputc(alphabet[((e->tail[0] & 3) << 4) | (e->tail[1] >> 4)], e->out);
            fputc(alphabet[((e->tail[1] & 15) << 2) | (e->tail[2] >> 6)], e->out);
            fputc(alphabet[e->tail[2] & 63], e->out);
            e->count = 0;
        }
    }
}
static void encode_end(struct encoder *e) {
    size_t count = e->count;
    if (!count) return;
    unsigned char zero[2] = {0, 0};
    /* Encode the incomplete group directly, with padding instead of zeros. */
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    memcpy(e->tail + count, zero, 3 - count);
    fputc(alphabet[e->tail[0] >> 2], e->out);
    fputc(alphabet[((e->tail[0] & 3) << 4) | (e->tail[1] >> 4)], e->out);
    fputc(count == 2 ? alphabet[(e->tail[1] & 15) << 2] : '=', e->out);
    fputc('=', e->out);
    e->count = 0;
}
static void encode_string(struct encoder *e, const char *s) {
    encode(e, (const unsigned char *)s, strlen(s));
    encode_end(e);
}
static int context(const char *path, const char *status, const char *id) {
    FILE *out = fopen(path, "w");
    if (!out) return 1;
    struct encoder e = { .out = out, .count = 0 };
    fputs("@SOH@@VERSION@@RS@exitcode@US@", out);
    encode_string(&e, status);
    fputs("@RS@alias@US@", out);
    unsigned char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), stdin)) > 0) encode(&e, buf, n);
    if (ferror(stdin)) { fclose(out); return 1; }
    encode_end(&e);
    fputs("@RS@environ@US@", out);
    for (char **entry = environ; *entry; ++entry)
        encode(&e, (const unsigned char *)*entry, strlen(*entry) + 1);
    encode_end(&e);
    fputs("@RS@prompt_id@US@", out);
    encode_string(&e, id);
    fputc('@EOT@', out);
    int failed = ferror(out);
    return fclose(out) != 0 || failed;
}

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
    if (argc == 5 && !strcmp(argv[1], "--context"))
        return context(argv[2], argv[3], argv[4]);
    if (argc == 2 && !strcmp(argv[1], "--time")) {
        printf("%lld\n", (long long)time(NULL));
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "--clock")) {
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 1;
        printf("%lld\n", (long long)now.tv_sec * 1000000000LL + now.tv_nsec);
        return 0;
    }
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

for _token, _value in {
    "@VERSION@": WIRE_VERSION,
    "@SOH@": BIN_SOH,
    "@RS@": BIN_RS,
    "@US@": BIN_US,
    "@EOT@": BIN_EOT,
}.items():
    TTY_FORWARD = TTY_FORWARD.replace(_token, _value)


def install_scripts(
    *, directory=None, signals=None, forward_path=None, adapter=None
) -> Path:
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
        adapter=adapter,
    ).items():
        script_path = ish_xdg_home / name
        with open(script_path, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        os.chmod(script_path, 0o644)
    return ish_xdg_home


def _binary_spec(library: NativeLibrary | None):
    """Select the forwarding executable or the optional native-editor library."""
    if library is not None:
        return (
            library.source_name,
            library.binary_name,
            library.source,
            library.compiler_flags,
            library.bundled_path(),
        )
    return FORWARD_SOURCE, FORWARD_BINARY, TTY_FORWARD, [], bundled_forward_binary()


def build_binary(*, directory=None, library: NativeLibrary | None = None) -> bool:
    """Copy a bundled helper, or build it with GCC in a source checkout.

    Log build diagnostics and remove temporary C source. The host must permit execution
    through both file permissions and the directory's mount policy, including noexec.
    """
    logger = get_logger()
    ish_xdg_home: Path = directory or config.XDG_DATA_HOME
    ish_xdg_home.mkdir(parents=True, exist_ok=True)
    source_name, binary_name, source_text, flags, bundled = _binary_spec(library)
    src_path = ish_xdg_home / source_name
    bin_path = (
        ish_xdg_home / binary_name
        if directory is not None or library is not None
        else Path(ISH_FORWARD)
    )
    import subprocess

    try:
        if bundled is not None:
            shutil.copyfile(bundled, bin_path)
            os.chmod(bin_path, 0o700)
            return True
        src_path.write_text(source_text, encoding="utf-8")
        cmd = ["gcc", "-O3", "-o", str(bin_path), str(src_path), *flags]
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
        logger.error("Could not prepare %s: %s", binary_name, exc)
        return False
    except Exception:
        logger.error(i18n.get("error", error=traceback.format_exc()))
        return False
    finally:
        src_path.unlink(missing_ok=True)


async def build_binary_async(
    *, directory: Path, library: NativeLibrary | None = None
) -> bool:
    """Prepare a session helper without blocking termination during compilation.

    A source build owns a separate compiler process group, including compiler
    subprocesses. Cancellation stops the group and reaps the compiler before the
    session directory can be removed. Distributions copy their bundled helper.
    """
    source_name, binary_name, source_text, flags, bundled = _binary_spec(library)
    if bundled is not None:
        return build_binary(directory=directory, library=library)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / source_name
    binary = directory / binary_name
    process = None
    spawn = None
    completed = False
    try:
        source.write_text(source_text, encoding="utf-8")
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "gcc",
                "-O3",
                "-o",
                str(binary),
                str(source),
                *flags,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        )
        # Do not lose ownership if termination arrives during subprocess setup.
        process = await asyncio.shield(spawn)
        _, error = await process.communicate()
        completed = True
        if process.returncode:
            get_logger().error(
                "gcc failed (exit %s): %s",
                process.returncode,
                error.decode("utf-8", errors="replace").strip(),
            )
            return False
        binary.chmod(0o755)
        return True
    except FileNotFoundError as exc:
        get_logger().error("Could not prepare %s: %s", binary_name, exc)
        return False
    finally:
        if process is None and spawn is not None:
            # Shielded creation can still be completing when cancellation lands.
            with contextlib.suppress(Exception):
                process = await spawn
        if process is not None and not completed:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.communicate(), 1)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.communicate()
        source.unlink(missing_ok=True)
