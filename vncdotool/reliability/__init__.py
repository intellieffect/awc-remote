"""Headless orchestration on top of the vncdotool client.

Everything here is additive: the protocol, client and ``api`` modules are
untouched, and a caller who wants the plain client keeps using it.  The layer
answers three questions the RFB wire cannot: did the connection reach a usable
first frame within a bound, did an input reach a verifiable outcome, and does
this process hold the cooperative lease on the target it is driving.
"""

from .actions import ActionReceipt, ReceiptStatus, perform
from .client import ReliableClient, ReliableFactory
from .frames import FrameInfo, FrameLog
from .lease import Lease, LeaseError, LeaseHeld, LeaseStore, NotLeaseOwner
from .session import (
    Outcome,
    Session,
    SessionError,
    SessionReport,
    Stage,
    StageResult,
    StageTimeout,
    StageTimeouts,
)

__all__ = [
    "ActionReceipt",
    "FrameInfo",
    "FrameLog",
    "Lease",
    "LeaseError",
    "LeaseHeld",
    "LeaseStore",
    "NotLeaseOwner",
    "Outcome",
    "ReceiptStatus",
    "ReliableClient",
    "ReliableFactory",
    "Session",
    "SessionError",
    "SessionReport",
    "Stage",
    "StageResult",
    "StageTimeout",
    "StageTimeouts",
    "perform",
]
