import io
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

from PIL import Image
from twisted.internet.task import Clock

from vncdotool.const import AuthTypes
from vncdotool.reliability import cli

from tests.unit.reliability_fakes import FakeServer

GREY = (40, 40, 40)
WHITE = (250, 250, 250)


class CLIHarness(unittest.TestCase):
    """Runs ``awc-remote`` against a scripted server, driving time by hand."""

    def setUp(self) -> None:
        self.clock = Clock()
        self.server: FakeServer | None = None
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(cli.DEFAULT_PASSWORD_ENV, None)

    def runner(self, start):
        d = start()
        outcome = []
        d.addBoth(outcome.append)
        for _ in range(200):
            if outcome:
                break
            self.clock.advance(0.5)
        self.assertTrue(outcome, "command never settled")
        if hasattr(outcome[0], "raiseException"):
            outcome[0].raiseException()
        return outcome[0]

    def run_cli(self, *argv: str, accept: bool = True):
        assert self.server is not None
        connect = self.server.connect_and_accept if accept else self.server.connect
        out = io.StringIO()
        code = cli.main(list(argv), connect=connect, clock=self.clock, runner=self.runner, stdout=out)
        return code, json.loads(out.getvalue())


class TestProbe(CLIHarness):
    def test_ready_reports_stages_and_frame(self) -> None:
        self.server = FakeServer(frames=[GREY])

        code, payload = self.run_cli("probe", "host::5901")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["session"]["ready"])
        self.assertEqual([s["outcome"] for s in payload["session"]["stages"]], ["ok", "ok", "ok"])
        self.assertEqual(payload["session"]["frame"]["width"], 64)
        self.assertFalse(payload["session"]["suspect_black"])
        self.assertEqual(payload["session"]["target"], "host:5901")
        self.assertTrue(self.server.dropped)

    def test_black_first_frame_is_ready_but_suspect(self) -> None:
        self.server = FakeServer(frames=[(0, 0, 0)])

        code, payload = self.run_cli("probe", "host")

        self.assertEqual(code, 0)
        self.assertTrue(payload["session"]["suspect_black"])

    def test_first_frame_timeout_exits_40(self) -> None:
        self.server = FakeServer(frames=[])

        code, payload = self.run_cli("probe", "host", "--first-frame-timeout", "2")

        self.assertEqual(code, 40)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["session"]["failed_stage"], "first_frame")

    def test_connect_timeout_exits_40(self) -> None:
        self.server = FakeServer()

        code, payload = self.run_cli("probe", "host", "--connect-timeout", "1", accept=False)

        self.assertEqual(code, 40)
        self.assertEqual(payload["session"]["failed_stage"], "connect")

    def test_authentication_failure_exits_3_and_leaks_nothing(self) -> None:
        self.server = FakeServer(auth=AuthTypes.VNC_AUTHENTICATION)

        code, payload = self.run_cli("probe", "host")

        self.assertEqual(code, 3)
        self.assertEqual(payload["session"]["failed_stage"], "authenticate")

    def test_password_comes_from_the_environment_not_the_output(self) -> None:
        os.environ[cli.DEFAULT_PASSWORD_ENV] = "s3cret"
        self.server = FakeServer(frames=[GREY])

        _, payload = self.run_cli("probe", "host")

        self.assertNotIn("s3cret", json.dumps(payload))
        self.assertEqual(self.server.factory.password, "s3cret")

    def test_password_file_is_read_without_its_newline(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with tempfile.NamedTemporaryFile("w", delete=False) as fp:
            fp.write("fromfile\n")
        self.addCleanup(os.unlink, fp.name)

        self.run_cli("probe", "host", "--password-file", fp.name)

        self.assertEqual(self.server.factory.password, "fromfile")

    def test_screenshot_is_opt_in(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frame.png")
            _, payload = self.run_cli("probe", "host", "--screenshot", path)

            self.assertEqual(payload["screenshot"], path)
            with Image.open(path) as saved:
                self.assertEqual(saved.size, (64, 48))
        self.server = FakeServer(frames=[GREY])
        _, payload = self.run_cli("probe", "host")
        self.assertIsNone(payload["screenshot"])


class TestAct(CLIHarness):
    def test_verified_key_exits_0(self) -> None:
        self.server = FakeServer(frames=[GREY, WHITE])

        code, payload = self.run_cli("act", "host", "--verify", "changed", "key", "a")

        self.assertEqual(code, 0)
        self.assertEqual(payload["receipt"]["status"], "verified")
        self.assertEqual(payload["receipt"]["action"], "key a")
        self.assertEqual([k for k, _ in self.server.transport.keys], [ord("a"), ord("a")])

    def test_sent_without_verification_exits_0(self) -> None:
        self.server = FakeServer(frames=[GREY])

        code, payload = self.run_cli("act", "host", "click", "3", "2")

        self.assertEqual(code, 0)
        self.assertEqual(payload["receipt"]["status"], "sent")
        self.assertEqual(self.server.transport.pointers[-1], (3, 2, 0))

    def test_unverified_exits_60_and_is_not_resent(self) -> None:
        self.server = FakeServer(frames=[GREY, GREY])

        code, payload = self.run_cli("act", "host", "--verify", "changed", "--verify-timeout", "1", "type", "hi")

        self.assertEqual(code, 60)
        self.assertEqual(payload["receipt"]["status"], "unknown")
        self.assertTrue(payload["receipt"]["sent"])
        self.assertEqual(len(self.server.transport.keys), 4)

    def test_expect_size_mismatch_exits_61_and_sends_nothing(self) -> None:
        self.server = FakeServer(width=64, height=48, frames=[GREY])

        code, payload = self.run_cli("act", "host", "--expect-size", "1920x1080", "click", "3", "2")

        self.assertEqual(code, 61)
        self.assertEqual(payload["receipt"]["status"], "failed")
        self.assertEqual(self.server.transport.pointers, [])

    def test_expect_size_match_lets_the_action_through(self) -> None:
        self.server = FakeServer(width=64, height=48, frames=[GREY])

        code, payload = self.run_cli("act", "host", "--expect-size", "64x48", "move", "3", "2")

        self.assertEqual(code, 0)
        self.assertEqual(payload["receipt"]["status"], "sent")

    def test_image_verification_against_a_reference(self) -> None:
        self.server = FakeServer(frames=[GREY, WHITE])
        with tempfile.TemporaryDirectory() as tmp:
            ref = os.path.join(tmp, "ref.png")
            Image.new("RGB", (4, 4), WHITE).save(ref)

            code, payload = self.run_cli(
                "act", "host", "--verify", "image", "--expect-image", ref, "--at", "2,2", "--fuzz", "0", "key", "a",
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["receipt"]["verified_by"], "matches_image")

    def test_image_verification_without_a_reference_is_a_usage_error(self) -> None:
        self.server = FakeServer(frames=[GREY])

        code, payload = self.run_cli("act", "host", "--verify", "image", "key", "a")

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])

    def test_session_failure_before_acting_sends_nothing(self) -> None:
        self.server = FakeServer(frames=[])

        code, payload = self.run_cli("act", "host", "--first-frame-timeout", "1", "key", "a")

        self.assertEqual(code, 40)
        self.assertNotIn("receipt", payload)
        self.assertEqual(self.server.transport.keys, [])


class TestLeaseCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def lease(self, *argv: str):
        out = io.StringIO()
        code = cli.main(["lease", "--lease-dir", self.tmp.name, *argv], stdout=out)
        return code, json.loads(out.getvalue())

    def test_acquire_status_renew_release(self) -> None:
        code, acquired = self.lease("acquire", "--target", "t:5900", "--owner", "me", "--ttl", "30")
        self.assertEqual(code, 0)
        token = acquired["lease"]["token"]
        self.assertTrue(token)

        code, status = self.lease("status", "--target", "t:5900")
        self.assertTrue(status["held"])
        self.assertIsNone(status["lease"]["token"])

        code, renewed = self.lease("renew", "--target", "t:5900", "--token", token, "--ttl", "60")
        self.assertEqual(code, 0)
        self.assertGreater(renewed["lease"]["expires_at"], acquired["lease"]["expires_at"])

        code, _ = self.lease("release", "--target", "t:5900", "--token", token)
        self.assertEqual(code, 0)
        _, status = self.lease("status", "--target", "t:5900")
        self.assertFalse(status["held"])

    def test_held_and_not_owner_exit_codes(self) -> None:
        self.lease("acquire", "--target", "t:5900", "--owner", "me")

        code, payload = self.lease("acquire", "--target", "t:5900", "--owner", "you")
        self.assertEqual(code, 50)
        self.assertEqual(payload["lease"]["owner"], "me")

        code, _ = self.lease("release", "--target", "t:5900", "--token", "nope")
        self.assertEqual(code, 51)

    def test_lease_dir_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"AWC_REMOTE_LEASE_DIR": self.tmp.name}):
            out = io.StringIO()
            cli.main(["lease", "acquire", "--target", "t", "--owner", "me"], stdout=out)
        self.assertTrue(any(name.endswith(".json") for name in os.listdir(self.tmp.name)))


