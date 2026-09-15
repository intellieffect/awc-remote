"""`awc-remote` end to end against the container fleet.

Every scenario shells out to the real console script, as the rest of this
suite does for `vncdo`, so a hang is bounded by the subprocess timeout and
not by anything in-process.  The fleet is Linux servers on loopback: it
proves the staged session, the receipts and the lease gate against real
RFB servers, and says nothing about macOS Screen Sharing.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest import TestCase

from PIL import Image

from tests.goldens import scenes
from vncdotool import rfb

from .utils import (
    HOST,
    LIBVNCSERVER_EXAMPLE,
    SUBPROCESS_TIMEOUT_HEADROOM,
    TIGERVNC,
    TIGERVNC_AUTH,
    VNCEV,
    VNCServer,
    normalize_size,
    port_open,
)

AWC_REMOTE = str(Path(sys.executable).parent / "awc-remote")
SCENE_DIR = Path(scenes.__file__).resolve().parent / "scenes"
# The scene player repaints asynchronously to the key event reaching the X
# server; a few seconds covers a loaded machine.
VERIFY_TIMEOUT = 8.0


def address(server: VNCServer) -> str:
    return f"{HOST}::{server.port}"


def run_awc(
    *args: str, env: Optional[Mapping[str, str]] = None, timeout: float = 30.0
) -> tuple[int, dict[str, Any], str]:
    # A password in the test runner's own environment must not leak into a
    # scenario that expects to be unauthenticated.
    child_env = {k: v for k, v in os.environ.items() if k != "VNC_PASSWORD"}
    child_env.update(env or {})
    try:
        result = subprocess.run(
            [AWC_REMOTE, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env=child_env, timeout=timeout + SUBPROCESS_TIMEOUT_HEADROOM,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"`awc-remote {' '.join(args)}` did not finish within {timeout}s") from exc
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise AssertionError(
            f"no JSON on stdout (exit {result.returncode}); stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        )
    return result.returncode, payload, result.stderr


class FleetCase(TestCase):
    server: VNCServer = TIGERVNC

    def setUp(self) -> None:
        if not port_open(HOST, self.server.port):
            self.fail(f"{self.server.name} is not listening on {self.server.port}; {self.server.how_to_start}")


class TestProbe(FleetCase):
    def test_reaches_a_first_frame_and_names_the_stages(self) -> None:
        code, payload, stderr = run_awc("probe", address(TIGERVNC))

        self.assertEqual(code, 0, stderr)
        session = payload["session"]
        self.assertTrue(session["ready"])
        self.assertEqual([s["stage"] for s in session["stages"]], ["connect", "authenticate", "first_frame"])
        self.assertEqual([s["outcome"] for s in session["stages"]], ["ok", "ok", "ok"])
        self.assertEqual((session["frame"]["width"], session["frame"]["height"]), TIGERVNC.size)
        self.assertFalse(session["suspect_black"])
        self.assertEqual(session["generation"], 1)
        self.assertEqual(session["target"], f"{HOST}:{TIGERVNC.port}")

    def test_screenshot_is_the_frame_it_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frame.png")
            code, payload, stderr = run_awc("probe", address(TIGERVNC), "--screenshot", path)

            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["screenshot"], path)
            with Image.open(path) as image:
                self.assertEqual(image.size, TIGERVNC.size)

    def test_refused_port_fails_the_connect_stage(self) -> None:
        code, payload, _ = run_awc("probe", f"{HOST}::1", "--connect-timeout", "3")

        self.assertEqual(code, 10)
        self.assertEqual(payload["session"]["failed_stage"], "connect")
        self.assertEqual(payload["session"]["stages"][0]["outcome"], "failed")


class TestProbeAuthentication(FleetCase):
    server = TIGERVNC_AUTH

    def test_password_from_the_environment_authenticates(self) -> None:
        code, payload, stderr = run_awc("probe", address(TIGERVNC_AUTH), env={"VNC_PASSWORD": TIGERVNC_AUTH.password})

        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["session"]["ready"])
        self.assertNotIn(TIGERVNC_AUTH.password, json.dumps(payload))

    def test_password_from_a_file_authenticates(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False) as fp:
            fp.write(TIGERVNC_AUTH.password + "\n")
        self.addCleanup(os.unlink, fp.name)

        code, payload, stderr = run_awc("probe", address(TIGERVNC_AUTH), "--password-file", fp.name)

        self.assertEqual(code, 0, stderr)

    def test_no_password_fails_the_authenticate_stage(self) -> None:
        code, payload, _ = run_awc("probe", address(TIGERVNC_AUTH))

        self.assertEqual(code, 3)
        self.assertEqual(payload["session"]["failed_stage"], "authenticate")

    def test_wrong_password_fails_the_authenticate_stage(self) -> None:
        code, payload, _ = run_awc("probe", address(TIGERVNC_AUTH), env={"VNC_PASSWORD": "not-it"})

        self.assertEqual(code, 3)
        self.assertEqual(payload["session"]["failed_stage"], "authenticate")
        self.assertNotIn("not-it", json.dumps(payload))


class TestProbeEventSink(FleetCase):
    """vncev renders no desktop, but it does answer a refresh with a small frame."""

    server = VNCEV

    def test_an_event_sink_still_reaches_a_first_frame(self) -> None:
        code, payload, stderr = run_awc("probe", address(VNCEV), timeout=15)

        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["session"]["ready"])
        self.assertGreater(payload["session"]["frame"]["rectangles"], 0)


class SilentAfterHandshake(threading.Thread):
    """A TCP server that completes the RFB 3.3 handshake and then says nothing.

    No fleet server stalls like this on purpose, and the first-frame bound is
    the case the earlier Screen Sharing probing tripped over, so the stall is
    scripted here on a real socket.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.listener = socket.socket()
        self.listener.bind((HOST, 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.handshakes = 0
        self.closed = threading.Event()

    def run(self) -> None:
        while not self.closed.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.sendall(b"RFB 003.003\n")
                    conn.recv(12)
                    conn.sendall(struct.pack("!I", 1))
                    conn.recv(1)
                    conn.sendall(struct.pack("!HH16sI", 100, 100, rfb.PixelFormat().to_bytes(), 0))
                    self.handshakes += 1
                    while not self.closed.is_set() and conn.recv(4096):
                        pass
                except OSError:
                    pass

    def stop(self) -> None:
        self.closed.set()
        self.listener.close()


class TestFirstFrameTimeout(TestCase):
    def test_a_server_that_never_paints_times_out_at_first_frame(self) -> None:
        stall = SilentAfterHandshake()
        stall.start()
        self.addCleanup(stall.stop)

        code, payload, _ = run_awc("probe", f"{HOST}::{stall.port}", "--first-frame-timeout", "2", timeout=15)

        self.assertEqual(code, 40, payload)
        session = payload["session"]
        self.assertEqual(session["failed_stage"], "first_frame")
        self.assertEqual([s["outcome"] for s in session["stages"]], ["ok", "ok", "timeout"])
        self.assertGreaterEqual(session["stages"][2]["elapsed"], 2.0)
        self.assertIsNone(session["frame"])
        self.assertEqual(stall.handshakes, 1)

    def test_an_action_is_not_sent_when_the_session_never_became_ready(self) -> None:
        stall = SilentAfterHandshake()
        stall.start()
        self.addCleanup(stall.stop)

        code, payload, _ = run_awc("act", f"{HOST}::{stall.port}", "--first-frame-timeout", "1", "key", "a", timeout=15)

        self.assertEqual(code, 40)
        self.assertNotIn("receipt", payload)


class TestAct(FleetCase):
    def reset_scene(self) -> None:
        code, payload, stderr = run_awc(
            "act", address(TIGERVNC), "--verify", "image", "--expect-image", str(SCENE_DIR / "0.png"),
            "--verify-timeout", str(VERIFY_TIMEOUT), "key", "0", timeout=VERIFY_TIMEOUT + 10,
        )
        self.assertIn(payload["receipt"]["status"], ("verified", "unknown"), stderr)

    def test_key_verified_against_the_scene_it_selects(self) -> None:
        self.reset_scene()

        code, payload, stderr = run_awc(
            "act", address(TIGERVNC), "--verify", "image", "--expect-image", str(SCENE_DIR / "s.png"),
            "--verify-timeout", str(VERIFY_TIMEOUT), "key", "s", timeout=VERIFY_TIMEOUT + 10,
        )

        self.assertEqual(code, 0, stderr)
        receipt = payload["receipt"]
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["verified_by"], "matches_image")
        self.assertGreater(receipt["frame"]["sequence"], receipt["sequence_before"])
        self.assertEqual(receipt["frame"]["generation"], payload["session"]["generation"])

    def test_wrong_reference_stays_unknown_even_though_pixels_changed(self) -> None:
        self.reset_scene()

        code, payload, _ = run_awc(
            "act", address(TIGERVNC), "--verify", "image", "--expect-image", str(SCENE_DIR / "d.png"),
            "--verify-timeout", "3", "key", "s", timeout=15,
        )

        self.assertEqual(code, 60)
        self.assertEqual(payload["receipt"]["status"], "unknown")
        self.assertTrue(payload["receipt"]["sent"])

    def test_a_key_that_changes_nothing_is_unknown_not_verified(self) -> None:
        self.reset_scene()
        run_awc("act", address(TIGERVNC), "--verify", "changed", "--verify-timeout", str(VERIFY_TIMEOUT), "key", "s",
                timeout=VERIFY_TIMEOUT + 10)

        code, payload, _ = run_awc(
            "act", address(TIGERVNC), "--verify", "changed", "--verify-timeout", "3", "key", "s", timeout=15,
        )

        self.assertEqual(code, 60)
        self.assertEqual(payload["receipt"]["status"], "unknown")
        self.assertIn("not satisfied", payload["receipt"]["reason"])

    def test_expect_size_mismatch_sends_nothing(self) -> None:
        self.reset_scene()

        code, payload, _ = run_awc("act", address(TIGERVNC), "--expect-size", "1024x768", "key", "s")

        self.assertEqual(code, 61)
        self.assertEqual(payload["receipt"]["status"], "failed")
        self.assertFalse(payload["receipt"]["sent"])
        _, after, _ = run_awc(
            "act", address(TIGERVNC), "--verify", "image", "--expect-image", str(SCENE_DIR / "0.png"),
            "--verify-timeout", "3", "move", "1", "1", timeout=15,
        )
        self.assertIn(after["receipt"]["status"], ("verified", "unknown"))

    def test_click_selects_the_scene_under_it(self) -> None:
        from tests.goldens import click_targets

        self.reset_scene()
        x, y = click_targets.click_target("s")

        code, payload, stderr = run_awc(
            "act", address(TIGERVNC), "--expect-size", "%dx%d" % TIGERVNC.size,
            "--verify", "image", "--expect-image", str(SCENE_DIR / "s.png"),
            "--verify-timeout", str(VERIFY_TIMEOUT), "click", str(x), str(y), timeout=VERIFY_TIMEOUT + 10,
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["receipt"]["status"], "verified")


class TestResize(FleetCase):
    server = LIBVNCSERVER_EXAMPLE

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(normalize_size, LIBVNCSERVER_EXAMPLE)

    def test_resize_after_sending_is_unknown_and_the_next_session_sees_the_new_size(self) -> None:
        normalize_size(LIBVNCSERVER_EXAMPLE)
        _, before, _ = run_awc("probe", address(LIBVNCSERVER_EXAMPLE))
        size_before = (before["session"]["frame"]["width"], before["session"]["frame"]["height"])

        code, payload, _ = run_awc(
            "act", address(LIBVNCSERVER_EXAMPLE), "--verify", "changed", "--verify-timeout", "5", "key", "down", timeout=20,
        )

        self.assertEqual(code, 60, payload)
        self.assertEqual(payload["receipt"]["status"], "unknown")
        self.assertIn("generation", payload["receipt"]["reason"])

        _, after, _ = run_awc("probe", address(LIBVNCSERVER_EXAMPLE))
        size_after = (after["session"]["frame"]["width"], after["session"]["frame"]["height"])
        self.assertNotEqual(size_after, size_before)

        code, refused, _ = run_awc("act", address(LIBVNCSERVER_EXAMPLE), "--expect-size", "%dx%d" % size_before, "key", "up")
        self.assertEqual(code, 61)
        self.assertFalse(refused["receipt"]["sent"])


class TestActionContention(FleetCase):
    """Two processes, one target: the second act is refused while the first still holds it."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_second_owner_is_refused_while_an_action_is_in_flight(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "VNC_PASSWORD"}
        # A verify on a static scene keeps the first process holding the target
        # for its whole verify bound: long enough for the second to collide.
        first = subprocess.Popen(
            [AWC_REMOTE, "act", address(TIGERVNC), "--lease-dir", self.tmp.name, "--lease-owner", "first",
             "--verify", "changed", "--verify-timeout", "6", "move", "2", "2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL,
        )
        self.addCleanup(first.kill)
        time.sleep(1.5)

        code, second, _ = run_awc(
            "act", address(TIGERVNC), "--lease-dir", self.tmp.name, "--lease-owner", "second", "key", "s",
        )
        self.assertEqual(code, 50, second)
        self.assertEqual(second["lease"]["owner"], "first")
        self.assertNotIn("receipt", second)

        out, err = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 60, err)
        self.assertEqual(json.loads(out)["receipt"]["status"], "unknown")

        code, third, stderr = run_awc(
            "act", address(TIGERVNC), "--lease-dir", self.tmp.name, "--lease-owner", "second", "move", "3", "3",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(third["receipt"]["status"], "sent")


class TestLeaseGate(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lease_dir = self.tmp.name

    def lease(self, *args: str) -> tuple[int, dict[str, Any]]:
        code, payload, _ = run_awc("lease", "--lease-dir", self.lease_dir, *args)
        return code, payload

    def test_only_the_holder_may_act_and_contenders_are_refused(self) -> None:
        code, acquired = self.lease("acquire", "--target", address(TIGERVNC), "--owner", "worker-a", "--ttl", "60")
        self.assertEqual(code, 0)
        token = acquired["lease"]["token"]
        self.assertEqual(acquired["lease"]["target"], f"{HOST}:{TIGERVNC.port}")

        code, refused = self.lease("acquire", "--target", f"{HOST}:{TIGERVNC.port - 5900}", "--owner", "worker-b")
        self.assertEqual(code, 50)
        self.assertEqual(refused["lease"]["owner"], "worker-a")

        code, payload, _ = run_awc(
            "act", address(TIGERVNC), "--lease-dir", self.lease_dir, "--lease-token", "not-the-token", "move", "1", "1",
        )
        self.assertEqual(code, 51)
        self.assertNotIn("receipt", payload)

        code, payload, stderr = run_awc(
            "act", address(TIGERVNC), "--lease-dir", self.lease_dir, "--lease-token", token, "move", "1", "1",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["receipt"]["status"], "sent")

        code, _ = self.lease("release", "--target", address(TIGERVNC), "--token", token)
        self.assertEqual(code, 0)
        code, status = self.lease("status", "--target", address(TIGERVNC))
        self.assertFalse(status["held"])
