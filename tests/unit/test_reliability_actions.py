import socket
import unittest

from PIL import Image
from twisted.internet.task import Clock

from vncdotool.keys import KEYMAP
from vncdotool.reliability import actions
from vncdotool.reliability.actions import ReceiptStatus, perform
from vncdotool.reliability.client import ReliableFactory
from vncdotool.reliability.session import Session, StageTimeouts

from reliability_fakes import FakeServer, settle

GREY = (40, 40, 40)
WHITE = (250, 250, 250)


class TestPerform(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.server = FakeServer(width=8, height=4, frames=[GREY])
        session = Session(
            ReliableFactory(), "host", 5900, socket.AF_INET,
            timeouts=StageTimeouts(), clock=self.clock, connect=self.server.connect_and_accept,
        )
        self.report = settle(session.start())
        self.client = self.server.protocol
        self.transport = self.server.transport

    def perform(self, action, **kwargs):
        kwargs.setdefault("timeout", 3.0)
        return perform(self.client, action, clock=self.clock, **kwargs)

    def key_events(self):
        return [key for key, _ in self.transport.keys]

    # -- nothing sent ------------------------------------------------------

    def test_failed_when_not_connected_and_nothing_is_sent(self) -> None:
        self.server.drop()

        receipt = settle(self.perform(actions.key_press("a")))

        self.assertIs(receipt.status, ReceiptStatus.FAILED)
        self.assertFalse(receipt.sent)
        self.assertEqual(self.key_events(), [])

    def test_failed_when_planned_against_an_older_generation(self) -> None:
        self.server.send_resize(16, 4)

        receipt = settle(self.perform(actions.key_press("a"), generation=self.report.generation))

        self.assertIs(receipt.status, ReceiptStatus.FAILED)
        self.assertIn("generation", receipt.reason)
        self.assertEqual(self.key_events(), [])

    def test_failed_when_the_action_raises_before_writing(self) -> None:
        receipt = settle(self.perform(actions.click(100, 100)))

        self.assertIs(receipt.status, ReceiptStatus.FAILED)
        self.assertEqual(self.transport.pointers, [])

    # -- sent --------------------------------------------------------------

    def test_sent_without_a_predicate(self) -> None:
        receipt = settle(self.perform(actions.key_press("a")))

        self.assertIs(receipt.status, ReceiptStatus.SENT)
        self.assertEqual(self.key_events(), [ord("a"), ord("a")])
        self.assertEqual(receipt.sequence_before, 1)
        self.assertIsNone(receipt.frame)
        self.assertEqual(self.transport.requests, [False])

    def test_click_moves_then_presses_at_the_point(self) -> None:
        receipt = settle(self.perform(actions.click(3, 2)))

        self.assertIs(receipt.status, ReceiptStatus.SENT)
        self.assertEqual(self.transport.pointers, [(3, 2, 0), (3, 2, 1), (3, 2, 0)])

    def test_type_sends_each_character(self) -> None:
        settle(self.perform(actions.type_text("ab")))
        self.assertEqual(self.key_events(), [ord("a"), ord("a"), ord("b"), ord("b")])

    def test_key_names_resolve_through_the_keymap(self) -> None:
        settle(self.perform(actions.key_press("enter")))
        self.assertEqual(self.key_events(), [KEYMAP["enter"], KEYMAP["enter"]])

    # -- verified ----------------------------------------------------------

    def test_verified_by_a_post_action_frame_that_satisfies_the_predicate(self) -> None:
        self.server.frames = [WHITE]

        receipt = settle(self.perform(actions.key_press("a"), verify=actions.pixels_changed))

        self.assertIs(receipt.status, ReceiptStatus.VERIFIED)
        self.assertEqual(receipt.verified_by, "pixels_changed")
        self.assertEqual(receipt.frame.sequence, 2)
        self.assertGreater(receipt.frame.sequence, receipt.sequence_before)
        self.assertEqual(self.transport.requests, [False, True])

    def test_verification_keeps_a_request_outstanding_until_satisfied(self) -> None:
        self.server.frames = [GREY, GREY, WHITE]

        receipt = settle(self.perform(actions.key_press("a"), verify=actions.pixels_changed))

        self.assertIs(receipt.status, ReceiptStatus.VERIFIED)
        self.assertEqual(receipt.frames_seen, 3)
        self.assertEqual(self.transport.requests, [False, True, True, True])

    def test_delayed_frame_inside_the_bound_verifies(self) -> None:
        d = self.perform(actions.key_press("a"), verify=actions.pixels_changed)
        self.clock.advance(2.0)
        self.assertFalse(d.called)

        self.server.send_frame(WHITE)

        self.assertIs(settle(d).status, ReceiptStatus.VERIFIED)

    def test_caller_predicate_decides_meaning_not_pixel_change(self) -> None:
        # The screen changes, but not to what the caller asked for.
        reference = Image.new("RGB", (8, 4), (1, 2, 3))
        self.server.frames = [WHITE]
        d = self.perform(actions.key_press("a"), verify=actions.matches_image(reference))
        self.clock.advance(3.0)

        receipt = settle(d)
        self.assertIs(receipt.status, ReceiptStatus.UNKNOWN)
        self.assertEqual(receipt.frames_seen, 1)

    def test_matches_image_verifies_the_region_it_is_given(self) -> None:
        reference = Image.new("RGB", (2, 2), WHITE)
        self.server.frames = [WHITE]

        receipt = settle(self.perform(actions.key_press("a"), verify=actions.matches_image(reference, 3, 1, fuzz=0)))

        self.assertIs(receipt.status, ReceiptStatus.VERIFIED)
        self.assertEqual(receipt.verified_by, "matches_image")

    def test_region_changed_compares_against_the_moment_of_sending(self) -> None:
        predicate = actions.region_changed(self.client, 0, 0, 4, 4)
        self.server.frames = [WHITE]

        receipt = settle(self.perform(actions.key_press("a"), verify=predicate))

        self.assertIs(receipt.status, ReceiptStatus.VERIFIED)

    # -- unknown -----------------------------------------------------------

    def test_unknown_when_no_frame_arrives_in_time(self) -> None:
        d = self.perform(actions.key_press("a"), verify=actions.pixels_changed)
        self.clock.advance(3.0)

        receipt = settle(d)
        self.assertIs(receipt.status, ReceiptStatus.UNKNOWN)
        self.assertTrue(receipt.sent)
        self.assertIsNone(receipt.frame)
        self.assertIn("0 frame(s)", receipt.reason)

    def test_static_screen_is_a_fresh_frame_that_does_not_verify(self) -> None:
        self.server.frames = [GREY]
        d = self.perform(actions.key_press("a"), verify=actions.pixels_changed)
        self.clock.advance(3.0)

        receipt = settle(d)
        self.assertIs(receipt.status, ReceiptStatus.UNKNOWN)
        self.assertIsNotNone(receipt.frame)
        self.assertTrue(receipt.frame.identical_to_previous)
        self.assertEqual(receipt.frames_seen, 1)

    def test_unknown_when_the_connection_drops_after_sending(self) -> None:
        d = self.perform(actions.key_press("a"), verify=actions.pixels_changed)

        self.server.drop()

        receipt = settle(d)
        self.assertIs(receipt.status, ReceiptStatus.UNKNOWN)
        self.assertIn("connection lost after sending", receipt.reason)
        self.assertEqual(self.key_events(), [ord("a"), ord("a")])

    def test_unknown_when_the_predicate_raises(self) -> None:
        def broken(client, frame):
            raise RuntimeError("boom")
        self.server.frames = [WHITE]

        receipt = settle(self.perform(actions.key_press("a"), verify=broken))

        self.assertIs(receipt.status, ReceiptStatus.UNKNOWN)
        self.assertIn("boom", receipt.reason)

    def test_no_retry_after_an_unknown_outcome(self) -> None:
        d = self.perform(actions.key_press("a"), verify=actions.pixels_changed)
        self.clock.advance(10.0)
        settle(d)

        self.assertEqual(self.client.events_sent, 2)
        self.assertEqual(self.clock.getDelayedCalls(), [])

    def test_black_post_action_frame_is_not_verified_by_not_black(self) -> None:
        self.server.frames = [(0, 0, 0)]
        d = self.perform(actions.key_press("a"), verify=actions.not_black)
        self.clock.advance(3.0)

        receipt = settle(d)
        self.assertIs(receipt.status, ReceiptStatus.UNKNOWN)
        self.assertTrue(receipt.frame.black)

    def test_receipt_json_names_status_and_frame(self) -> None:
        self.server.frames = [WHITE]
        receipt = settle(self.perform(actions.key_press("a"), verify=actions.pixels_changed))

        data = receipt.to_json()
        self.assertEqual(data["status"], "verified")
        self.assertEqual(data["action"], "key a")
        self.assertEqual(data["frame"]["sequence"], 2)
