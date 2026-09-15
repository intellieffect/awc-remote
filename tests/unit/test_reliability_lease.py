import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vncdotool.reliability.lease import LeaseHeld, LeaseStore, NotLeaseOwner, slug

# The canonical spelling: what `awc-remote` derives from "studio.example::5900".
TARGET = "studio.example:5900"
TARGET_SPELLING = "studio.example::5900"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestLeaseStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.store = LeaseStore(self.tmp.name, clock=self.clock)

    def test_acquire_records_owner_and_expiry(self) -> None:
        lease = self.store.acquire(TARGET, "worker-a", ttl=60)

        self.assertEqual(lease.owner, "worker-a")
        self.assertEqual(lease.expires_at, 1060.0)
        self.assertEqual(lease.remaining(self.clock.now), 60.0)
        self.assertEqual(self.store.status(TARGET).token, lease.token)

    def test_second_owner_is_refused_while_held(self) -> None:
        self.store.acquire(TARGET, "worker-a", ttl=60)

        with self.assertRaises(LeaseHeld) as caught:
            self.store.acquire(TARGET, "worker-b", ttl=60)
        self.assertEqual(caught.exception.holder.owner, "worker-a")

    def test_same_owner_does_not_get_a_second_lease(self) -> None:
        self.store.acquire(TARGET, "worker-a", ttl=60)

        with self.assertRaises(LeaseHeld):
            self.store.acquire(TARGET, "worker-a", ttl=60)

    def test_targets_are_independent(self) -> None:
        self.store.acquire(TARGET, "worker-a", ttl=60)
        other = self.store.acquire("mini.example:5900", "worker-b", ttl=60)

        self.assertEqual(other.owner, "worker-b")

    def test_expired_lease_can_be_taken_over(self) -> None:
        self.store.acquire(TARGET, "worker-a", ttl=60)
        self.clock.now += 60

        self.assertIsNone(self.store.status(TARGET))
        lease = self.store.acquire(TARGET, "worker-b", ttl=60)
        self.assertEqual(lease.owner, "worker-b")

    def test_release_needs_the_token(self) -> None:
        lease = self.store.acquire(TARGET, "worker-a", ttl=60)

        with self.assertRaises(NotLeaseOwner):
            self.store.release(TARGET, "not-the-token")
        self.assertIsNotNone(self.store.status(TARGET))

        self.store.release(TARGET, lease.token)
        self.assertIsNone(self.store.status(TARGET))

    def test_release_of_nothing_is_not_an_error(self) -> None:
        self.store.release(TARGET, "whatever")

    def test_renew_extends_only_for_the_holder(self) -> None:
        lease = self.store.acquire(TARGET, "worker-a", ttl=60)
        self.clock.now += 30

        renewed = self.store.renew(TARGET, lease.token, ttl=60)
        self.assertEqual(renewed.expires_at, 1090.0)
        with self.assertRaises(NotLeaseOwner):
            self.store.renew(TARGET, "not-the-token", ttl=60)

    def test_renew_after_expiry_is_refused(self) -> None:
        lease = self.store.acquire(TARGET, "worker-a", ttl=60)
        self.clock.now += 61

        with self.assertRaises(NotLeaseOwner):
            self.store.renew(TARGET, lease.token, ttl=60)

    def test_ttl_must_be_finite_and_positive(self) -> None:
        for bad in (0, -5, float("nan"), float("inf"), "soon"):
            with self.subTest(ttl=bad), self.assertRaises(ValueError):
                self.store.acquire(TARGET, "worker-a", ttl=bad)
        self.assertIsNone(self.store.status(TARGET))

    def test_hold_ensure_extends_and_release_gives_back(self) -> None:
        from vncdotool.reliability.lease import hold, take
        taken = take(self.store, TARGET, "worker-a", ttl=10)
        self.assertTrue(taken.owned)

        adopted = hold(self.store, TARGET, taken.lease.token)
        self.assertFalse(adopted.owned)
        adopted.ensure(100)
        self.assertEqual(self.store.status(TARGET).expires_at, 1100.0)

        taken.release()
        self.assertIsNone(self.store.status(TARGET))
        with self.assertRaises(NotLeaseOwner):
            adopted.ensure(10)

    def test_hold_with_a_wrong_token_is_refused(self) -> None:
        from vncdotool.reliability.lease import hold
        self.store.acquire(TARGET, "worker-a", ttl=10)
        with self.assertRaises(NotLeaseOwner):
            hold(self.store, TARGET, "nope")

    def test_status_hides_the_token(self) -> None:
        self.store.acquire(TARGET, "worker-a", ttl=60)
        self.assertIsNone(self.store.status(TARGET).to_json()["token"])

    def test_corrupt_record_is_treated_as_free(self) -> None:
        self.store.acquire(TARGET, "worker-a", ttl=60)
        (Path(self.tmp.name) / (slug(TARGET) + ".json")).write_text("{not json")

        self.assertIsNone(self.store.status(TARGET))
        self.assertEqual(self.store.acquire(TARGET, "worker-b", ttl=60).owner, "worker-b")

    def test_without_fcntl_every_operation_says_so(self) -> None:
        from unittest import mock

        from vncdotool.reliability import lease as lease_module

        with mock.patch.object(lease_module, "fcntl", None):
            with self.assertRaises(lease_module.LeasesUnavailable):
                self.store.acquire(TARGET, "worker-a", ttl=60)
            with self.assertRaises(lease_module.LeasesUnavailable):
                self.store.status(TARGET)

    def test_slug_is_stable_and_distinct(self) -> None:
        self.assertEqual(slug(TARGET), slug(TARGET))
        self.assertNotEqual(slug(TARGET), slug("studio.example:5901"))
        self.assertNotIn(":", slug(TARGET))

    def test_dotted_targets_do_not_share_a_record(self) -> None:
        self.store.acquire("studio.example:5900", "worker-a", ttl=60)
        self.store.acquire("studio.example:5901", "worker-b", ttl=60)

        self.assertEqual(self.store.status("studio.example:5900").owner, "worker-a")
        self.assertEqual(self.store.status("studio.example:5901").owner, "worker-b")


