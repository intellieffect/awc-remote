"""Cooperative, target-scoped exclusive leases.

The guarantee is exactly this: two processes that both go through the same
lease directory will not both believe they hold the lease on one target at
one time.  Nothing here reaches the VNC server.  A person at the console, a
Screen Sharing session, or any VNC client that does not consult this
directory is neither blocked nor detected.  Coordinating across machines
needs the directory on a filesystem they share; the lock is ``fcntl.flock``,
so a network filesystem that does not honour it voids the guarantee.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import socket
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

DEFAULT_TTL = 300.0
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def check_seconds(value: float | str, what: str) -> float:
    """A finite, positive number of seconds, or ``ValueError`` naming ``what``."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number of seconds, not {value!r}") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{what} must be finite and positive, not {value!r}")
    return seconds


class LeaseError(Exception):
    pass


class LeaseHeld(LeaseError):
    def __init__(self, holder: "Lease") -> None:
        super().__init__(
            f"{holder.target} is leased by {holder.owner!r} until "
            f"{holder.expires_at:.0f} (pid {holder.pid} on {holder.host})"
        )
        self.holder = holder


class NotLeaseOwner(LeaseError):
    pass


@dataclass(frozen=True)
class Lease:
    target: str
    owner: str
    token: str
    acquired_at: float
    expires_at: float
    pid: int
    host: str

    def remaining(self, now: float | None = None) -> float:
        if now is None:
            now = time.time()
        return max(0.0, self.expires_at - now)

    def expired(self, now: float | None = None) -> bool:
        return self.remaining(now) <= 0

    def to_json(self, reveal_token: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not reveal_token:
            data["token"] = None
        return data


def default_directory() -> Path:
    env = os.environ.get("AWC_REMOTE_LEASE_DIR")
    if env:
        return Path(env)
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(state) / "awc-remote" / "leases"


def slug(target: str) -> str:
    """A filename for a target that is readable and cannot collide."""
    short = hashlib.blake2b(target.encode("utf-8"), digest_size=6).hexdigest()
    return f"{_SLUG.sub('_', target)[:60]}-{short}"


class LeaseStore:
    def __init__(self, directory: Path | str | None = None, clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory) if directory is not None else default_directory()
        self.clock = clock

    def _paths(self, target: str) -> tuple[Path, Path]:
        # Not with_suffix: a target such as "studio.example:5900" has a dot
        # of its own, and with_suffix would cut the name there.
        name = slug(target)
        return self.directory / (name + ".lock"), self.directory / (name + ".json")

    @contextmanager
    def _locked(self, target: str) -> Iterator[Path]:
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path, record_path = self._paths(target)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield record_path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _read(record_path: Path) -> Lease | None:
        try:
            data = json.loads(record_path.read_text())
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            return None
        try:
            return Lease(**data)
        except TypeError:
            return None

    @staticmethod
    def _write(record_path: Path, lease: Lease) -> None:
        tmp = record_path.with_name(record_path.name + ".tmp")
        with open(tmp, "w", opener=lambda p, f: os.open(p, f, 0o600)) as fp:
            json.dump(asdict(lease), fp)
        os.replace(tmp, record_path)

    def acquire(self, target: str, owner: str, ttl: float = DEFAULT_TTL) -> Lease:
        """Take the lease on ``target``, or raise :class:`LeaseHeld`.

        An expired lease is taken over regardless of who held it.  Holding a
        lease already, even as the same owner, does not grant a second one:
        renew the one you have.
        """
        ttl = check_seconds(ttl, "ttl")
        now = self.clock()
        with self._locked(target) as record_path:
            current = self._read(record_path)
            if current is not None and not current.expired(now):
                raise LeaseHeld(current)
            lease = Lease(
                target=target,
                owner=owner,
                token=secrets.token_hex(16),
                acquired_at=now,
                expires_at=now + ttl,
                pid=os.getpid(),
                host=socket.gethostname(),
            )
            self._write(record_path, lease)
            return lease

    def renew(self, target: str, token: str, ttl: float = DEFAULT_TTL) -> Lease:
        """Extend a lease you hold; raises :class:`NotLeaseOwner` otherwise."""
        ttl = check_seconds(ttl, "ttl")
        now = self.clock()
        with self._locked(target) as record_path:
            current = self._read(record_path)
            if current is None or current.expired(now):
                raise NotLeaseOwner(f"no live lease on {target} to renew")
            if not secrets.compare_digest(current.token, token):
                raise NotLeaseOwner(f"{target} is leased by {current.owner!r}, not by this token")
            renewed = Lease(**{**asdict(current), "expires_at": now + ttl})
            self._write(record_path, renewed)
            return renewed

    def release(self, target: str, token: str) -> None:
        """Give the lease back; raises :class:`NotLeaseOwner` for another's lease.

        Releasing a lease that has already expired or was never taken is not
        an error: the caller's intent, that it holds nothing, is satisfied.
        """
        now = self.clock()
        with self._locked(target) as record_path:
            current = self._read(record_path)
            if current is None or current.expired(now):
                return
            if not secrets.compare_digest(current.token, token):
                raise NotLeaseOwner(f"{target} is leased by {current.owner!r}, not by this token")
            record_path.unlink()

    def held_by(self, target: str, token: str) -> Lease:
        """The live lease on ``target`` if ``token`` is its token; else :class:`NotLeaseOwner`."""
        current = self.status(target)
        if current is None:
            raise NotLeaseOwner(f"no live lease on {target}")
        if not secrets.compare_digest(current.token, token):
            raise NotLeaseOwner(f"{target} is leased by {current.owner!r}, not by this token")
        return current

    def status(self, target: str) -> Lease | None:
        """The live lease on ``target``, or None."""
        now = self.clock()
        with self._locked(target) as record_path:
            current = self._read(record_path)
        if current is None or current.expired(now):
            return None
        return current


class LeaseHold:
    """A lease this process holds, kept alive across one bounded operation.

    :meth:`ensure` is what an action calls right before it writes input: it
    proves the lease is still this process's and pushes the expiry past the
    operation's own bound, so the lease cannot lapse between the check and
    the last byte of verification.
    """

    def __init__(self, store: LeaseStore, lease: Lease, owned: bool = False) -> None:
        self.store = store
        self.lease = lease
        # Whether this hold took the lease itself (and so gives it back), or
        # was handed a token by whoever did.
        self.owned = owned

    @property
    def target(self) -> str:
        return self.lease.target

    def ensure(self, seconds: float) -> Lease:
        """Renew so at least ``seconds`` remain; :class:`NotLeaseOwner` if it is not ours."""
        seconds = check_seconds(seconds, "seconds")
        now = self.store.clock()
        remaining = self.lease.remaining(now)
        ttl = max(seconds, remaining)
        self.lease = self.store.renew(self.lease.target, self.lease.token, ttl)
        return self.lease

    def release(self) -> None:
        self.store.release(self.lease.target, self.lease.token)


def take(store: LeaseStore, target: str, owner: str, ttl: float = DEFAULT_TTL) -> LeaseHold:
    """Acquire ``target`` for this process and hand back a hold that releases it."""
    return LeaseHold(store, store.acquire(target, owner, ttl), owned=True)


def hold(store: LeaseStore, target: str, token: str) -> LeaseHold:
    """Adopt a lease someone else acquired; releasing stays their job."""
    return LeaseHold(store, store.held_by(target, token), owned=False)
