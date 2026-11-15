"""Connect selected Python plugins to the stream UI and shell startup contract."""

from __future__ import annotations

import inspect
from pathlib import Path

from .scripts import Unavailable

API_VERSION = 1
DIRECTORY = Path(__file__).with_name("bundled")


class Extensions:
    """Own registrations and cleanup while reusing the existing PluginManager."""

    def __init__(self):
        """Leave all UI and shell integrations absent until explicitly selected."""
        self.manager = None
        self.bar = None
        self.popup = None
        self._shells = []
        self._cleanups = []
        self._sealed = False
        self._closed = False

    @property
    def has_ui(self):
        """Report registered UI, independently of shell preparation or focus."""
        return self.bar is not None or self.popup is not None

    def load(self, names, *, directory=None, library_dir=None):
        """Load only selected modules and establish their registrations once."""
        if self.manager is not None or self._closed:
            raise RuntimeError("Stream plugins can only be loaded once")
        from ish.plugin.manager import PluginManager

        names = tuple(dict.fromkeys(names))
        self.manager = PluginManager(
            script_dir=DIRECTORY if directory is None else Path(directory),
            lib_dir=library_dir,
        )
        try:
            self.manager.load(names=names)
            for name in names:
                module = self.manager.get(name)
                if module is None:
                    raise Unavailable(f"Cannot load stream plugin: {name}")
                version = getattr(module, "STREAM_API", None)
                if type(version) is not int or version != API_VERSION:
                    raise Unavailable(f"Unsupported stream plugin API: {name}")
                setup = getattr(module, "setup", None)
                if not callable(setup):
                    raise Unavailable(f"Stream plugin has no setup function: {name}")
                cleanup = setup(self)
                if cleanup is not None:
                    if not callable(cleanup):
                        raise TypeError("Plugin setup must return a cleanup or None")
                    self._cleanups.append(cleanup)
        finally:
            self._sealed = True

    def _register(self):
        if self._sealed or self._closed:
            raise RuntimeError("Stream registration is only available during setup")

    def set_bar(self, content):
        """Register one PTK formatted-text provider, without a terminal writer."""
        self._register()
        if self.bar is not None:
            raise ValueError("A stream bar is already registered")
        if content is None:
            raise ValueError("A bar provider must not be None")
        self.bar = content

    def set_popup(self, factory):
        """Register a view factory; the coordinator retains the focus handshake."""
        self._register()
        if self.popup is not None:
            raise ValueError("A stream popup is already registered")
        if not callable(factory):
            raise TypeError("A popup factory must be callable")
        self.popup = factory

    def add_shell(self, matches, build):
        """Register native startup policy behind the shared integration contract."""
        self._register()
        if not callable(matches) or not callable(build):
            raise TypeError("A shell registration needs matching and build functions")
        self._shells.append((matches, build))

    def select_shell(self, argv):
        """Select one provider without interpreting shell names in the application."""
        selected = [build for matches, build in self._shells if matches(tuple(argv))]
        if not selected:
            raise Unavailable(
                "No prompt integration is installed for this shell in the selected plugins"
            )
        if len(selected) != 1:
            raise Unavailable("Multiple selected plugins support this shell")
        return selected[0]

    def get(self, name):
        """Look up a loaded dependency through the existing manager registry."""
        return self.manager.get(name) if self.manager is not None else None

    async def close(self):
        """Release plugin resources in reverse order after stopping their UI."""
        if self._closed:
            return []
        self._closed = True
        errors = []
        for cleanup in reversed(self._cleanups):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                errors.append(str(error))
        self._cleanups.clear()
        self.bar = self.popup = None
        self._shells.clear()
        return errors
