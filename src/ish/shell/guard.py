"""Load adapter-selected native policies within the original shell process.

The Linux preload library affects only its initial process and controlling TTY.
Adapters choose the library and its features; loading alone does not enable the
tcsh read-ahead workaround. Forked children use the original ioctl, and exec
children receive the original LD_PRELOAD environment. No input is read or
synthesized by the guard.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import IntFlag
from pathlib import Path

from ish.runtime.distribution import bundled_root

from .constants import BUNDLED_FORWARD_DIRECTORY

GUARD_FD = "_ISH_NATIVE_FD"
GUARD_PRELOAD = "_ISH_NATIVE_ORIGINAL_PRELOAD"
GUARD_FEATURES = "_ISH_NATIVE_FEATURES"
GUARD_ACTIVE = "_ISH_NATIVE_ACTIVE"
GUARD_CHECKED = "_ish_native_policy"
GUARD_ABI = 1


class NativeFeature(IntFlag):
    """Opt-in behaviors implemented by the shared native library."""

    NONE = 0
    SUPPRESS_TTY_READAHEAD = 1


@dataclass(frozen=True)
class NativeLibrary:
    """Describe a native build artifact without binding it to a shell name."""

    binary_name: str
    source_name: str
    source: str
    compiler_flags: tuple[str, ...] = ("-shared", "-fPIC", "-ldl")

    def bundled_path(self) -> Path | None:
        """Locate a marked distribution's artifact, even if packaging omitted it."""
        root = bundled_root()
        return root / BUNDLED_FORWARD_DIRECTORY / self.binary_name if root else None


@dataclass(frozen=True)
class PreloadPolicy:
    """Select a library and the native behaviors authorized for one adapter."""

    library: NativeLibrary
    features: NativeFeature = NativeFeature.NONE

    def __post_init__(self):
        """Reject unimplemented features before opening a PTY or spawning a shell."""
        if not isinstance(self.features, NativeFeature):
            raise TypeError("Native features must use NativeFeature")
        if int(self.features) & ~sum(int(feature) for feature in NativeFeature):
            raise ValueError(f"Unsupported native features: {self.features!r}")

    @property
    def activation_token(self) -> str:
        """Require the loaded library to acknowledge the ABI and exact feature set."""
        return f"{GUARD_ABI}:{int(self.features)}"


SHELL_GUARD_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

static pid_t owner;
static unsigned long features;
static struct stat input_tty;
static int (*original_ioctl)(int, unsigned long, ...);

static int select_features(void) {
    const char *value = getenv("@GUARD_FEATURES@");
    if (!value || !*value) return 0;
    char *end;
    errno = 0;
    unsigned long selected = strtoul(value, &end, 10);
    if (errno || *end || (selected & ~@SUPPORTED_FEATURES@UL)) return 0;
    features = selected;
    return 1;
}

__attribute__((constructor)) static void initialize(void) {
    int saved_errno = errno;
    original_ioctl = dlsym(RTLD_NEXT, "ioctl");
    const char *descriptor = getenv("@GUARD_FD@");
    if (descriptor) {
        char *end;
        errno = 0;
        long fd = strtol(descriptor, &end, 10);
        if (!errno && *descriptor && !*end && fd >= 3 && fd <= INT_MAX) {
            if (original_ioctl && select_features() &&
                fstat(STDIN_FILENO, &input_tty) == 0 &&
                S_ISCHR(input_tty.st_mode) && isatty(STDIN_FILENO)) {
                owner = getpid();
                char active[64];
                snprintf(active, sizeof(active), "@GUARD_ABI@:%lu", features);
                setenv("@GUARD_ACTIVE@", active, 1);
            }
            /* The loader owns its mapping; this inherited handle is no longer needed. */
            close((int)fd);
        }
        const char *previous = getenv("@GUARD_PRELOAD@");
        if (previous) setenv("LD_PRELOAD", previous, 1);
        else unsetenv("LD_PRELOAD");
        unsetenv("@GUARD_FD@");
        unsetenv("@GUARD_PRELOAD@");
        unsetenv("@GUARD_FEATURES@");
    }
    errno = saved_errno;
}

int ioctl(int fd, unsigned long request, ...) {
    va_list args;
    va_start(args, request);
    void *argument = va_arg(args, void *);
    va_end(args);
    int saved_errno = errno;
    if ((features & @SUPPRESS_TTY_READAHEAD@UL) && request == FIONREAD &&
        argument && owner && owner == getpid()) {
        struct stat target;
        if (fstat(fd, &target) == 0 && S_ISCHR(target.st_mode) &&
            target.st_dev == input_tty.st_dev &&
            target.st_ino == input_tty.st_ino && target.st_rdev == input_tty.st_rdev) {
            /* Load_input_line then takes the normal byte-wise editor path. */
            *(int *)argument = 0;
            errno = saved_errno;
            return 0;
        }
    }
    errno = saved_errno;
    if (!original_ioctl) original_ioctl = dlsym(RTLD_NEXT, "ioctl");
    if (original_ioctl) return original_ioctl(fd, request, argument);
    errno = ENOSYS;
    return -1;
}
"""
for _token, _value in {
    "GUARD_FD": GUARD_FD,
    "GUARD_PRELOAD": GUARD_PRELOAD,
    "GUARD_FEATURES": GUARD_FEATURES,
    "GUARD_ACTIVE": GUARD_ACTIVE,
    "GUARD_ABI": GUARD_ABI,
    "SUPPORTED_FEATURES": sum(int(feature) for feature in NativeFeature),
    "SUPPRESS_TTY_READAHEAD": int(NativeFeature.SUPPRESS_TTY_READAHEAD),
}.items():
    SHELL_GUARD_SOURCE = SHELL_GUARD_SOURCE.replace(f"@{_token}@", str(_value))


@contextlib.contextmanager
def spawn_environment(
    environ: dict[str, str], library: Path | None, policy: PreloadPolicy | None = None
):
    """Pass a library FD through exec, supporting paths containing spaces or colons."""
    if library is None and policy is None:
        yield environ, ()
        return
    if library is None or policy is None:
        raise ValueError(
            "A native library path and preload policy must be supplied together"
        )
    with library.open("rb") as handle:
        env = environ.copy()
        for key in (
            GUARD_FD,
            GUARD_PRELOAD,
            GUARD_FEATURES,
            GUARD_ACTIVE,
            GUARD_CHECKED,
        ):
            env.pop(key, None)
        if "LD_PRELOAD" in environ:
            env[GUARD_PRELOAD] = environ["LD_PRELOAD"]
        env[GUARD_FD] = str(handle.fileno())
        env[GUARD_FEATURES] = str(int(policy.features))
        env["LD_PRELOAD"] = f"/proc/self/fd/{handle.fileno()}"
        if environ.get("LD_PRELOAD"):
            env["LD_PRELOAD"] += " " + environ["LD_PRELOAD"]
        yield env, (handle.fileno(),)
