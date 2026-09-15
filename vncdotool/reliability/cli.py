"""``awc-remote``: the reliability layer as a JSON command line.

One JSON object on stdout per invocation, an exit status that names the
outcome class, and no credential anywhere on the command line, in the
output, or in a log line.  The password comes from an environment variable
or a file.
"""

from __future__ import annotations

import argparse
import enum
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from twisted.internet import reactor as default_reactor
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.python.failure import Failure

from ..command import ExitStatus, parse_server
from . import actions
from .client import ReliableFactory
from .lease import LeaseError, LeaseHeld, LeaseHold, LeaseStore, NotLeaseOwner, check_seconds, hold, take
from .session import Connector, Session, SessionError, StageTimeout, StageTimeouts, connect_endpoint, target_name

log = logging.getLogger(__name__)

DEFAULT_PASSWORD_ENV = "VNC_PASSWORD"

Runner = Callable[[Callable[[], Deferred]], Any]


class ExitCode(enum.IntEnum):
    SUCCESS = ExitStatus.SUCCESS
    ERROR = ExitStatus.ERROR
    USAGE = ExitStatus.USAGE
    AUTHENTICATION_FAILED = ExitStatus.AUTHENTICATION_FAILED
    CONNECTION_FAILED = ExitStatus.CONNECTION_FAILED
    CONNECTION_LOST = ExitStatus.CONNECTION_LOST
    TIMEOUT = ExitStatus.TIMEOUT
    LEASE_HELD = 50
    NOT_LEASE_OWNER = 51
    ACTION_UNKNOWN = 60
    ACTION_FAILED = 61


def run_in_reactor(start: Callable[[], Deferred]) -> Any:
    """Run ``start()`` to completion inside a fresh reactor run and return its result."""
    outcome: list[Any] = []

    def go() -> None:
        try:
            d = start()
        except BaseException:
            outcome.append(Failure())
            default_reactor.stop()
            return
        d.addBoth(outcome.append)
        d.addBoth(lambda _: default_reactor.stop())

    default_reactor.callWhenRunning(go)
    default_reactor.run()
    result = outcome[0]
    if isinstance(result, Failure):
        result.raiseException()
    return result


