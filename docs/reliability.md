# Reliable Sessions: `awc-remote`

A VNC server acknowledges nothing. It does not say that a key press was
applied, that the screen you are looking at is the current one, or that
the desktop is ready for input at all. The `vncdotool.reliability` package
and its `awc-remote` command line put explicit bounds and explicit
outcomes around those three questions, on top of the unchanged
`vncdo`/`api` client.

## Three separate facts

**Received.** A frame is a framebuffer update that painted pixels, stamped
with the time it committed and a sequence number. That timestamp is the only
measure of freshness. A frame whose pixels equal the previous frame's is a
fresh observation of a static screen, and is reported as
`identical_to_previous: true`, never as stale.

**Suspect.** A frame that is entirely black is flagged `black: true`. A
locked or blanked display looks like that, and so does a server that has not
painted yet. The flag is a reason to look again or to wait, not evidence of
a lock and not evidence of a lost connection.

**Verified.** An input is *verified* only when a predicate the caller
supplies holds on a frame received after the input was sent. Pixels having
changed is one available predicate, and a weak one: a clock repaints too.
What an action *meant* is only known to the caller, so meaning is what the
predicate asserts.

Coordinates belong to a **generation**: a counter that advances when the
connection is made and when the desktop is resized. An action planned
against an earlier generation is refused before anything is sent.

## Sessions in stages

`probe` drives a connection through three bounded stages and reports each
one's outcome: `ok`, `timeout`, `failed` or `disconnected`.

| stage | ends when | default bound |
| --- | --- | --- |
| `connect` | the TCP (or WebSocket, or Unix socket) transport is up | 10 s |
| `authenticate` | the RFB handshake and security exchange finish | 15 s |
| `first_frame` | a non-incremental update has covered the whole screen | 30 s |

```
VNC_PASSWORD=... awc-remote probe studio.local --first-frame-timeout 45
```

```json
{
  "ok": true,
  "session": {
    "target": "studio.local:5900",
    "ready": true,
    "failed_stage": null,
    "stages": [
      {"stage": "connect", "outcome": "ok", "elapsed": 0.02, "error": null},
      {"stage": "authenticate", "outcome": "ok", "elapsed": 0.31, "error": null},
      {"stage": "first_frame", "outcome": "ok", "elapsed": 2.9, "error": null}
    ],
    "generation": 1,
    "frame": {"sequence": 1, "width": 2560, "height": 1440, "black": false, "...": "..."},
    "suspect_black": false
  },
  "screenshot": null
}
```

A stage that times out closes the connection and exits 40. Nothing is
retried: whatever was in flight when the bound passed is reported as it
stood, and the decision to try again is the caller's.

Two things seen against macOS Screen Sharing are worth knowing. A wrong
password does not come back as an authentication failure: the server
drops the connection, so the `authenticate` stage ends `disconnected`
(exit 11) rather than `failed` (exit 3), and a few wrong attempts lock
the server for a while so that even the right password disconnects. And
on a macOS host, a Python interpreter that the system has not granted
Local Network access -- a `uv`-managed one, for instance -- fails the
`connect` stage against a LAN or VM address with `No route to host`,
while the same command from a signed interpreter connects; that is the
host's privacy setting, not the target.

The password never appears on the command line. It is read from
`$VNC_PASSWORD` (or the variable named by `--password-env`) or from
`--password-file`. Output and logs carry only whether one was set.
`--screenshot FILE` saves the last frame, and is opt-in because a frame of
someone's desktop is theirs to protect: the file is created new with mode
0600, and an existing path or a symlink is refused rather than overwritten
or written through.

## Actions and receipts

`act` runs a `probe`, sends one input, and settles a **receipt** for it:

| status | meaning | exit |
| --- | --- | --- |
| `verified` | the input was written and the predicate held on a later frame | 0 |
| `sent` | the input was written; no verification was asked for | 0 |
| `unknown` | the input was written and nothing confirmed or denied it in time, or the connection dropped afterwards | 60 |
| `failed` | nothing was sent: not connected, wrong generation or screen size, or a refused coordinate | 61 |

