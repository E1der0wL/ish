"""Use the existing host reader and restoration contract for management screens."""

from __future__ import annotations

import asyncio
import os

from prompt_toolkit.application import Application, get_app_session
from prompt_toolkit.input.typeahead import store_typeahead
from prompt_toolkit.output.vt100 import Vt100_Output

from ish.terminal.features import FEATURES
from ish.terminal.host import HostBroker, HostInput
from ish.terminal.rendering import RestoringOutput, RestoringRenderer


class Presentation:
    """Observe startup state before painting, without negotiating UI capabilities."""

    PROBE_SECONDS = 0.75

    def __init__(self):
        """Keep observations in the same broker used by the terminal renderer."""
        self.host = HostBroker(features=dict.fromkeys(FEATURES, "off"))
        self.app = None
        self.presentation_ready = False

    async def observe(self, source, output):
        """Use one reader and queue user keys for PTK in their original order."""
        try:
            supported = isinstance(output, Vt100_Output) and os.isatty(source.fileno())
        except (OSError, ValueError):
            supported = False
        if not supported:
            self.host.cancel_probe()
            self.presentation_ready = True
            return
        pending = []

        def read():
            pending.extend(source.read_keys())
            if source.closed:
                self.host.ready.set()

        try:
            with source.raw_mode(), source.attach(read):
                output.write_raw(self.host.begin_restore_probe())
                output.flush()
                try:
                    await asyncio.wait_for(self.host.ready.wait(), self.PROBE_SECONDS)
                except TimeoutError:
                    pass  # Missing observations remain unknown; no support is granted.
                read()
                pending.extend(source.flush_keys())
        finally:
            store_typeahead(source, pending)
            self.host.finish_probe()
        self.presentation_ready = True


class ManagementApplication(Application):
    """Restore observed host state on close and on PTK external-editor handoff."""

    def __init__(self, *, input=None, output=None, **kwargs):
        """Suppress constructor cursor writes until the first observation finishes."""
        self.presentation = Presentation()
        self.source = input if input is not None else get_app_session().input
        self.destination = output if output is not None else get_app_session().output
        super().__init__(
            input=self.source,
            output=RestoringOutput(self.destination, self.presentation),
            **kwargs,
        )
        self.presentation.app = self
        self.renderer = RestoringRenderer(self.presentation, self.renderer)

    async def run_async(self, *args, **kwargs):
        """Temporarily wrap caller-owned input without closing it or adding readers."""
        wrapped = (
            HostInput(self.source, self.presentation.host)
            if hasattr(self.source, "vt100_parser")
            else None
        )
        self.input = wrapped or self.source
        try:
            if not self.presentation.presentation_ready:
                await self.presentation.observe(self.input, self.destination)
            return await super().run_async(*args, **kwargs)
        finally:
            if wrapped is not None:
                wrapped.restore_parser()
            self.input = self.source
