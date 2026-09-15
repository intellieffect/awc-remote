"""Bounded connection stages: connect, authenticate, first frame.

Each stage has its own timeout and ends in exactly one outcome.  A session
that fails or times out is left closed and is not retried: the input a
caller sent before an ambiguous failure may or may not have landed, and
only the caller can decide what to do about that.
"""

from __future__ import annotations

import enum
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Callable

from twisted.internet import reactor as default_reactor
from twisted.internet.defer import Deferred
from twisted.internet.endpoints import HostnameEndpoint, UNIXClientEndpoint
from twisted.internet.error import ConnectionClosed
from twisted.python.failure import Failure

from .. import websocket
from ..client import AuthenticationError, VNCDoException
from .client import ReliableClient, ReliableFactory
from .frames import FrameInfo

log = logging.getLogger(__name__)

Connector = Callable[[ReliableFactory, str, int, websocket.AddressFamily], Deferred]


class Stage(str, enum.Enum):
    CONNECT = "connect"
    AUTHENTICATE = "authenticate"
    FIRST_FRAME = "first_frame"


class Outcome(str, enum.Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class StageTimeouts:
    connect: float = 10.0
    authenticate: float = 15.0
    first_frame: float = 30.0

    def for_stage(self, stage: Stage) -> float:
        return {
            Stage.CONNECT: self.connect,
            Stage.AUTHENTICATE: self.authenticate,
            Stage.FIRST_FRAME: self.first_frame,
        }[stage]


@dataclass
class StageResult:
    stage: Stage
    outcome: Outcome | None = None
    started_at: float = 0.0
    ended_at: float | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float | None:
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    def to_json(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "outcome": None if self.outcome is None else self.outcome.value,
            "elapsed": self.elapsed,
            "error": self.error,
        }


@dataclass
class SessionReport:
    target: str
    stages: list[StageResult] = field(default_factory=list)
    frame: FrameInfo | None = None
    generation: int = 0

    @property
    def ready(self) -> bool:
        return len(self.stages) == len(Stage) and all(s.outcome is Outcome.OK for s in self.stages)

    @property
    def failed_stage(self) -> StageResult | None:
        for result in self.stages:
            if result.outcome not in (None, Outcome.OK):
                return result
        return None

    def to_json(self) -> dict[str, Any]:
        failed = self.failed_stage
        return {
            "target": self.target,
            "ready": self.ready,
            "failed_stage": None if failed is None else failed.stage.value,
            "stages": [s.to_json() for s in self.stages],
            "generation": self.generation,
            "frame": None if self.frame is None else self.frame.to_json(),
            "suspect_black": self.frame is not None and self.frame.black,
        }


class SessionError(VNCDoException):
    def __init__(self, report: SessionReport, cause: BaseException | None = None) -> None:
        failed = report.failed_stage
        assert failed is not None
        super().__init__(f"{failed.stage.value} {failed.outcome.value if failed.outcome else ''}: {failed.error}")
        self.report = report
        self.stage = failed.stage
        self.outcome = failed.outcome
        self.cause = cause


class StageTimeout(SessionError):
    pass


def connect_endpoint(
    factory: ReliableFactory, host: str, port: int, family: websocket.AddressFamily
) -> Deferred:
    """As :func:`vncdotool.client.factory_connect`, returning the attempt so it can be cancelled."""
    if family is websocket.WEBSOCKET:
        return websocket.connect(default_reactor, factory, host)
    if family in {socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6}:
        factory.tls_hostname = host
        return HostnameEndpoint(default_reactor, host, port).connect(factory)
    if hasattr(socket, "AF_UNIX") and family == socket.AF_UNIX:
        return UNIXClientEndpoint(default_reactor, host).connect(factory)
    raise ValueError(family)


class Session:
    """Drive one connection through its stages within the given bounds.

    ``clock`` and ``connect`` are injectable so a test can run the whole
    state machine against a mocked transport without starting the reactor.
    """

    def __init__(
        self,
        factory: ReliableFactory,
        host: str,
        port: int = 5900,
        family: websocket.AddressFamily = socket.AF_INET,
        timeouts: StageTimeouts | None = None,
        clock: Any = default_reactor,
        connect: Connector = connect_endpoint,
    ) -> None:
        self.factory = factory
        self.host = host
        self.port = port
        self.family = family
        self.timeouts = timeouts or StageTimeouts()
        self.clock = clock
        self.connect = connect
        self.report = SessionReport(target=f"{host}:{port}")
        self.protocol: ReliableClient | None = None
        self.result: Deferred = Deferred()
        self._attempt: Deferred | None = None
        self._timer: Any = None
        self._current: StageResult | None = None
        self._cancel_link_watch: Callable[[], None] | None = None
        self._finished = False

    # -- stage bookkeeping -------------------------------------------------

    def _begin(self, stage: Stage) -> None:
        self._current = StageResult(stage=stage, started_at=self.clock.seconds())
        self.report.stages.append(self._current)
        self._timer = self.clock.callLater(self.timeouts.for_stage(stage), self._timed_out, stage)

    def _end(self, outcome: Outcome, error: str | None = None) -> StageResult:
        assert self._current is not None
        if self._timer is not None and self._timer.active():
            self._timer.cancel()
        self._timer = None
        self._current.outcome = outcome
        self._current.ended_at = self.clock.seconds()
        self._current.error = error
        return self._current

    def _fail(self, outcome: Outcome, error: str, cause: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._end(outcome, error)
        self._close_transport()
        exc_type = StageTimeout if outcome is Outcome.TIMEOUT else SessionError
        self.result.errback(exc_type(self.report, cause))

    def _close_transport(self) -> None:
        if self._cancel_link_watch is not None:
            self._cancel_link_watch()
            self._cancel_link_watch = None
        if self.protocol is not None and self.protocol.link_up:
            transport = self.protocol.transport
            # ITransport promises only loseConnection; a TCP transport also
            # aborts, which does not wait for buffered writes to drain.
            abort = getattr(transport, "abortConnection", transport.loseConnection)
            abort()
        elif self._attempt is not None and not self._attempt.called:
            self._attempt.cancel()

    def _timed_out(self, stage: Stage) -> None:
        self._fail(Outcome.TIMEOUT, f"{stage.value} exceeded {self.timeouts.for_stage(stage)}s")

    # -- stages ------------------------------------------------------------

    def start(self) -> Deferred:
        """Returns a Deferred: :class:`SessionReport` when ready, else :class:`SessionError`."""
        self.factory.onTransportConnected(self._transport_connected)
        self.factory.deferred.addCallbacks(self._authenticated, self._connection_failed)
        self._begin(Stage.CONNECT)
        try:
            self._attempt = self.connect(self.factory, self.host, self.port, self.family)
        except Exception as exc:
            self._fail(Outcome.FAILED, str(exc), exc)
            return self.result
        if self._attempt is not None:
            self._attempt.addErrback(self._attempt_failed)
        return self.result

    def _attempt_failed(self, reason: Failure) -> None:
        # factory_connect routes this through clientConnectionFailed and the
        # factory deferred; an already-finished session swallows the echo.
        if self._finished:
            return
        self.factory.clientConnectionFailed(None, reason)  # type: ignore[arg-type]

    def _transport_connected(self, protocol: ReliableClient) -> None:
        if self._finished:
            return
        self.protocol = protocol
        self._cancel_link_watch = protocol.onLinkLost(self._link_lost)
        self._end(Outcome.OK)
        self._begin(Stage.AUTHENTICATE)

    def _connection_failed(self, reason: Failure) -> None:
        if self._finished:
            return None
        outcome = Outcome.DISCONNECTED if reason.check(ConnectionClosed) else Outcome.FAILED
        error = reason.getErrorMessage()
        if reason.check(AuthenticationError):
            error = f"authentication failed: {error}"
        self._fail(outcome, error, reason.value)
        return None

    def _link_lost(self, reason: Failure) -> None:
        if self._finished:
            return
        self._fail(Outcome.DISCONNECTED, f"connection lost: {reason.getErrorMessage()}", reason.value)

    def _authenticated(self, protocol: ReliableClient) -> ReliableClient:
        if self._finished:
            return protocol
        self.protocol = protocol
        self._end(Outcome.OK)
        self._begin(Stage.FIRST_FRAME)
        protocol.refreshScreen(incremental=False).addCallback(self._first_frame)
        return protocol

    def _first_frame(self, protocol: ReliableClient) -> None:
        if self._finished:
            return
        self._finished = True
        self._end(Outcome.OK)
        self.report.frame = protocol.frames.latest
        self.report.generation = protocol.frames.generation
        if self.report.frame is not None and self.report.frame.black:
            log.info("first frame from %s is all black; treating as suspect, not as a lock", self.report.target)
        self.result.callback(self.report)

    # -- teardown ----------------------------------------------------------

    def close(self) -> Deferred:
        """Drop the connection; fires once the transport reports it gone."""
        done: Deferred = Deferred()
        protocol = self.protocol
        if protocol is None or not protocol.link_up:
            done.callback(None)
            return done
        protocol.onLinkLost(lambda _reason: done.callback(None))
        protocol.transport.loseConnection()
        return done
