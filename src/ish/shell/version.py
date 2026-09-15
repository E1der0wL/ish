"""Check native shell versions before acquiring interactive session resources."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass

from ish.lang import i18n

VERSION_PROBE_TIMEOUT = 5.0
VERSION_OUTPUT_LIMIT = 16 * 1024


class ShellVersionError(RuntimeError):
    """Report an unsupported or unverifiable shell without starting a session."""


@dataclass(frozen=True)
class VersionPolicy:
    """Declare a release floor and the shell's rc-free version banner format."""

    minimum: tuple[int, ...]
    prefix: str
    args: tuple[str, ...] = ("--version",)

    @property
    def required(self) -> str:
        """Format the required release for errors and diagnostics."""
        return ".".join(map(str, self.minimum))

    def parse(self, output: bytes) -> tuple[int, ...] | None:
        """Accept numeric release banners, including Bash's build/release suffix."""
        for line in output.decode("ascii", errors="replace").splitlines():
            if not line.startswith(self.prefix):
                continue
            words = line[len(self.prefix) :].split()
            if not words:
                return None
            release, marker, build = words[0].partition("(")
            if marker:
                number, closing, suffix = build.partition(")")
                if (
                    not closing
                    or not number.isascii()
                    or not number.isdecimal()
                    or suffix != "-release"
                ):
                    return None
            parts = release.split(".")
            if not 2 <= len(parts) <= 4 or any(
                len(part) > 9 or not part.isascii() or not part.isdecimal()
                for part in parts
            ):
                return None
            return tuple(map(int, parts))
        return None

    def accepts(self, detected: tuple[int, ...]) -> bool:
        """Compare numeric components, treating missing trailing components as zero."""
        width = max(len(detected), len(self.minimum))
        return detected + (0,) * (width - len(detected)) >= self.minimum + (0,) * (
            width - len(self.minimum)
        )


async def _probe(path: str, policy: VersionPolicy) -> tuple[int, bytes]:
    """Bound captured output and reap the probe even during startup cancellation."""
    env = os.environ.copy()
    # Version flags do not read rc files. Also remove environment-based startup
    # hooks so an executable wrapper cannot accidentally source user commands.
    for name in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4"):
        env.pop(name, None)
    for name in list(env):
        if name.startswith("BASH_FUNC_"):
            env.pop(name)
    env["LC_ALL"] = "C"
    process = None
    spawn = None
    completed = False
    try:
        async with asyncio.timeout(VERSION_PROBE_TIMEOUT):
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    path,
                    *policy.args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
            )
            # Retain ownership if TERM/HUP arrives while the process is spawning.
            process = await asyncio.shield(spawn)
            output = bytearray()
            while chunk := await process.stdout.read(VERSION_OUTPUT_LIMIT + 1):
                output.extend(chunk)
                if len(output) > VERSION_OUTPUT_LIMIT:
                    raise ShellVersionError(i18n.get("shell_version_output_limit"))
            status = await process.wait()
            completed = True
            return status, bytes(output)
    finally:
        if process is None and spawn is not None:
            with contextlib.suppress(Exception):
                process = await spawn
        if process is not None and not completed:
            # The isolated probe never owns the user's terminal. Kill the whole
            # probe group so a wrapper's children cannot retain its output pipe.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            # A pipe transport can be paused at its high-water mark. Drain it
            # after killing the writer before waiting for the transport to close.
            while await process.stdout.read(VERSION_OUTPUT_LIMIT):
                pass
            await process.wait()


async def check_version(
    path: str, shell: str, policy: VersionPolicy | None
) -> str | None:
    """Validate one selected executable, or skip shells without a numeric floor."""
    if policy is None:
        return None
    try:
        status, output = await _probe(path, policy)
        if status:
            raise ShellVersionError(i18n.get("shell_version_probe_exit", status=status))
        detected = policy.parse(output)
        if detected is None:
            raise ShellVersionError(i18n.get("shell_version_unrecognized"))
    except (OSError, TimeoutError, ShellVersionError) as exc:
        reason = (
            i18n.get("shell_version_timeout", seconds=VERSION_PROBE_TIMEOUT)
            if isinstance(exc, TimeoutError)
            else str(exc)
        )
        raise ShellVersionError(
            i18n.get(
                "shell_version_unavailable",
                shell=shell,
                required=policy.required,
                path=path,
                reason=reason,
            )
        ) from exc
    version = ".".join(map(str, detected))
    if not policy.accepts(detected):
        raise ShellVersionError(
            i18n.get(
                "shell_version_too_old",
                shell=shell,
                detected=version,
                required=policy.required,
                path=path,
            )
        )
    return version
