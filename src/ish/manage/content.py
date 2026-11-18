"""Supply translated help categories to both the TUI and plain-text output."""

from ish.lang import Message, i18n

PAGES = (
    (Message.MANAGE_OVERVIEW, Message.MANAGE_HELP_OVERVIEW),
    (Message.MANAGE_KEYS, Message.MANAGE_HELP_KEYS_BODY),
    (Message.MANAGE_SETTINGS, Message.MANAGE_HELP_SETTINGS_BODY),
    (Message.MANAGE_PLUGINS, Message.MANAGE_HELP_PLUGINS_BODY),
    (Message.MANAGE_FEATURES, Message.MANAGE_HELP_FEATURES_BODY),
    (Message.MANAGE_INPUT, Message.MANAGE_HELP_INPUT_BODY),
    (Message.MANAGE_DIAGNOSTICS, Message.MANAGE_HELP_DIAGNOSTICS_BODY),
    (Message.MANAGE_LIMITS, Message.MANAGE_HELP_LIMITS_BODY),
)


def help_pages(parser):
    """Keep command option details owned by the actual parser."""
    return [(i18n.get(title), i18n.get(body)) for title, body in PAGES] + [
        (i18n.get(Message.MANAGE_OPTIONS), parser.format_help())
    ]


def help_text(parser):
    """Offer the same documentation when stdout is a pipe or file."""
    return (
        "\n\n".join(f"{title}\n\n{body}" for title, body in help_pages(parser)) + "\n"
    )
