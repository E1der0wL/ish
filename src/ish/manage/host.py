"""Use the existing host reader and restoration contract for management screens."""

from __future__ import annotations

import asyncio
import os

from prompt_toolkit.application import get_app_session
from prompt_toolkit.input.typeahead import store_typeahead
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from ish.terminal.features import FEATURES
from ish.terminal.host import HostBroker, HostInput
from ish.terminal.rendering import RestoringOutput, RestoringRenderer, ScreenApplication
from ish.terminal.settings import inherited_color_depth

from .palette import PanelStyle


class Presentation:
    """Observe the management screen's text color depth and restore its initial state."""

    PROBE_SECONDS = 0.75

    def __init__(self, output, *, probe_color):
        """Keep observations in the same broker used by the terminal renderer."""
        self.output = output
        self.host = HostBroker(
            features=dict.fromkeys(FEATURES, "off"),
            on_color_probe=self._write_probe if probe_color else None,
        )
        self.app = None
        self.presentation_ready = False

    def _write_probe(self, wire):
        """Send all startup queries through the presentation's existing output."""
        self.output.write_raw(wire)
        self.output.flush()

    async def observe(self, source):
        """Use one reader and queue user keys for PTK in their original order."""
        try:
            supported = isinstance(self.output, Vt100_Output) and os.isatty(
                source.fileno()
            )
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
                self._write_probe(self.host.begin_restore_probe())
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


class ManagementApplication(ScreenApplication):
    """Restore observed host state on close and on PTK external-editor handoff."""

    def __init__(self, *, input=None, output=None, color_depth=None, **kwargs):
        """Suppress constructor cursor writes until the first observation finishes."""
        preferred_depth = color_depth or ColorDepth.from_env()
        outer_color_term = os.environ.get("COLORTERM", "")
        self.source = input if input is not None else get_app_session().input
        destination = output if output is not None else get_app_session().output
        self.presentation = Presentation(
            destination, probe_color=preferred_depth is None
        )
        super().__init__(
            input=self.source,
            output=RestoringOutput(destination, self.presentation),
            color_depth=preferred_depth
            or (
                lambda: inherited_color_depth(
                    self.presentation.host.profile.color_depth, outer_color_term
                )
            ),
            **kwargs,
        )
        self.presentation.app = self
        self.renderer = RestoringRenderer(
            self.presentation, self.renderer, sync_resize=True
        )
        # Apply pane emphasis after PTK resolves both widget and inline swatch colors.
        merged = self.renderer.style
        self.renderer.style = PanelStyle(lambda: merged)

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
                await self.presentation.observe(self.input)
            return await super().run_async(*args, **kwargs)
        finally:
            if wrapped is not None:
                wrapped.restore_parser()
            self.input = self.source
