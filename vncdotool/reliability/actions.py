"""Action receipts: what an input did, as far as this end can tell.

Sending an event proves that bytes were written to the socket.  A receipt
distinguishes that from an outcome the caller can observe on a frame that
arrived afterwards, checked by a predicate the caller supplies.  Pixels
having changed is one such predicate, and a weak one: a clock ticking over
changes pixels too.  Semantic success is whatever the caller's predicate
asserts, nothing more.

An ``UNKNOWN`` receipt is the honest answer when the events went out and
nothing confirmed or denied them within the bound.  Nothing here resends on
``UNKNOWN``: a second keypress after an unconfirmed first is two keypresses.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from twisted.internet import reactor as default_reactor
from twisted.internet.defer import Deferred
from twisted.python.failure import Failure

from .client import ReliableClient
from .frames import FrameInfo

if TYPE_CHECKING:
    from PIL import Image

log = logging.getLogger(__name__)

Action = Callable[[ReliableClient], Any]
Predicate = Callable[[ReliableClient, FrameInfo], bool]


class ReceiptStatus(str, enum.Enum):
    SENT = "sent"
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass
class ActionReceipt:
    action: str
    status: ReceiptStatus
    generation: int
    sequence_before: int
    frame: FrameInfo | None = None
    frames_seen: int = 0
    verified_by: str | None = None
    reason: str | None = None
    sent_at: float | None = None

    @property
    def sent(self) -> bool:
        return self.status is not ReceiptStatus.FAILED

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status.value,
            "sent": self.sent,
            "generation": self.generation,
            "sequence_before": self.sequence_before,
            "frames_seen": self.frames_seen,
            "verified_by": self.verified_by,
            "reason": self.reason,
            "sent_at": self.sent_at,
            "frame": None if self.frame is None else self.frame.to_json(),
        }


def _name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", None) or type(fn).__name__


class _Verification:
    def __init__(
        self,
        client: ReliableClient,
        receipt: ActionReceipt,
        verify: Predicate,
        timeout: float,
        clock: Any,
    ) -> None:
        self.client = client
        self.receipt = receipt
        self.verify = verify
        self.result: Deferred = Deferred()
        self.done = False
        self.timer = clock.callLater(timeout, self._timeout, timeout)
        self.cancel_link = client.onLinkLost(self._link_lost)

    def start(self) -> Deferred:
        self._request()
        return self.result

    def _request(self) -> None:
        self.client.refreshScreen(incremental=True).addCallback(self._frame)

    def _frame(self, _: object) -> None:
        if self.done:
            return
        frame = self.client.frames.latest
        if frame is None or frame.sequence <= self.receipt.sequence_before:
            self._request()
            return
        self.receipt.frame = frame
        self.receipt.frames_seen += 1
        try:
            verified = bool(self.verify(self.client, frame))
        except Exception as exc:
            self._finish(ReceiptStatus.UNKNOWN, f"predicate {_name(self.verify)} raised {exc!r}")
            return
        if verified:
            self.receipt.verified_by = _name(self.verify)
            self._finish(ReceiptStatus.VERIFIED)
        else:
            self._request()

    def _timeout(self, timeout: float) -> None:
        self._finish(
            ReceiptStatus.UNKNOWN,
            f"{_name(self.verify)} not satisfied within {timeout}s "
            f"({self.receipt.frames_seen} frame(s) received after sending)",
        )

    def _link_lost(self, reason: Failure) -> None:
        self._finish(ReceiptStatus.UNKNOWN, f"connection lost after sending: {reason.getErrorMessage()}")

    def _finish(self, status: ReceiptStatus, reason: str | None = None) -> None:
        if self.done:
            return
        self.done = True
        if self.timer.active():
            self.timer.cancel()
        self.cancel_link()
        self.receipt.status = status
        self.receipt.reason = reason
        self.result.callback(self.receipt)


def perform(
    client: ReliableClient,
    action: Action,
    *,
    name: str | None = None,
    verify: Predicate | None = None,
    timeout: float = 5.0,
    generation: int | None = None,
    clock: Any = default_reactor,
) -> Deferred:
    """Run ``action`` against ``client`` and settle an :class:`ActionReceipt`.

    The Deferred never errbacks; every outcome is a receipt.  ``generation``
    is the screen generation the action's coordinates were measured on; a
    mismatch fails the action before anything is sent.
    """
    label = name or _name(action)
    receipt = ActionReceipt(
        action=label,
        status=ReceiptStatus.FAILED,
        generation=client.frames.generation,
        sequence_before=client.frames.sequence,
    )
    if not client.link_up:
        receipt.reason = "not connected; nothing sent"
        return _settled(receipt)
    if generation is not None and generation != client.frames.generation:
        receipt.reason = (
            f"planned against screen generation {generation}, screen is now "
            f"generation {client.frames.generation}; nothing sent"
        )
        return _settled(receipt)

    try:
        action(client)
    except Exception as exc:
        receipt.reason = f"{label} raised {exc!r} before sending"
        return _settled(receipt)
    receipt.status = ReceiptStatus.SENT
    receipt.sent_at = clock.seconds()

    if verify is None:
        return _settled(receipt)
    return _Verification(client, receipt, verify, timeout, clock).start()


def _settled(receipt: ActionReceipt) -> Deferred:
    d: Deferred = Deferred()
    d.callback(receipt)
    return d


# -- ready-made actions ------------------------------------------------------


def key_press(key: str) -> Action:
    def press(client: ReliableClient) -> None:
        client.keyPress(key)
    press.__name__ = f"key {key}"
    return press


def type_text(text: str) -> Action:
    def type_(client: ReliableClient) -> None:
        for char in text:
            client.keyPress(char)
    type_.__name__ = f"type {len(text)} char(s)"
    return type_


def click(x: int, y: int, button: int = 1) -> Action:
    def click_(client: ReliableClient) -> None:
        client._requireOnScreen((x, y, x + 1, y + 1))
        client.mouseMove(x, y)
        client.mousePress(button)
    click_.__name__ = f"click {button} at {x},{y}"
    return click_


def mouse_move(x: int, y: int) -> Action:
    def move(client: ReliableClient) -> None:
        client._requireOnScreen((x, y, x + 1, y + 1))
        client.mouseMove(x, y)
    move.__name__ = f"move to {x},{y}"
    return move


# -- ready-made predicates ---------------------------------------------------


def pixels_changed(client: ReliableClient, frame: FrameInfo) -> bool:
    """Weak: the frame differs from the one before it.  Anything animating passes."""
    return not frame.identical_to_previous


def not_black(client: ReliableClient, frame: FrameInfo) -> bool:
    return not frame.black


def matches_image(image: "Image.Image", x: int = 0, y: int = 0, fuzz: int | None = None, blur: int | None = None) -> Predicate:
    """The region at (x, y) the size of ``image`` matches it, as ``expect`` would judge."""
    from .. import imagematch

    reference = image.convert("RGB")
    w, h = reference.size

    def matches(client: ReliableClient, frame: FrameInfo) -> bool:
        region = client.renderRegion(x, y, w, h)
        return bool(imagematch.matches(region, reference, client._fuzz(fuzz), client._blur(blur)))
    matches.__name__ = "matches_image"
    return matches


def region_changed(client: ReliableClient, x: int, y: int, w: int, h: int) -> Predicate:
    """The region differs from how it looks at the moment this is called."""
    from PIL import ImageChops

    before = client.renderRegion(x, y, w, h)

    def changed(client: ReliableClient, frame: FrameInfo) -> bool:
        after = client.renderRegion(x, y, w, h)
        return ImageChops.difference(before, after).getbbox() is not None
    changed.__name__ = f"region_changed {x},{y} {w}x{h}"
    return changed
