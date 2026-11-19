"""Map one explicitly configured host shortcut without adding UI or shell hooks.

Configure the host to send CSI 9001 ~ for the desired physical shortcut before
selecting this plugin. This example does not detect or alter host key bindings.
"""

from ish.terminal.host import StructuredKey

PLUGIN_META = {
    "version": "1.0.0",
    "description": "Explicit terminal key mapping",
    "category": "module",
}
TERMINAL_API = 1
KEY_MAPPINGS = {"\x1b[9001~": StructuredKey("f13", 57376)}


def setup(api):
    """Register through the existing PluginManager surface for this session."""
    api.register_keys("sample_f13", KEY_MAPPINGS)
