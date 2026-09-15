"""A :class:`VNCDoToolClient` that keeps frame metadata and link state."""

from __future__ import annotations

import logging
from typing import Any, Callable

from twisted.internet.protocol import connectionDone
from twisted.python.failure import Failure

from ..client import VNCDoToolClient, VNCDoToolFactory
from ..keys import Key
from .frames import FrameInfo, FrameLog

log = logging.getLogger(__name__)

FrameListener = Callable[[FrameInfo], None]
LinkListener = Callable[[Failure], None]


class ReliableClient(VNCDoToolClient):
    factory: "ReliableFactory"

    def __init__(self) -> None:
        super().__init__()
        self.frames = FrameLog()
        self.events_sent = 0
        self.link_up = False
        self.link_lost: Failure | None = None
        self._frame_listeners: list[FrameListener] = []
        self._link_listeners: list[LinkListener] = []

    # -- link state --------------------------------------------------------

    def connectionMade(self) -> None:
        super().connectionMade()
        self.link_up = True
        self.frames.new_generation("connected")
        self.factory.transportConnected(self)

    def connectionLost(self, reason: Failure = connectionDone) -> None:
        self.link_up = False
        self.link_lost = reason
        super().connectionLost(reason)
        listeners, self._link_listeners = self._link_listeners, []
        for listener in listeners:
            listener(reason)

    def onLinkLost(self, listener: LinkListener) -> Callable[[], None]:
        """Call ``listener`` once if the connection drops; returns a canceller."""
        self._link_listeners.append(listener)

        def cancel() -> None:
            if listener in self._link_listeners:
                self._link_listeners.remove(listener)

        return cancel

    # -- frames ------------------------------------------------------------

    def commitUpdate(self, rectangles: list[tuple[int, int, int, int]] | None = None) -> None:
        if rectangles and self.screen is not None:
            info = self.frames.record(self.screen, len(rectangles))
            for listener in list(self._frame_listeners):
                listener(info)
        super().commitUpdate(rectangles)

    def onFrame(self, listener: FrameListener) -> Callable[[], None]:
        """Call ``listener`` with every frame that paints pixels; returns a canceller."""
        self._frame_listeners.append(listener)

        def cancel() -> None:
            if listener in self._frame_listeners:
                self._frame_listeners.remove(listener)

        return cancel

    def updateDesktopSize(self, width: int, height: int) -> None:
        super().updateDesktopSize(width, height)
        self.frames.new_generation(f"resized to {width}x{height}")

    # -- input -------------------------------------------------------------

    def keyEvent(self, key: Key | int, down: bool = True) -> None:
        super().keyEvent(key, down)
        self.events_sent += 1

    def pointerEvent(self, x: int, y: int, buttonmask: int = 0) -> None:
        super().pointerEvent(x, y, buttonmask)
        self.events_sent += 1


class ReliableFactory(VNCDoToolFactory):
    protocol = ReliableClient

    def __init__(self) -> None:
        super().__init__()
        self._transport_listeners: list[Callable[[ReliableClient], None]] = []

    def onTransportConnected(self, listener: Callable[[ReliableClient], None]) -> None:
        self._transport_listeners.append(listener)

    def transportConnected(self, protocol: ReliableClient) -> None:
        listeners, self._transport_listeners = self._transport_listeners, []
        for listener in listeners:
            listener(protocol)

    def describe(self) -> dict[str, Any]:
        """Connection settings safe to print: never the password."""
        return {
            "username": self.username,
            "password_set": self.password is not None,
            "shared": bool(self.shared),
        }