def cli_lease(lease_dir: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vncdotool.reliability.cli", "lease", "--lease-dir", lease_dir, *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


class TestLeaseAcrossProcesses(unittest.TestCase):
    """The cooperative guarantee: two processes, one lease directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LeaseStore(self.tmp.name)

    def test_another_process_is_refused_while_this_one_holds(self) -> None:
        lease = self.store.acquire(TARGET, "in-process", ttl=60)

        run = cli_lease(self.tmp.name, "acquire", "--target", TARGET_SPELLING, "--owner", "other-process")

        self.assertEqual(run.returncode, 50, run.stderr)
        payload = json.loads(run.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["lease"]["owner"], "in-process")
        self.assertIsNone(payload["lease"]["token"])
        self.store.release(TARGET, lease.token)

    def test_another_process_cannot_release_this_ones_lease(self) -> None:
        lease = self.store.acquire(TARGET, "in-process", ttl=60)

        run = cli_lease(self.tmp.name, "release", "--target", TARGET_SPELLING, "--token", "guess")

        self.assertEqual(run.returncode, 51, run.stderr)
        self.assertEqual(self.store.status(TARGET).token, lease.token)

    def test_another_process_takes_over_once_expired(self) -> None:
        self.store.acquire(TARGET, "in-process", ttl=0.05)
        import time
        time.sleep(0.1)

        run = cli_lease(self.tmp.name, "acquire", "--target", TARGET_SPELLING, "--owner", "other-process", "--ttl", "5")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["lease"]["owner"], "other-process")

    def test_simultaneous_contenders_yield_exactly_one_holder(self) -> None:
        env = dict(os.environ)
        contenders = [
            subprocess.Popen(
                [sys.executable, "-m", "vncdotool.reliability.cli", "lease", "--lease-dir", self.tmp.name,
                 "acquire", "--target", TARGET_SPELLING, "--owner", f"contender-{i}", "--ttl", "30"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            for i in range(3)
        ]
        results = [p.communicate(timeout=60) for p in contenders]
        codes = [p.returncode for p in contenders]

        self.assertEqual(sorted(codes), [0, 50, 50], results)
        winner = json.loads(results[codes.index(0)][0])["lease"]
        self.assertEqual(self.store.status(TARGET).owner, winner["owner"])