class JSONArgumentParser(argparse.ArgumentParser):
    """Usage errors are still one JSON object on stdout, exit 2."""

    def error(self, message: str) -> Any:
        json.dump({"ok": False, "error": message, "usage": self.format_usage().strip()}, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        sys.exit(int(ExitCode.USAGE))


def positive_seconds(text: str) -> float:
    try:
        return check_seconds(text, "seconds")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def size_wxh(text: str) -> tuple[int, int]:
    w, sep, h = text.lower().partition("x")
    if not sep or not w.isdigit() or not h.isdigit() or int(w) == 0 or int(h) == 0:
        raise argparse.ArgumentTypeError(f"a size is WIDTHxHEIGHT in pixels, not {text!r}")
    return int(w), int(h)


def read_password(options: argparse.Namespace) -> str | None:
    if options.password_file:
        return Path(options.password_file).read_text().rstrip("\r\n")
    env = options.password_env or DEFAULT_PASSWORD_ENV
    return os.environ.get(env)


def build_parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(
        prog="awc-remote",
        description="Bounded VNC sessions, verified inputs and cooperative leases, reported as JSON",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    def connection_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("server", help="address[:display|::port], ws:// or wss:// URL, or a unix socket path")
        p.add_argument("--username")
        p.add_argument("--password-env", metavar="NAME",
                       help=f"environment variable holding the password [{DEFAULT_PASSWORD_ENV}]")
        p.add_argument("--password-file", metavar="FILE", help="file holding the password; trailing newline ignored")
        p.add_argument("--connect-timeout", type=positive_seconds, default=StageTimeouts.connect, metavar="S")
        p.add_argument("--auth-timeout", type=positive_seconds, default=StageTimeouts.authenticate, metavar="S")
        p.add_argument("--first-frame-timeout", type=positive_seconds, default=StageTimeouts.first_frame, metavar="S")
        p.add_argument("--screenshot", metavar="FILE",
                       help="also save the last frame to a new FILE, readable by you alone; an existing path is refused")
        lease = p.add_mutually_exclusive_group()
        lease.add_argument("--lease-token", metavar="TOKEN",
                           help="run only while this token holds the lease on the server, renewing it to cover the run")
        lease.add_argument("--lease-owner", metavar="NAME",
                           help="take the lease on the server as NAME for this run and release it afterwards")
        p.add_argument("--lease-dir", metavar="DIR", help=argparse.SUPPRESS)

    probe = sub.add_parser("probe", help="connect, authenticate and receive a first frame within bounds")
    connection_options(probe)

    act = sub.add_parser("act", help="probe, send one input, and verify it against a frame received afterwards")
    connection_options(act)
    act.add_argument("--expect-size", type=size_wxh, metavar="WxH",
                     help="refuse to act unless the first frame has this size (coordinates were measured on it)")
    act.add_argument("--verify", choices=["none", "changed", "not-black", "image"], default="none",
                     help="what counts as verified: nothing (report sent), any pixel change (weak), a non-black frame, "
                          "or --expect-image matching")
    act.add_argument("--expect-image", metavar="FILE", help="reference image for --verify image")
    act.add_argument("--at", metavar="X,Y", help="where --expect-image is compared, default 0,0")
    act.add_argument("--fuzz", type=int, metavar="N")
    act.add_argument("--verify-timeout", type=positive_seconds, default=5.0, metavar="S")
    act_sub = act.add_subparsers(dest="action", required=True)
    act_sub.add_parser("key").add_argument("key")
    act_sub.add_parser("type").add_argument("text")
    click = act_sub.add_parser("click")
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    click.add_argument("--button", type=int, default=1)
    move = act_sub.add_parser("move")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)

    lease = sub.add_parser("lease", help="cooperative exclusive lease on a target")
    lease.add_argument("--lease-dir", metavar="DIR", help="[$AWC_REMOTE_LEASE_DIR or ~/.local/state/awc-remote/leases]")
    lease_sub = lease.add_subparsers(dest="lease_command", required=True)
    acquire = lease_sub.add_parser("acquire")
    acquire.add_argument("--target", required=True, help="the server, spelled as probe/act take it")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--ttl", type=positive_seconds, default=300.0, metavar="S")
    renew = lease_sub.add_parser("renew")
    renew.add_argument("--target", required=True)
    renew.add_argument("--token", required=True)
    renew.add_argument("--ttl", type=positive_seconds, default=300.0, metavar="S")
    release = lease_sub.add_parser("release")
    release.add_argument("--target", required=True)
    release.add_argument("--token", required=True)
    status = lease_sub.add_parser("status")
    status.add_argument("--target", required=True)
    return parser


def canonical_target(spelling: str) -> str:
    """A lease target as a session reports it, whatever spelling was given."""
    try:
        family, host, port = parse_server(spelling)
    except (ValueError, OSError):
        return spelling
    return target_name(family, host, port)


def _run_budget(options: argparse.Namespace) -> float:
    """How long this invocation may hold the target, from dialling to the last frame."""
    budget = options.connect_timeout + options.auth_timeout + options.first_frame_timeout
    if getattr(options, "verify_timeout", None):
        budget += options.verify_timeout
    return budget + actions.LEASE_MARGIN


def _lease_hold(options: argparse.Namespace) -> LeaseHold | tuple[dict[str, Any], ExitCode] | None:
    """A hold covering the run, a refusal, or None when no lease was asked for."""
    if not options.lease_token and not options.lease_owner:
        return None
    target = canonical_target(options.server)
    store = LeaseStore(options.lease_dir)
    try:
        if options.lease_owner:
            return take(store, target, options.lease_owner, _run_budget(options))
        held = hold(store, target, options.lease_token)
        held.ensure(_run_budget(options))
        return held
    except LeaseHeld as exc:
        return {"ok": False, "error": str(exc), "target": target, "lease": exc.holder.to_json()}, ExitCode.LEASE_HELD
    except NotLeaseOwner as exc:
        return {"ok": False, "error": str(exc), "target": target}, ExitCode.NOT_LEASE_OWNER


def _release(held: LeaseHold | None) -> None:
    if held is not None and held.owned:
        try:
            held.release()
        except LeaseError as exc:
            log.warning("could not release %s: %s", held.target, exc)


def _build_session(options: argparse.Namespace, connect: Connector, clock: Any) -> Session:
    family, host, port = parse_server(options.server)
    factory = ReliableFactory()
    factory.username = options.username
    factory.password = read_password(options)
    timeouts = StageTimeouts(options.connect_timeout, options.auth_timeout, options.first_frame_timeout)
    return Session(factory, host, port, family, timeouts=timeouts, clock=clock, connect=connect)


def _session_failure(exc: SessionError) -> tuple[dict[str, Any], ExitCode]:
    report = exc.report.to_json()
    if isinstance(exc, StageTimeout):
        code = ExitCode.TIMEOUT
    elif exc.outcome is not None and exc.outcome.value == "disconnected":
        code = ExitCode.CONNECTION_LOST
    elif exc.stage.value == "authenticate":
        code = ExitCode.AUTHENTICATION_FAILED
    else:
        code = ExitCode.CONNECTION_FAILED
    return {"ok": False, "session": report}, code


def _screenshot(session: Session, path: str | None) -> str | None:
    """Save the last frame to a new file only this user can read.

    The file is created exclusively and without following a symlink, so a
    path someone else prepared is refused rather than overwritten or
    written through.
    """
    if path is None or session.protocol is None or session.protocol.screen is None:
        return None
    from PIL import Image

    suffix = Path(path).suffix.lower()
    image_format = Image.registered_extensions().get(suffix)
    if image_format is None:
        raise ValueError(f"cannot tell an image format from {path!r}; use an extension such as .png")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fp:
        session.protocol.renderScreen().save(fp, format=image_format)
    return path


def _report_error(exc: BaseException, session: Session | None = None) -> tuple[dict[str, Any], ExitCode]:
    payload: dict[str, Any] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if session is not None:
        payload["session"] = session.report.to_json()
    return payload, ExitCode.ERROR


@inlineCallbacks
def _probe(options: argparse.Namespace, connect: Connector, clock: Any) -> Any:
    held = _lease_hold(options)
    if isinstance(held, tuple):
        return held
    session = _build_session(options, connect, clock)
    try:
        try:
            report = yield session.start()
        except SessionError as exc:
            return _session_failure(exc)
        try:
            shot = _screenshot(session, options.screenshot)
        except (OSError, ValueError) as exc:
            return _report_error(exc, session)
        return {"ok": True, "session": report.to_json(), "screenshot": shot}, ExitCode.SUCCESS
    finally:
        yield session.close()
        _release(held)


def _predicate(options: argparse.Namespace) -> actions.Predicate | None:
    if options.verify == "none":
        return None
    if options.verify == "changed":
        return actions.pixels_changed
    if options.verify == "not-black":
        return actions.not_black
    from PIL import Image

    if not options.expect_image:
        raise ValueError("--verify image needs --expect-image FILE")
    x, y = (0, 0)
    if options.at:
        x, y = (int(v) for v in options.at.split(","))
    return actions.matches_image(Image.open(options.expect_image), x, y, fuzz=options.fuzz)


def _action(options: argparse.Namespace) -> actions.Action:
    if options.action == "key":
        return actions.key_press(options.key)
    if options.action == "type":
        return actions.type_text(options.text)
    if options.action == "click":
        return actions.click(options.x, options.y, options.button)
    return actions.mouse_move(options.x, options.y)


@inlineCallbacks
def _act(options: argparse.Namespace, connect: Connector, clock: Any) -> Any:
    try:
        verify = _predicate(options)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}, ExitCode.USAGE
    action = _action(options)
    held = _lease_hold(options)
    if isinstance(held, tuple):
        return held

    session = _build_session(options, connect, clock)
    try:
        try:
            report = yield session.start()
        except SessionError as exc:
            return _session_failure(exc)
        protocol = session.protocol
        assert protocol is not None and report.frame is not None

        if options.expect_size and options.expect_size != report.frame.size:
            receipt = actions.ActionReceipt(
                action=action.__name__, status=actions.ReceiptStatus.FAILED,
                generation=report.generation, sequence_before=report.frame.sequence,
                reason=f"screen is {report.frame.width}x{report.frame.height}, not "
                       f"{options.expect_size[0]}x{options.expect_size[1]}; nothing sent",
            )
        else:
            receipt = yield actions.perform(
                protocol, action, verify=verify, timeout=options.verify_timeout,
                generation=report.generation, lease=held, clock=clock,
            )
        try:
            shot = _screenshot(session, options.screenshot)
        except (OSError, ValueError) as exc:
            payload, code = _report_error(exc, session)
            payload["receipt"] = receipt.to_json()
            return payload, code
        code = {
            actions.ReceiptStatus.VERIFIED: ExitCode.SUCCESS,
            actions.ReceiptStatus.SENT: ExitCode.SUCCESS,
            actions.ReceiptStatus.UNKNOWN: ExitCode.ACTION_UNKNOWN,
            actions.ReceiptStatus.FAILED: ExitCode.ACTION_FAILED,
        }[receipt.status]
        return {
            "ok": code is ExitCode.SUCCESS,
            "session": report.to_json(),
            "receipt": receipt.to_json(),
            "screenshot": shot,
        }, code
    finally:
        yield session.close()
        _release(held)


