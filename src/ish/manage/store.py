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
    DEFAULT_NEWLINE = "\n"
    NEW_FILE_MODE = 0o600

    def __init__(self, path, *, settings_type, loader, no_rc=False):
        """Read the selected file once without loading any plugin implementation."""
        self.path = Path(path).expanduser().absolute()
        self.settings_type = settings_type
        self.loader = loader
        self.no_rc = no_rc
        self.settings = settings_type()
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
                    or not isinstance(node.targets[0], ast.Attribute)
                    or not isinstance(node.targets[0].value, ast.Name)
                    or node.targets[0].value.id != cls.OBJECT
                    or node.targets[0].attr in values
                ):
                    raise ValueError
                values[node.targets[0].attr] = ast.literal_eval(node.value)
        except (SyntaxError, ValueError, TypeError) as error:
            raise ValueError(i18n.get(Message.MANAGE_STORE_BLOCK)) from error
        return "".join(lines[:start]), values

    def reload(self):
        """Replace the snapshot after an explicit reload or external editor return."""
        self.error = None
        self.settings = self.settings_type()
        self._managed = {}
        try:
            # Keep a complete, editable fallback even when arbitrary rc code
            # fails before returning settings or leaves invalid partial values.
            self.settings.validate(self.path.parent)
            self._target = self.path.resolve()
            self._identity = self._identity_of(self.path)
            self._original = (
                self.path.read_bytes() if self._identity is not None else None
            )
            text, self._encoding, self._newline = self._decode(self._original or b"")
            self._prefix, self._managed = self._block(text)
            if not self.no_rc:
                self.settings = self.loader(self.path)
            if (
                self.path.resolve() != self._target
                or self._identity_of(self.path) != self._identity
            ):
                raise ValueError(i18n.get(Message.MANAGE_STORE_CHANGED))
            if (
                self.path.read_bytes() if self._identity is not None else None
            ) != self._original:
                raise ValueError(i18n.get(Message.MANAGE_STORE_CHANGED))
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
        selected = copy.deepcopy(self.settings)
        allowed = {item.name for item in fields(selected)}
        for name, value in changes.items():
            if name not in allowed:
                raise ValueError(i18n.get(Message.MANAGE_STORE_FIELD, name=name))
            setattr(selected, name, copy.deepcopy(value))
        selected.validate(self.path.parent)
        managed = dict(self._managed)
        for name in changes:
            managed[name] = self._literal(getattr(selected, name), name)
        if set(managed).difference(allowed):
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
        finally:
            staged.unlink(missing_ok=True)
        self._original = raw
        self._identity = self._identity_of(self.path)
        self._managed, self.settings = managed, selected
        return backup
