"""Own private startup resources for an explicitly selected shell integration."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ish.lang import Message, i18n

NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


class Integration:
    """Own startup files while a plugin chooses shell-specific startup policy."""

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        nonce: str,
        build: Callable,
        popup_socket: str | None = None,
        script_dir: Path | None = None,
    ):
        """Apply the selected builder without exposing input or terminal ownership."""
        if not NONCE_PATTERN.fullmatch(nonce):
            raise ValueError(i18n.get(Message.STREAM_NONCE_INVALID))
        home = env.get("HOME")
        if not home or not os.path.isabs(home):
            raise ValueError(i18n.get(Message.STREAM_HOME_ABSOLUTE))
        name = Path(argv[0]).name if argv else "shell"
        self.argv = list(argv)
        self.env = dict(env)
        self.directory = tempfile.TemporaryDirectory(prefix=f"ish-stream-{name}-")
        try:
            directory = Path(self.directory.name)
            self.argv, self.env = build(
                self.argv,
                self.env,
                directory,
                nonce,
                popup_socket,
                script_dir=script_dir,
            )
        except BaseException:
            self.directory.cleanup()
            raise

    def close(self) -> None:
        """Remove only this integration's private startup directory."""
        self.directory.cleanup()

    def __enter__(self) -> Integration:
        """Keep generated startup files alive for the child session."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Release startup files after the child is finished."""
        self.close()
