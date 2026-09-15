# AWC Remote reliability layer — first implementation

Status: built as `vncdotool/reliability/` and the `awc-remote` console
script; user documentation in `docs/reliability.md`. Live keyboard/mouse
verification against a real desktop has not been run (no isolated VM was
available); the unit suite drives every path through a scripted RFB server
behind a mocked transport.

## Problem
VNC event delivery does not prove that the screen is ready or the intended operation succeeded. Add a headless orchestration layer while preserving vncdotool's protocol/client API and MIT notices.
## Scope and acceptance
1. Bounded connection/authentication/first-frame stages with explicit timeout/error states. Do not retry ambiguous inputs after reconnect.
2. Frame metadata: receive time, sequence/generation, dimensions. Identical pixels can be a valid fresh static screen. A black frame is suspect, not proof of lock or disconnection. New sessions/resizes invalidate obsolete coordinates.
3. Action receipts: sent, verified, unknown/failed. Verification is based on caller-supplied observable predicates, bounded timeout and a post-action frame. Mere pixel change is not semantic success.
4. Target-scoped exclusive leases with owner, expiry, explicit release, owner checks and concurrency tests. State the guarantee boundary: cooperative clients only; cannot claim to lock out arbitrary humans or external VNC clients.
5. Additive Python API and small JSON CLI with injectable transport and meaningful tests. No credentials in arguments/logs/fixtures; screenshots opt-in and private.
## Tests
Use unittest per existing CLAUDE.md. Test delayed/missing/black/identical/resized frames, disconnect after send, verification timeout, two-process lease contention, expiry and non-owner rejection. Preserve relevant upstream tests. Keep concurrency one on this busy machine. Live keyboard/mouse tests only in an isolated VM; unavailable VM means unrun, never a fake pass.
## Deferred
Product GUI, billing, cloud relay, OCR/LLM accuracy claims, unattended password retrieval/unlock, public release and merge.

## Implementation notes

- `frames.py`: `FrameInfo` (sequence, generation, receive time, size, digest, `identical_to_previous`, `black`) and `FrameLog`. Freshness is the receive clock alone.
- `client.py`: `ReliableClient`/`ReliableFactory`, a subclass that records a frame per painting `commitUpdate`, bumps the generation on connect and desktop resize, counts input events, and exposes link-loss and frame listeners. Upstream `client.py`, `rfb.py` and `api.py` are unchanged.
- `session.py`: `Session` with `StageTimeouts`; `connect_endpoint` mirrors `factory_connect` but returns the attempt so a connect timeout can cancel it. A finished session ignores late callbacks and never reconnects.
- `actions.py`: `perform()` settles an `ActionReceipt`; verification keeps an incremental update request outstanding, as `stable` does, and only frames with a sequence past the one at send time count. Predicates: `pixels_changed` (weak), `not_black`, `matches_image`, `region_changed`.
- `lease.py`: `LeaseStore` over `fcntl.flock` and a JSON record per target; token-checked renew/release; expired leases are taken over. POSIX only.
- `cli.py`: `awc-remote probe|act|lease`; `main()` takes the connector, clock and runner so tests run it without the reactor. Exit codes reuse `vncdo`'s where they coincide (3, 10, 11, 40) and add 50/51 for leases and 60/61 for receipts.

## Not verified here

Against a real macOS Screen Sharing server: whether the 15 s first-frame timeout seen earlier recurs, whether closing a concurrent Screen Sharing session was what unblocked it, and how `--verify image` behaves on a 5120x2880 frame. Those need the isolated VM or a scoped run against a target whose owner has agreed.
