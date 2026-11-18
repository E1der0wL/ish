"""Copy to ~/ish/.ishrc.py and edit choices for subsequent ish sessions."""

# None detects LANG and falls back to English; use "ko" or "en" to override.
config.lang = None  # noqa: F821
config.escape_delay = 0.05  # noqa: F821

# No plugins means no UI, popup shortcut, or shell hook.
config.plugins = []  # noqa: F821

# Put selected packages under ~/ish/plugin/script, or choose another directory.
# config.plugin_dir = "/absolute/path/to/ish/examples/terminal_plugins"
# config.plugins = ["terminal_bar", "terminal_popup"]
# config.features = {"hyperlinks": "on", "enhanced_keyboard": "auto"}
# config.mouse = "auto"
# config.clipboard = "write"
# config.shell = "bash"
# config.log_file = ".ish.log"
# config.log_level = "DEBUG"
