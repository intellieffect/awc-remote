"""What a received framebuffer update does and does not prove.

A frame carries three separate facts, and callers conflate them at their
peril:

* **received**: an update committed, at ``received_at``.  This is the only
  freshness clock.  A frame whose pixels equal the previous frame's is a
  fresh observation of a static screen, not a stale one.
* **black**: every pixel is black.  A locked or blanked display looks like
  this, and so does a server that has not painted yet.  It is a reason to
  keep waiting or to look again, not evidence of either.
* **generation**: bumps when the connection is (re)made and when the desktop
  is resized.  Coordinates measured on an earlier generation may point at
  nothing, so an action carries the generation it was planned against.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image


@dataclass(frozen=True)
class FrameInfo:
    sequence: int
    generation: int
    received_at: float
    received_wall: float
    width: int
    height: int
    rectangles: int
    digest: str
    identical_to_previous: bool
    black: bool

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def age(self, now: float | None = None) -> float:
        """Seconds since this frame committed, on the monotonic clock."""
        if now is None:
            now = time.monotonic()
        return max(0.0, now - self.received_at)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def digest_of(image: "Image.Image") -> str:
    return hashlib.blake2b(image.tobytes(), digest_size=16).hexdigest()


def is_black(image: "Image.Image") -> bool:
    extrema = image.getextrema()
    if isinstance(extrema[0], tuple):
        return all(high == 0 for _, high in extrema)
    return extrema[1] == 0


class FrameLog:
    """Metadata for the frames one connection has committed."""

    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._monotonic = monotonic
        self._wall = wall
        self.generation = 0
        self.sequence = 0
        self.latest: FrameInfo | None = None
        self.generation_reasons: list[str] = []

    def new_generation(self, reason: str) -> int:
        self.generation += 1
        self.generation_reasons.append(reason)
        return self.generation

    def record(self, image: "Image.Image", rectangles: int) -> FrameInfo:
        self.sequence += 1
        digest = digest_of(image)
        previous = self.latest
        info = FrameInfo(
            sequence=self.sequence,
            generation=self.generation,
            received_at=self._monotonic(),
            received_wall=self._wall(),
            width=image.width,
            height=image.height,
            rectangles=rectangles,
            digest=digest,
            identical_to_previous=previous is not None and previous.digest == digest,
            black=is_black(image),
        )
        self.latest = info
        return info

    def is_current(self, info: FrameInfo) -> bool:
        """Whether coordinates taken from ``info`` still address this screen."""
        return info.generation == self.generation

    def to_json(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "sequence": self.sequence,
            "latest": None if self.latest is None else self.latest.to_json(),
        }