class TestParser(unittest.TestCase):
    def test_no_password_option_exists(self) -> None:
        parser = cli.build_parser()
        for action in parser._subparsers._group_actions[0].choices["probe"]._actions:
            self.assertNotIn("--password", action.option_strings)
            self.assertNotIn("-p", action.option_strings)

    def test_parse_server_forms(self) -> None:
        self.assertEqual(cli.parse_server("host:1")[1:], ("host", 5901))
        self.assertEqual(cli.parse_server("host::5999")[2], 5999)
        self.assertEqual(cli.parse_server("host")[0], socket.AF_UNSPEC)


class TestLeaseGate(CLIHarness):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from vncdotool.reliability.lease import LeaseStore
        self.store = LeaseStore(self.tmp.name)

    def test_holder_may_act(self) -> None:
        lease = self.store.acquire("host:5901", "me", ttl=60)
        self.server = FakeServer(frames=[GREY])

        code, payload = self.run_cli(
            "act", "host:1", "--lease-dir", self.tmp.name, "--lease-token", lease.token, "key", "a",
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["receipt"]["status"], "sent")

    def test_wrong_token_is_refused_before_connecting(self) -> None:
        self.store.acquire("host:5901", "someone-else", ttl=60)
        self.server = FakeServer(frames=[GREY])

        code, payload = self.run_cli(
            "probe", "host::5901", "--lease-dir", self.tmp.name, "--lease-token", "nope",
        )

        self.assertEqual(code, 51)
        self.assertEqual(payload["target"], "host:5901")
        self.assertIsNone(self.server.protocol)

    def test_no_lease_at_all_is_refused_when_a_token_is_offered(self) -> None:
        self.server = FakeServer(frames=[GREY])

        code, _ = self.run_cli("probe", "host", "--lease-dir", self.tmp.name, "--lease-token", "x")

        self.assertEqual(code, 51)

    def test_lease_target_spellings_are_normalised(self) -> None:
        out = io.StringIO()
        cli.main(["lease", "--lease-dir", self.tmp.name, "acquire", "--target", "host:1", "--owner", "me"], stdout=out)
        token = json.loads(out.getvalue())["lease"]["token"]

        lease = ["lease", "--lease-dir", self.tmp.name]
        out = io.StringIO()
        code = cli.main([*lease, "acquire", "--target", "host::5901", "--owner", "you"], stdout=out)
        self.assertEqual(code, 50)

        out = io.StringIO()
        code = cli.main([*lease, "release", "--target", "host::5901", "--token", token], stdout=out)
        self.assertEqual(code, 0)


