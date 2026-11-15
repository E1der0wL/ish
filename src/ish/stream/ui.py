"""Public values supplied by optional stream UI plugins.

Plugins describe PTK content and receive a completion callback. The coordinator
retains terminal output, popup input ownership, and native return handshakes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.layout import AnyContainer


@dataclass(frozen=True)
class PopupView:
    """One fresh popup's PTK content and initial focus target."""

    content: AnyContainer
    focus: Any


PopupFactory = Callable[[Callable[[str | None], bool]], PopupView]
