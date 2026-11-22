"""Declarative settings shared by extension discovery, editing and activation."""

from dataclasses import dataclass

from ish.lang import Message, i18n


def _scalar(value):
    return type(value) in (int, bool) or type(value) is str and value.isprintable()


def _label(value):
    if isinstance(value, str):
        value = {"en": value}
    if (
        not isinstance(value, dict)
        or "en" not in value
        or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() and v.isprintable()
            for k, v in value.items()
        )
    ):
        raise TypeError(i18n.get(Message.MANAGE_SCHEMA_INVALID))
    return dict(value)


@dataclass(frozen=True)
class Option:
    """One package-owned option. The default determines the stored scalar type."""

    title: dict
    description: dict
    default: str | int | bool
    choices: tuple

    def value(self, value):
        """Validate both UI edits and settings supplied directly in Python."""
        if (
            not _scalar(value)
            or type(value) is not type(self.default)
            or self.choices
            and value not in self.choices
        ):
            raise ValueError(
                i18n.get(Message.MANAGE_OPTION_INVALID, name=self.label(self.title))
            )
        return value

    @staticmethod
    def label(labels):
        """Resolve extension-owned translations with their English fallback."""
        return labels.get(i18n.current_lang, labels["en"])


def declarations(raw):
    """Read literal PLUGIN_META settings without importing a package."""
    if not isinstance(raw, dict):
        raise TypeError(i18n.get(Message.MANAGE_SCHEMA_INVALID))
    result = {}
    for name, item in raw.items():
        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or not isinstance(item, dict)
            or not {"title", "description", "default"} <= item.keys()
            or item.keys() - {"title", "description", "default", "choices"}
        ):
            raise TypeError(i18n.get(Message.MANAGE_SCHEMA_INVALID))
        default = item["default"]
        options = item.get("choices", ())
        if (
            not _scalar(default)
            or not isinstance(options, (tuple, list))
            or options
            and (
                default not in options
                or any(type(o) is not type(default) or not _scalar(o) for o in options)
                or len(set(options)) != len(options)
            )
        ):
            raise TypeError(i18n.get(Message.MANAGE_SCHEMA_INVALID))
        result[name] = Option(
            _label(item["title"]), _label(item["description"]), default, tuple(options)
        )
    return result


def resolved(schema, overrides):
    """Apply package defaults and validate only the declared settings."""
    return {
        key: spec.value(overrides.get(key, spec.default))
        for key, spec in schema.items()
    }