class TestLeaseOwnerForTheRun(CLIHarness):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from vncdotool.reliability.lease import LeaseStore
        self.store = LeaseStore(self.tmp.name)

    def test_takes_the_lease_for_the_run_and_releases_it(self) -> None:
        self.server = FakeServer(frames=[GREY, WHITE])

        code, payload = self.run_cli(
            "act", "host::5901", "--lease-dir", self.tmp.name, "--lease-owner", "me", "--verify", "changed", "key", "a",
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["receipt"]["status"], "verified")
        self.assertIsNone(self.store.status("host:5901"))

    def test_refused_while_another_owner_holds_and_nothing_is_dialled(self) -> None:
        self.store.acquire("host:5901", "other", ttl=60)
        self.server = FakeServer(frames=[GREY])

        code, payload = self.run_cli("act", "host:1", "--lease-dir", self.tmp.name, "--lease-owner", "me", "key", "a")

        self.assertEqual(code, 50)
        self.assertEqual(payload["lease"]["owner"], "other")
        self.assertIsNone(self.server.protocol)

    def test_token_holder_keeps_the_lease_after_the_run(self) -> None:
        lease = self.store.acquire("host:5901", "me", ttl=60)
        self.server = FakeServer(frames=[GREY])

        code, _ = self.run_cli("probe", "host:1", "--lease-dir", self.tmp.name, "--lease-token", lease.token)

        self.assertEqual(code, 0)
        self.assertEqual(self.store.status("host:5901").token, lease.token)

    def test_token_and_owner_are_exclusive(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with mock.patch("sys.stdout", io.StringIO()) as out, self.assertRaises(SystemExit) as caught:
            self.run_cli("probe", "host", "--lease-token", "t", "--lease-owner", "me")
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])


class TestScreenshotSafety(CLIHarness):
    def test_existing_path_is_refused_and_the_session_still_closes(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
            fp.write(b"keep me")
        self.addCleanup(os.unlink, fp.name)

        code, payload = self.run_cli("probe", "host", "--screenshot", fp.name)

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("FileExistsError", payload["error"])
        self.assertTrue(payload["session"]["ready"])
        self.assertTrue(self.server.dropped)
        with open(fp.name, "rb") as kept:
            self.assertEqual(kept.read(), b"keep me")

    def test_symlink_is_not_written_through(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target.png")
            link = os.path.join(tmp, "link.png")
            os.symlink(target, link)

            code, payload = self.run_cli("probe", "host", "--screenshot", link)

            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(target))

    def test_new_file_is_private(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frame.png")
            old = os.umask(0)
            try:
                code, _ = self.run_cli("probe", "host", "--screenshot", path)
            finally:
                os.umask(old)
            self.assertEqual(code, 0)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_unknown_extension_is_an_error_with_json(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_cli("probe", "host", "--screenshot", os.path.join(tmp, "frame.what"))
        self.assertEqual(code, 1)
        self.assertIn("image format", payload["error"])

    def test_screenshot_failure_after_an_action_still_reports_the_receipt(self) -> None:
        self.server = FakeServer(frames=[GREY])
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
            pass
        self.addCleanup(os.unlink, fp.name)

        code, payload = self.run_cli("act", "host", "--screenshot", fp.name, "key", "a")

        self.assertEqual(code, 1)
        self.assertEqual(payload["receipt"]["status"], "sent")


class TestUsageErrorsAreJSON(unittest.TestCase):
    def usage(self, *argv: str) -> dict:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit) as caught:
            cli.main(list(argv))
        self.assertEqual(caught.exception.code, 2)
        return json.loads(out.getvalue())

    def test_non_finite_and_non_positive_timeouts(self) -> None:
        for bad in ("nan", "inf", "0", "-3", "soon"):
            with self.subTest(value=bad):
                payload = self.usage("probe", "host", "--first-frame-timeout", bad)
                self.assertFalse(payload["ok"])
                self.assertRegex(payload["error"], "finite|number")

    def test_bad_ttl(self) -> None:
        payload = self.usage("lease", "acquire", "--target", "t", "--owner", "me", "--ttl", "inf")
        self.assertFalse(payload["ok"])

    def test_bad_expect_size(self) -> None:
        payload = self.usage("act", "host", "--expect-size", "garbage", "key", "a")
        self.assertIn("WIDTHxHEIGHT", payload["error"])


class TestWithoutFcntl(CLIHarness):
    """Windows has no flock: probe and act still run, and lease commands say why they cannot."""

    def test_probe_runs_and_lease_reports_unavailable(self) -> None:
        from vncdotool.reliability import lease as lease_module

        with mock.patch.object(lease_module, "fcntl", None):
            self.server = FakeServer(frames=[GREY])
            code, payload = self.run_cli("probe", "host")
            self.assertEqual(code, 0)

            out = io.StringIO()
            code = cli.main(["lease", "--lease-dir", tempfile.gettempdir(), "status", "--target", "t"], stdout=out)
            self.assertEqual(code, 1)
            self.assertIn("fcntl", json.loads(out.getvalue())["error"])
