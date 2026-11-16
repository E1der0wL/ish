"""Copy to ~/ish/.ishrc.py and edit choices for subsequent ish sessions."""

# None detects LANG and falls back to English; use "ko" or "en" to override.
config.lang = None  # noqa: F821
config.escape_delay = 0.05  # noqa: F821

# No plugins means no UI, popup shortcut, or shell hook.
config.plugins = []  # noqa: F821

# After copying bundled providers and example plugins into ~/ish/plugins:
# config.plugin_dir = "plugins"
# config.plugins = ["stream_bash", "stream_bar", "stream_popup"]
# Choose stream_zsh or stream_tcsh for those shells.
# config.shell = "bash"
# config.log_file = ".ish.log"
# config.log_level = "DEBUG"
