"""Preserve user Python while saving explicitly edited next-session choices."""

from __future__ import annotations

import ast
import copy
import io
import math
import os
import stat
import tempfile
import tokenize
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path

from ish.lang import Message, i18n
from ish.runtime.settings import validate_command

from .icons import validate_icons
from .palette import validate_palette


class SettingsStore:
    """Own one rc snapshot and an isolated, literal-only block at its end.

    Existing symlinks remain symlinks: replacement targets the resolved file and
    refuses a changed target. A same-directory backup preserves the original
    bytes. The last pre-replace check detects external edits, but this is not a
    filesystem transaction with editors that do not cooperate with ish.
    """

    START = "# >>> ish settings"
    END = "# <<< ish settings"
    STAGE_PREFIX = ".ish-settings-"
    BACKUP_PREFIX = ".ishrc-backup-"
    OBJECT = "config"
    HELP_OPTIONS = {
        "browser": validate_command,
        "ui_palette": validate_palette,
        "ui_icons": validate_icons,
        "active_preset": lambda value: (
            value if isinstance(value, str) and value.isprintable() else ""
        ),
    }
    DEFAULT_NEWLINE = "\n"
    NEW_FILE_MODE = 0o600

    def __init__(self, path, *, settings_type, loader, no_rc=False):
        """Read the selected file once without loading any plugin implementation."""
        self.path = Path(path).expanduser().absolute()
        self.settings_type = settings_type
        self.loader = loader
        self.no_rc = no_rc
        self.settings = settings_type()
        self._field_names = frozenset(item.name for item in fields(self.settings))
        # A first failed rc still needs a complete form. Later failures retain
        # the validated settings from the most recent successful load or save.
        self.settings.validate(self.path.parent)
        self.writes = 0
        self.error = None
        self._original = None
        self._target = self.path
        self._identity = None
        self._managed = {}
        self.reload()

    @staticmethod
    def _identity_of(path):
        """Keep content identity separate from target and permission state."""
        try:
            value = path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode):
            raise IsADirectoryError(str(path))
        return (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_size)

    @classmethod
    def _decode(cls, raw):
        """Use Python's source encoding rule and retain its original newline form."""
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        text = raw.decode(encoding)
        newline = "\r\n" if "\r\n" in text else cls.DEFAULT_NEWLINE
        return text, encoding, newline

    @classmethod
    def _field(cls, target):
        """Identify a direct assignment to the public config object."""
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == cls.OBJECT
        ):
            return target.attr
        return None

    @staticmethod
    def _assignment(node):
        """Return the target of one plain top-level assignment, if present."""
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            return node.targets[0]
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            return node.target
        return None

    @staticmethod
    def _mentions_binding(node, name):
        """Include bindings that Python's AST stores as strings rather than Name nodes."""
        for item in ast.walk(node):
            if isinstance(item, ast.alias):
                if (item.asname or item.name.split(".")[0]) in (name, "*"):
                    return True
            elif isinstance(item, ast.Name):
                if item.id == name:
                    return True
            elif name in (getattr(item, "name", None), getattr(item, "rest", None)):
                # Definitions, exception handlers and structural pattern captures.
                return True
        return False

    @classmethod
    def _palette_literal(cls, statements, position, name):
        """Follow one literal dictionary while its binding and uses are unambiguous."""
        value = None
        for index, node in enumerate(statements):
            if index == position:
                continue
            target = cls._assignment(node)
            if isinstance(target, ast.Name) and target.id == name:
                if index < position:
                    value = node.value
                    continue
                # A fresh binding leaves the already assigned dictionary intact.
                if not any(
                    isinstance(n, ast.Name) and n.id == name
                    for n in ast.walk(node.value)
                ):
                    break
            if cls._mentions_binding(node, name):
                # An alias, mutation, or conditional binding is not a literal.
                value = None
        if not isinstance(value, ast.Dict):
            raise ValueError("Unresolved palette variable")
        return ast.literal_eval(value)

    @classmethod
    def read_help_options(cls, path):
        """Read literal UI overrides for interactive help without evaluating the rc."""
        try:
            # A special file at the rc path must not block a read-only query.
            with open(
                path,
                "rb",
                opener=lambda name, flags: os.open(
                    name, flags | getattr(os, "O_NONBLOCK", 0)
                ),
            ) as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    return {}
                statements = ast.parse(source.read(), filename=os.fspath(path)).body
        except (OSError, SyntaxError, TypeError, ValueError, RecursionError):
            return {}
        pending = set(cls.HELP_OPTIONS)
        overrides = {}
        for index in reversed(range(len(statements))):
            node = statements[index]
            field = cls._field(cls._assignment(node))
            if field in pending:
                try:
                    value = (
                        cls._palette_literal(statements, index, node.value.id)
                        if field == "ui_palette" and isinstance(node.value, ast.Name)
                        else ast.literal_eval(node.value)
                    )
                    overrides[field] = cls.HELP_OPTIONS[field](value)
                except (ValueError, TypeError, RecursionError):
                    pass
            # A later conditional or in-place edit makes an earlier literal
            # ambiguous for that field only. A final managed assignment wins.
            pending.difference_update(cls._field(item) for item in ast.walk(node))
            if not pending:
                break
        return overrides

    @classmethod
    def _block(cls, text):
        """Accept exactly one top-level generated block after all user statements."""
        lines = text.splitlines(keepends=True)
        markers = []
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            if token.string.startswith((cls.START, cls.END)):
                if token.start[1] != 0 or token.string not in (cls.START, cls.END):
                    raise ValueError(i18n.get(Message.MANAGE_STORE_BLOCK))
                markers.append((token.start[0] - 1, token.string))
        if not markers:
            return text, {}
        if len(markers) != 2 or [item[1] for item in markers] != [cls.START, cls.END]:
            raise ValueError(i18n.get(Message.MANAGE_STORE_BLOCK))
        start, end = markers[0][0], markers[1][0]
        if any(line.strip() for line in lines[end + 1 :]):
            raise ValueError(i18n.get(Message.MANAGE_STORE_BLOCK))
        body = "".join(lines[start + 1 : end])
        values = {}
        try:
            tree = ast.parse(body)
            for node in tree.body:
                if (
                    not isinstance(node, ast.Assign)
                    or len(node.targets) != 1
                    or (name := cls._field(node.targets[0])) is None
                    or name in values
                ):
                    raise ValueError
                values[name] = ast.literal_eval(node.value)
        except (SyntaxError, ValueError, TypeError) as error:
            raise ValueError(i18n.get(Message.MANAGE_STORE_BLOCK)) from error
        return "".join(lines[:start]), values

    def reload(self):
        """Replace the snapshot after an explicit reload or external editor return."""
        self.error = None
        try:
            target = self.path.resolve()
            identity = self._identity_of(self.path)
            original = self.path.read_bytes() if identity is not None else None
            text, encoding, newline = self._decode(original or b"")
            prefix, managed = self._block(text)
            if self.no_rc:
                settings = self.settings_type()
                settings.validate(self.path.parent)
            else:
                settings = self.loader(self.path)
            if (
                self.path.resolve() != target
                or self._identity_of(self.path) != identity
            ):
                raise ValueError(i18n.get(Message.MANAGE_STORE_CHANGED))
            if (self.path.read_bytes() if identity is not None else None) != original:
                raise ValueError(i18n.get(Message.MANAGE_STORE_CHANGED))
            self.settings = settings
            self._target, self._identity, self._original = target, identity, original
            self._encoding, self._newline = encoding, newline
            self._prefix, self._managed = prefix, managed
        except (
            OSError,
            ValueError,
            SyntaxError,
            RuntimeError,
            tokenize.TokenError,
        ) as error:
            self.error = error
        return self.settings

    @classmethod
    def _literal(cls, value, name):
        """Serialize supported settings without calling arbitrary repr methods."""
        if isinstance(value, Path):
            value = str(value)
        if type(value) is float and not math.isfinite(value):
            raise ValueError(i18n.get(Message.MANAGE_STORE_VALUE, name=name))
        if value is None or type(value) in (str, int, float, bool):
            return value
        if type(value) in (list, tuple):
            return [cls._literal(item, name) for item in value]
        if type(value) is dict and all(type(key) is str for key in value):
            return {key: cls._literal(item, name) for key, item in value.items()}
        raise ValueError(i18n.get(Message.MANAGE_STORE_VALUE, name=name))

    def _unchanged(self):
        """Refuse target replacement or an edit observed since the displayed snapshot."""
        if self.path.resolve() != self._target:
            raise ValueError(i18n.get(Message.MANAGE_STORE_LINK))
        if self._identity_of(self.path) != self._identity:
            raise ValueError(i18n.get(Message.MANAGE_STORE_CHANGED))
        current = self.path.read_bytes() if self._identity is not None else None
        if current != self._original:
            raise ValueError(i18n.get(Message.MANAGE_STORE_CHANGED))

    def _stage(self, data, *, prefix, mode):
        """Fully flush a new private file before it can become the selected rc."""
        descriptor, name = tempfile.mkstemp(dir=self._target.parent, prefix=prefix)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def validated(self, changes: Mapping):
        """Validate proposed values against the current snapshot without writing it."""
        selected = copy.deepcopy(self.settings)
        for name, value in changes.items():
            if name not in self._field_names:
                raise ValueError(i18n.get(Message.MANAGE_STORE_FIELD, name=name))
            setattr(selected, name, copy.deepcopy(value))
        selected.validate(self.path.parent)
        return selected

    def save(self, changes: Mapping):
        """Validate and atomically replace only explicitly changed field assignments.

        Return the retained backup path, or None when there was no previous file
        or no change. No Python rc, shell, native library, or plugin is executed.
        """
        if self.error is not None:
            raise ValueError(i18n.get(Message.MANAGE_STORE_INVALID, error=self.error))
        if not isinstance(changes, Mapping):
            raise ValueError(i18n.get(Message.MANAGE_STORE_VALUE, name="changes"))
        if not changes:
            return None
        selected = self.validated(changes)
        managed = dict(self._managed)
        for name in changes:
            managed[name] = self._literal(getattr(selected, name), name)
        if set(managed).difference(self._field_names):
            raise ValueError(i18n.get(Message.MANAGE_STORE_BLOCK))
        lines = [self.START]
        lines.extend(
            f"{self.OBJECT}.{name} = {ascii(value)}"
            for name, value in sorted(managed.items())
        )
        lines.append(self.END)
        prefix = self._prefix
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += self._newline
        text = prefix + self._newline.join(lines) + self._newline
        raw = text.encode(self._encoding)
        # Compiling bytes applies the same encoding-cookie rules as startup.
        try:
            compile(raw, str(self.path), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as error:
            raise ValueError(
                i18n.get(Message.MANAGE_STORE_INVALID, error=error)
            ) from error
        self._unchanged()
        if raw == self._original:
            return None
        self._target.parent.mkdir(parents=True, exist_ok=True)
        mode = (
            stat.S_IMODE(self.path.stat().st_mode)
            if self._identity is not None
            else self.NEW_FILE_MODE
        )
        staged = self._stage(raw, prefix=self.STAGE_PREFIX, mode=mode)
        backup = None
        try:
            self._unchanged()
            if self._original is not None:
                backup = self._stage(
                    self._original, prefix=self.BACKUP_PREFIX, mode=mode
                )
            self._unchanged()
            os.replace(staged, self._target)
            self.writes += 1
        finally:
            staged.unlink(missing_ok=True)
        self._original = raw
        self._identity = self._identity_of(self.path)
        self._managed, self.settings = managed, selected
        return backup