```
awc-remote act studio.local --expect-size 2560x1440 --verify changed click 1180 690
awc-remote act studio.local --verify image --expect-image unlocked.png --at 0,0 key enter
```

`--expect-size WxH` is the cross-process form of the generation check: the
coordinates were measured on a frame of that size, and a first frame of any
other size fails the action without sending it.

`unknown` is the honest answer and stays unknown. A second key press after
an unconfirmed first one is two key presses, so `awc-remote` never resends;
inspect the frame in the receipt and decide. A desktop resize that lands
after the input was sent also settles as `unknown`: the frame that arrived
belongs to a new generation and is not comparable to the one the action
was planned on.

## Cooperative leases

```
awc-remote lease acquire --target studio.local:5900 --owner worker-7 --ttl 300
awc-remote lease renew   --target studio.local:5900 --token TOKEN --ttl 300
awc-remote lease release --target studio.local:5900 --token TOKEN
awc-remote lease status  --target studio.local:5900
```

A lease is exclusive per target, expires at the end of its `--ttl`, and is
released or renewed only with the token that `acquire` printed. A second
`acquire` while a live lease exists exits 50; a release or renew with the
wrong token exits 51. Leases live under `$AWC_REMOTE_LEASE_DIR`, or
`~/.local/state/awc-remote/leases`.

`probe` and `act` take the lease into the run in one of two ways.
`--lease-owner NAME` takes the lease as NAME for exactly this invocation,
sized to its own bounds (every stage timeout plus the verify timeout), and
releases it when the run ends; a target someone else holds exits 50 before
anything is dialled. `--lease-token TOKEN` runs under a lease acquired
earlier, renews it to cover the run, and leaves it held afterwards; a token
that does not hold the target exits 51. In both forms the lease is checked
and renewed again right before the input is written, so it cannot lapse
between the check and the last frame of verification. Without either
option, leases are not consulted at all.

A target is spelled as the server is spelled for `probe` and `act`:
`host:1` and `host::5901` name one lease, recorded as `host:5901`, the
same string a session report carries as `target`. Nothing resolves names:
`localhost`, `127.0.0.1` and the machine's hostname are three targets, and
a server reachable by two addresses can be leased twice. Pick one
spelling per target and use it everywhere.

The guarantee is exactly this: **two processes that go through the same
lease directory will not both believe they hold one target at one time.**
Nothing reaches the VNC server. A person at the console, a Screen Sharing
session, or a VNC client that does not consult the directory is neither
blocked nor noticed. Processes on different machines coordinate only if
the directory is on a filesystem they share and that honours `flock`.
Leases use `fcntl`, which exists on macOS and Linux; on Windows the
`lease` command and the `--lease-*` options are not available.

## From Python

Everything the command line does is available in-process, on Twisted
Deferreds, with the reactor clock and the connector injectable so it can be
driven under test without a server.

```python
from vncdotool.reliability import ReliableFactory, Session, StageTimeouts, perform, actions

factory = ReliableFactory()
factory.password = read_secret()
session = Session(factory, "studio.local", 5900, timeouts=StageTimeouts(first_frame=45))

def on_ready(report):
    client = session.protocol
    return perform(
        client, actions.click(1180, 690),
        verify=actions.region_changed(client, 1100, 650, 200, 80),
        timeout=5, generation=report.generation,
    )

session.start().addCallback(on_ready).addCallback(print)
```

`Session.start()` fires with a `SessionReport`, or fails with a
`SessionError` (`StageTimeout` for the timeout case) that carries the same
report. `perform()` never fails: every outcome is an `ActionReceipt`.

## What this does not do

It does not decide that a black screen is a lock screen, that a screen is
unlocked, or that any text was recognised; a caller with a reference image
or a region of interest supplies the predicate. It does not retrieve or type
passwords on its own. It does not stop anyone else from using the target.
And none of the stages, receipts or leases know anything about the
operating system on the other end beyond what the RFB stream shows.
