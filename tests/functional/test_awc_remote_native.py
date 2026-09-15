"""`awc-remote` against the VNC server native to a hosted CI runner.

On the macOS runner that is Apple Screen Sharing: ARD authentication with a
username, a socket-activated server whose first connection can stall, and
a framebuffer with no rendered desktop behind it. What passes here proves
the staged session and the receipt against the Screen Sharing protocol on
a blank hosted screen; it says nothing about an interactive user desktop,
whose frames, timing and focus rules are a separate measurement.

Hosted runners only: a developer's macOS may well have Screen Sharing on
5900, and it is theirs, so off CI every case skips rather than dialling it.
"""
from __future__ import annotations

import json
import os
from unittest import TestCase, skipUnless

from .test_awc_remote import address, run_awc
from .utils import (
    HOST,
    OS_SERVER_PASSWORD,
    OS_SERVER_TIMEOUT,
    os_servers,
    port_open,
    running_in_ci,
)

# Generous: a hosted Screen Sharing answered a key event in over five seconds.
STAGE_TIMEOUT = str(OS_SERVER_TIMEOUT)


def _env() -> dict[str, str]:
    return {"VNC_PASSWORD": OS_SERVER_PASSWORD}


@skipUnless(running_in_ci(), "hosted CI runner only")
class TestNativeServer(TestCase):
    def setUp(self) -> None:
        servers = os_servers()
        if not servers:
            self.skipTest("no OS-hosted server on this platform")
        self.server = servers[0]
        if not port_open(HOST, self.server.port):
            self.fail(f"{self.server.name} is not listening on {self.server.port}; {self.server.how_to_start}")

    def options(self) -> list[str]:
        opts = ["--connect-timeout", STAGE_TIMEOUT, "--auth-timeout", STAGE_TIMEOUT,
                "--first-frame-timeout", STAGE_TIMEOUT]
        if self.server.username:
            opts += ["--username", self.server.username]
        return opts

    def test_probe_reaches_a_first_frame(self) -> None:
        code, payload, stderr = run_awc(
            "probe", address(self.server), *self.options(), env=_env(), timeout=OS_SERVER_TIMEOUT * 3,
        )

        self.assertEqual(code, 0, f"{stderr}\n{json.dumps(payload, indent=2)}")
        session = payload["session"]
        self.assertTrue(session["ready"])
        self.assertEqual([s["outcome"] for s in session["stages"]], ["ok", "ok", "ok"])
        self.assertGreater(session["frame"]["width"], 0)
        # Recorded, not asserted: a hosted runner has no desktop behind the
        # framebuffer, so black is the expected observation there and a
        # non-black frame would be the surprise worth reading about.
        print(f"\n{self.server.name}: first frame {session['frame']['width']}x{session['frame']['height']} "
              f"black={session['frame']['black']} first_frame took {session['stages'][2]['elapsed']:.2f}s")

    def test_a_key_is_sent_and_the_receipt_is_honest(self) -> None:
        code, payload, stderr = run_awc(
            "act", address(self.server), *self.options(), "--verify", "not-black",
            "--verify-timeout", STAGE_TIMEOUT, "key", "shift", env=_env(), timeout=OS_SERVER_TIMEOUT * 4,
        )

        self.assertIn(code, (0, 60), f"{stderr}\n{json.dumps(payload, indent=2)}")
        receipt = payload["receipt"]
        self.assertTrue(receipt["sent"])
        self.assertIn(receipt["status"], ("verified", "sent", "unknown"))
        print(f"\n{self.server.name}: key shift -> {receipt['status']} ({receipt['reason']}) "
              f"frames_seen={receipt['frames_seen']}")

    def test_the_wrong_password_fails_authenticate_without_a_leak(self) -> None:
        code, payload, _ = run_awc(
            "probe", address(self.server), *self.options(), env={"VNC_PASSWORD": "not-the-password"},
            timeout=OS_SERVER_TIMEOUT * 3,
        )

        self.assertEqual(code, 3, payload)
        self.assertEqual(payload["session"]["failed_stage"], "authenticate")
        self.assertNotIn("not-the-password", json.dumps(payload))
        self.assertNotIn(OS_SERVER_PASSWORD, json.dumps(payload))
        self.assertIsNotNone(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