def _lease(options: argparse.Namespace) -> tuple[dict[str, Any], ExitCode]:
    store = LeaseStore(options.lease_dir)
    target = canonical_target(options.target)
    try:
        if options.lease_command == "acquire":
            lease = store.acquire(target, options.owner, options.ttl)
            return {"ok": True, "lease": lease.to_json(reveal_token=True)}, ExitCode.SUCCESS
        if options.lease_command == "renew":
            lease = store.renew(target, options.token, options.ttl)
            return {"ok": True, "lease": lease.to_json(reveal_token=True)}, ExitCode.SUCCESS
        if options.lease_command == "release":
            store.release(target, options.token)
            return {"ok": True, "released": target}, ExitCode.SUCCESS
        current = store.status(target)
        return {"ok": True, "held": current is not None,
                "lease": None if current is None else current.to_json()}, ExitCode.SUCCESS
    except LeaseHeld as exc:
        return {"ok": False, "error": str(exc), "lease": exc.holder.to_json()}, ExitCode.LEASE_HELD
    except NotLeaseOwner as exc:
        return {"ok": False, "error": str(exc)}, ExitCode.NOT_LEASE_OWNER
    except (LeaseError, ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}, ExitCode.ERROR


def main(
    argv: list[str] | None = None,
    *,
    connect: Connector = connect_endpoint,
    clock: Any = default_reactor,
    runner: Runner = run_in_reactor,
    stdout: Any = None,
) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if options.verbose > 1 else logging.INFO if options.verbose else logging.WARNING)
    out = stdout or sys.stdout

    try:
        if options.command == "lease":
            payload, code = _lease(options)
        else:
            handler = _probe if options.command == "probe" else _act
            payload, code = runner(lambda: handler(options, connect, clock))
    except Exception as exc:  # noqa: BLE001 -- the contract is JSON out, whatever went wrong
        log.debug("unhandled", exc_info=True)
        payload, code = _report_error(exc)

    json.dump(payload, out, indent=2, sort_keys=True)
    out.write("\n")
    return int(code)


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
