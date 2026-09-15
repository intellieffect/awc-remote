import socket
import unittest

from twisted.internet.task import Clock
from twisted.python.failure import Failure

from vncdotool.const import AuthTypes
from vncdotool.reliability.client import ReliableFactory
from vncdotool.reliability.session import Outcome, Session, SessionError, Stage, StageTimeout, StageTimeouts

from tests.unit.reliability_fakes import FakeServer, outcome_of, settle

TIMEOUTS = StageTimeouts(connect=2.0, authenticate=3.0, first_frame=5.0)


class TestSessionStages(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def session(self, server: FakeServer, accept: bool = True) -> Session:
        connect = server.connect_and_accept if accept else server.connect
        return Session(
            ReliableFactory(), "host", 5901, socket.AF_INET,
            timeouts=TIMEOUTS, clock=self.clock, connect=connect,
        )

    def stages(self, session: Session) -> dict[Stage, Outcome | None]:
        return {s.stage: s.outcome for s in session.report.stages}

    def test_ready_after_first_frame_within_bounds(self) -> None:
        server = FakeServer(frames=[(5, 5, 5)])
        session = self.session(server)

        report = settle(session.start())

        self.assertTrue(report.ready)
        self.assertEqual(self.stages(session), {
            Stage.CONNECT: Outcome.OK, Stage.AUTHENTICATE: Outcome.OK, Stage.FIRST_FRAME: Outcome.OK,
        })
        self.assertEqual(report.frame.sequence, 1)
        self.assertEqual(report.frame.size, (64, 48))
        self.assertFalse(report.frame.black)
        self.assertEqual(server.transport.requests, [False])
        self.assertEqual(report.to_json()["failed_stage"], None)

    def test_first_frame_is_asked_for_non_incrementally(self) -> None:
        server = FakeServer(frames=[(5, 5, 5)])
        settle(self.session(server).start())

        self.assertEqual(server.transport.requests, [False])

    def test_connect_timeout_cancels_the_attempt(self) -> None:
        server = FakeServer()
        session = self.session(server, accept=False)
        d = session.start()

        self.clock.advance(TIMEOUTS.connect)

        failure = outcome_of(d)
        self.assertIsInstance(failure, Failure)
        self.assertTrue(failure.check(StageTimeout))
        self.assertIs(failure.value.stage, Stage.CONNECT)
        self.assertIs(failure.value.outcome, Outcome.TIMEOUT)
        self.assertTrue(server.attempt.called)
        self.assertFalse(session.report.ready)

    def test_a_late_accept_after_connect_timeout_is_ignored(self) -> None:
        server = FakeServer(frames=[(1, 1, 1)])
        session = self.session(server, accept=False)
        d = session.start()
        self.clock.advance(TIMEOUTS.connect)
        outcome_of(d)

        server.accept()

        self.assertEqual(len(session.report.stages), 1)

    def test_authentication_failure_is_named(self) -> None:
        server = FakeServer(auth=AuthTypes.VNC_AUTHENTICATION)
        session = self.session(server)

        failure = outcome_of(session.start())

        self.assertTrue(failure.check(SessionError))
        self.assertIs(failure.value.stage, Stage.AUTHENTICATE)
        self.assertIs(failure.value.outcome, Outcome.FAILED)
        self.assertIn("authentication failed", str(failure.value))
        self.assertTrue(server.dropped)

    def test_authenticate_timeout_when_the_server_stalls_after_tcp(self) -> None:
        server = FakeServer(handshake=False)
        session = self.session(server)
        d = session.start()
        self.assertEqual(self.stages(session)[Stage.CONNECT], Outcome.OK)

        self.clock.advance(TIMEOUTS.authenticate)

        failure = outcome_of(d)
        self.assertTrue(failure.check(StageTimeout))
        self.assertIs(failure.value.stage, Stage.AUTHENTICATE)
        self.assertTrue(server.dropped)

    def test_first_frame_timeout_when_no_update_arrives(self) -> None:
        server = FakeServer(frames=[])
        session = self.session(server)
        d = session.start()
        self.assertEqual(self.stages(session)[Stage.AUTHENTICATE], Outcome.OK)

        self.clock.advance(TIMEOUTS.first_frame - 0.1)
        self.assertFalse(d.called)
        self.clock.advance(0.1)

        failure = outcome_of(d)
        self.assertTrue(failure.check(StageTimeout))
        self.assertIs(failure.value.stage, Stage.FIRST_FRAME)
        self.assertEqual(failure.value.report.to_json()["failed_stage"], "first_frame")
        self.assertIsNone(failure.value.report.frame)

    def test_delayed_first_frame_inside_the_bound_is_ready(self) -> None:
        server = FakeServer()
        session = self.session(server)
        d = session.start()

        self.clock.advance(TIMEOUTS.first_frame - 1)
        server.send_frame((7, 7, 7))

        report = settle(d)
        self.assertTrue(report.ready)
        self.assertAlmostEqual(session.report.stages[-1].elapsed, TIMEOUTS.first_frame - 1)

    def test_empty_updates_do_not_count_as_a_first_frame(self) -> None:
        server = FakeServer()
        session = self.session(server)
        d = session.start()

        server.send_empty_update()
        self.assertFalse(d.called)
        server.send_frame((7, 7, 7))

        self.assertTrue(settle(d).ready)

    def test_black_first_frame_is_ready_and_marked_suspect(self) -> None:
        server = FakeServer(frames=[(0, 0, 0)])
        session = self.session(server)

        report = settle(session.start())

        self.assertTrue(report.ready)
        self.assertTrue(report.frame.black)
        self.assertTrue(report.to_json()["suspect_black"])

    def test_partial_first_frame_waits_for_the_rest(self) -> None:
        server = FakeServer(width=4, height=2)
        session = self.session(server)
        d = session.start()

        server.send_frame((1, 1, 1), 0, 0, 4, 1)
        self.assertFalse(d.called)
        server.send_frame((1, 1, 1), 0, 1, 4, 1)

        self.assertTrue(settle(d).ready)

    def test_disconnect_during_first_frame(self) -> None:
        server = FakeServer()
        session = self.session(server)
        d = session.start()

        server.drop()

        failure = outcome_of(d)
        self.assertTrue(failure.check(SessionError))
        self.assertIs(failure.value.stage, Stage.FIRST_FRAME)
        self.assertIs(failure.value.outcome, Outcome.DISCONNECTED)

    def test_resize_before_first_frame_reports_the_new_generation(self) -> None:
        server = FakeServer(width=4, height=2)
        session = self.session(server)
        d = session.start()

        server.send_resize(8, 2)
        server.send_frame((1, 1, 1))

        report = settle(d)
        self.assertEqual(report.generation, 2)
        self.assertEqual(report.frame.size, (8, 2))

    def test_close_waits_for_the_link_to_go(self) -> None:
        server = FakeServer(frames=[(1, 1, 1)])
        session = self.session(server)
        settle(session.start())

        settle(session.close())

        self.assertTrue(server.dropped)
        self.assertFalse(session.protocol.link_up)

    def test_timer_is_cancelled_once_ready(self) -> None:
        server = FakeServer(frames=[(1, 1, 1)])
        session = self.session(server)
        settle(session.start())

        self.assertEqual(self.clock.getDelayedCalls(), [])

    def test_report_never_contains_the_password(self) -> None:
        server = FakeServer(frames=[(1, 1, 1)])
        session = self.session(server)
        session.factory.password = "hunter2"
        report = settle(session.start())

        self.assertNotIn("hunter2", str(report.to_json()))
        self.assertEqual(session.factory.describe()["password_set"], True)
        self.assertNotIn("hunter2", str(session.factory.describe()))


class TestTargetName(unittest.TestCase):
    def test_display_and_port_spellings_name_one_target(self) -> None:
        from vncdotool.command import parse_server
        from vncdotool.reliability.session import target_name

        self.assertEqual(target_name(*parse_server("host:1")), target_name(*parse_server("host::5901")))
        self.assertEqual(target_name(*parse_server("ws://h/x?y=1")), parse_server("ws://h/x?y=1")[1])
        self.assertEqual(target_name(socket.AF_UNIX, "/tmp/s", 0), "unix:/tmp/s")


class TestStageTimeoutsValidation(unittest.TestCase):
    def test_every_bound_must_be_finite_and_positive(self) -> None:
        for bad in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                StageTimeouts(first_frame=bad)
        self.assertEqual(StageTimeouts(1, 2, 3).total, 6)
