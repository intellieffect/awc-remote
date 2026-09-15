"""A scripted RFB 3.3 server behind a mocked transport, for the reliability tests.

Everything runs synchronously on the calling thread: the reactor is never
started, and time is whatever ``twisted.internet.task.Clock`` a test hands
to the code under test.  Frames are delivered only in answer to a
FramebufferUpdateRequest the client actually wrote, so a test that never
scripts a frame sees the client wait, as it would against a silent server.
"""
from __future__ import annotations

from struct import pack, unpack
from typing import Any

from PIL import Image
from twisted.internet.defer import Deferred
from twisted.internet.error import ConnectionDone
from twisted.python.failure import Failure

from vncdotool import rfb
from vncdotool.const import AuthTypes, Encoding, MsgC2S, MsgS2C

PIXEL_FORMAT = rfb.PixelFormat()

Frame = Any  # a PIL image, an (r, g, b) fill, or raw bytes for the whole screen


class FakeTransport:
    def __init__(self, server: "FakeServer") -> None:
        self.server = server
        self.writes: list[bytes] = []
        self.requests: list[bool] = []
        self.keys: list[tuple[int, bool]] = []
        self.pointers: list[tuple[int, int, int]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        if self.closed:
            return
        kind = data[0]
        if kind == MsgC2S.FRAMEBUFFER_UPDATE_REQUEST:
            incremental = bool(data[1])
            self.requests.append(incremental)
            self.server.on_request(incremental)
        elif kind == MsgC2S.KEY_EVENT:
            down, key = unpack("!BxxI", data[1:8])
            self.keys.append((key, bool(down)))
        elif kind == MsgC2S.POINTER_EVENT:
            mask, x, y = unpack("!BHH", data[1:6])
            self.pointers.append((x, y, mask))

    def setTcpNoDelay(self, flag: bool) -> None:
        pass

    def loseConnection(self) -> None:
        # Twisted closes after the current event finishes, so a handler that
        # closes and then reports its reason gets to report it.
        self.server.drop_when_idle()

    def abortConnection(self) -> None:
        self.server.drop()


class FakeServer:
    def __init__(
        self,
        width: int = 64,
        height: int = 48,
        frames: list[Frame] | None = None,
        auth: int = AuthTypes.NONE,
        handshake: bool = True,
    ) -> None:
        self.width = width
        self.height = height
        self.frames = list(frames or [])
        self.auth = auth
        self.handshake = handshake
        self.protocol: Any = None
        self.transport: FakeTransport | None = None
        self.attempt: Deferred | None = None
        self.factory: Any = None
        self._pending: list[bytes] = []
        self._busy = False
        self._drop_pending = False
        self.dropped = False
        self.frames_sent = 0

    # -- the Connector the session takes ----------------------------------

    def connect(self, factory: Any, host: str, port: int, family: Any) -> Deferred:
        self.factory = factory
        self.attempt = Deferred()
        self.attempt.addErrback(lambda failure: None)
        return self.attempt

    def accept(self) -> None:
        """The TCP side connects: build the protocol and run the handshake."""
        self.protocol = self.factory.buildProtocol(None)
        self.transport = FakeTransport(self)
        self.protocol.transport = self.transport
        self.protocol.connectionMade()
        if self.handshake:
            self.deliver(b"RFB 003.003\n")
            self.deliver(pack("!I", self.auth))
            if self.auth == AuthTypes.VNC_AUTHENTICATION:
                self.deliver(b"\x00" * 16)
                return
            self.deliver(pack("!HH16sI", self.width, self.height, PIXEL_FORMAT.to_bytes(), 0))

    def connect_and_accept(self, factory: Any, host: str, port: int, family: Any) -> Deferred:
        attempt = self.connect(factory, host, port, family)
        self.accept()
        return attempt

    # -- server-to-client traffic -----------------------------------------

    def deliver(self, data: bytes) -> None:
        self._pending.append(data)
        self._drain()

    def _drain(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            while self._pending and not self.dropped:
                self.protocol.dataReceived(self._pending.pop(0))
        finally:
            self._busy = False
        if self._drop_pending:
            self.drop()

    def drop_when_idle(self) -> None:
        if self._busy:
            self._drop_pending = True
        else:
            self.drop()

    def on_request(self, incremental: bool) -> None:
        if self.frames:
            self.send_frame(self.frames.pop(0))

    def encode(self, frame: Frame, width: int, height: int) -> bytes:
        if isinstance(frame, bytes):
            return frame
        if isinstance(frame, Image.Image):
            image = frame.convert("RGB")
        else:
            image = Image.new("RGB", (width, height), frame)
        return image.tobytes("raw", "RGBX")

    def send_frame(self, frame: Frame, x: int = 0, y: int = 0, width: int | None = None, height: int | None = None) -> None:
        width = self.width if width is None else width
        height = self.height if height is None else height
        body = self.encode(frame, width, height)
        assert len(body) == width * height * 4, (len(body), width, height)
        self.frames_sent += 1
        self.deliver(self.update([self.rect(x, y, width, height, Encoding.RAW, body)]))

    def send_resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.deliver(self.update([self.rect(0, 0, width, height, Encoding.PSEUDO_DESKTOP_SIZE)]))

    def send_empty_update(self) -> None:
        self.deliver(self.update([]))

    @staticmethod
    def rect(x: int, y: int, w: int, h: int, encoding: int, body: bytes = b"") -> bytes:
        return pack("!HHHHi", x, y, w, h, int(encoding)) + body

    @staticmethod
    def update(rects: list[bytes]) -> bytes:
        return pack("!BxH", MsgS2C.FRAMEBUFFER_UPDATE, len(rects)) + b"".join(rects)

    def drop(self) -> None:
        if self.dropped:
            return
        self.dropped = True
        assert self.transport is not None
        self.transport.closed = True
        self.protocol.connectionLost(Failure(ConnectionDone()))


def settle(deferred: Deferred) -> Any:
    """The value of a Deferred that has already fired; fails a test otherwise."""
    outcome: list[Any] = []
    deferred.addBoth(outcome.append)
    if not outcome:
        raise AssertionError("deferred has not fired")
    result = outcome[0]
    if isinstance(result, Failure):
        result.raiseException()
    return result


def outcome_of(deferred: Deferred) -> Any:
    """Like :func:`settle`, but hands a Failure back instead of raising it."""
    outcome: list[Any] = []
    deferred.addBoth(outcome.append)
    if not outcome:
        raise AssertionError("deferred has not fired")
    return outcome[0]
