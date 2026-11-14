"""Correlate shell input permissions with submissions and individual requests.

The shell emits C only at an interactive syntax read, and R before native input
may be consumed. Neither is a command-start observation. A primary grant still
requires the existing prompt/context/input-return handshake in the session.
"""

from dataclasses import dataclass
from enum import Enum

from .protocol import parse_prompt_id

EDIT = "edit"
MODE = "edit_mode"
VERSION = "1"
IDENTITY_FILE = "submission"


class InputKind(Enum):
    """Describe the consumer authorized by a correlated shell request."""

    SYNTAX = "C"
    RAW = "R"


class InputOwner(Enum):
    """Name the states with distinct input forwarding guarantees."""

    PRIMARY = "primary"
    BARRIER = "barrier"
    EDITOR = "editor"
    RELEASE = "release"
    RAW = "raw"
    RECLAIM = "reclaim"


@dataclass(frozen=True)
class InputRequest:
    """Identify one shell permission within one submitted command."""

    submission: int
    sequence: int
    kind: InputKind


def decode_request(body: bytes, token: str) -> InputRequest | None:
    """Reject malformed, foreign, or unbounded wire identities before admission."""
    parts = body.split(b";")
    if len(parts) != 5 or parts[:2] != [VERSION.encode(), token.encode()]:
        return None
    submission, sequence = (parse_prompt_id(part) for part in parts[2:4])
    if not submission or not sequence:
        return None
    try:
        kind = InputKind(parts[4].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return InputRequest(submission, sequence, kind)


def decode_mode(body: bytes, token: str) -> tuple[int, bool] | None:
    """Read a capability tied to a primary generation, without activating it."""
    parts = body.split(b";")
    if len(parts) != 4 or parts[:2] != [VERSION.encode(), token.encode()]:
        return None
    boundary = parse_prompt_id(parts[2])
    if not boundary or parts[3] not in (b"0", b"1"):
        return None
    return boundary, parts[3] == b"1"


class Handoff:
    """Own one submission's permissions; only acknowledged primary closes it.

    Syntax replies return to the barrier. Raw input remains native until the
    session starts reclaiming it. Raw-to-syntax transitions need an additional
    proven reclaim boundary and are deliberately not admitted by this contract.
    """

    def __init__(self):
        """Start with no active submission or shell request."""
        self._serial = 0
        self.submission = 0
        self.sequence = 0
        self.owner = InputOwner.PRIMARY
        self.request: InputRequest | None = None

    def begin(self) -> int:
        """Hold all trailing input before any byte of a new submission is sent."""
        if self.owner is not InputOwner.PRIMARY:
            raise RuntimeError("A submission requires an acknowledged primary")
        self._serial += 1
        self.submission = self._serial
        self.sequence = 0
        self.request = None
        self.owner = InputOwner.BARRIER
        return self.submission

    def receive(self, request: InputRequest) -> bool:
        """Admit exactly the next C/R request while its submission is awaiting one."""
        if (
            self.owner is not InputOwner.BARRIER
            or request.submission != self.submission
            or request.sequence != self.sequence + 1
        ):
            return False
        self.sequence = request.sequence
        self.request = request
        self.owner = (
            InputOwner.EDITOR
            if request.kind is InputKind.SYNTAX
            else InputOwner.RELEASE
        )
        return True

    def owns(self, request: InputRequest) -> bool:
        """Require the very request admitted by this session, not a copied value."""
        return self.owner is InputOwner.EDITOR and self.request is request

    def reply(self, request: InputRequest) -> bool:
        """Consume a syntax permission before delivering its one physical line."""
        if not self.owns(request):
            return False
        self.request = None
        self.owner = InputOwner.BARRIER
        return True

    def release(self, request: InputRequest) -> bool:
        """Open live forwarding after older held input has been enqueued in order."""
        if self.owner is not InputOwner.RELEASE or self.request is not request:
            return False
        self.owner = InputOwner.RAW
        return True

    def reclaim(self) -> None:
        """Stop raw forwarding and reject late replies during cancellation or return."""
        self.request = None
        self.owner = InputOwner.RECLAIM

    def primary(self) -> None:
        """Finish after the session has verified context and input-return completion."""
        self.request = None
        self.submission = 0
        self.sequence = 0
        self.owner = InputOwner.PRIMARY
