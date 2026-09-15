import unittest

from PIL import Image
from twisted.internet.task import Clock

from vncdotool.reliability.client import ReliableFactory
from vncdotool.reliability.frames import FrameLog, is_black

from tests.unit.reliability_fakes import FakeServer


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestFrameLog(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.log = FrameLog(monotonic=self.clock, wall=lambda: 1.0)
        self.log.new_generation("connected")

    def test_records_sequence_size_and_receive_time(self) -> None:
        info = self.log.record(Image.new("RGB", (8, 6), (1, 2, 3)), rectangles=2)

        self.assertEqual(info.sequence, 1)
        self.assertEqual(info.generation, 1)
        self.assertEqual(info.size, (8, 6))
        self.assertEqual(info.rectangles, 2)
        self.assertEqual(info.received_at, 100.0)
        self.assertEqual(info.age(103.5), 3.5)

    def test_identical_pixels_are_fresh_not_stale(self) -> None:
        image = Image.new("RGB", (8, 6), (9, 9, 9))
        first = self.log.record(image, 1)
        self.clock.now += 2.0
        second = self.log.record(image.copy(), 1)

        self.assertTrue(second.identical_to_previous)
        self.assertFalse(first.identical_to_previous)
        self.assertEqual(second.sequence, 2)
        self.assertGreater(second.received_at, first.received_at)
        self.assertEqual(second.age(self.clock.now), 0.0)

    def test_black_is_flagged_not_treated_as_an_error(self) -> None:
        info = self.log.record(Image.new("RGB", (8, 6), "black"), 1)

        self.assertTrue(info.black)
        self.assertTrue(self.log.is_current(info))

    def test_black_needs_every_channel_at_zero(self) -> None:
        self.assertFalse(is_black(Image.new("RGB", (2, 2), (0, 0, 1))))
        self.assertTrue(is_black(Image.new("L", (2, 2), 0)))

    def test_resize_starts_a_generation_that_outdates_earlier_frames(self) -> None:
        before = self.log.record(Image.new("RGB", (8, 6)), 1)
        self.log.new_generation("resized")
        after = self.log.record(Image.new("RGB", (16, 6)), 1)

        self.assertFalse(self.log.is_current(before))
        self.assertTrue(self.log.is_current(after))
        self.assertEqual(after.generation, 2)
        self.assertEqual(after.sequence, 2)

    def test_json_carries_the_fields_a_caller_decides_on(self) -> None:
        info = self.log.record(Image.new("RGB", (8, 6)), 1)

        data = info.to_json()
        for key in ("sequence", "generation", "received_at", "width", "height", "black", "identical_to_previous"):
            self.assertIn(key, data)


class TestReliableClientFrames(unittest.TestCase):
    """The wire path: frames arrive as RFB messages through a mocked transport."""

    def setUp(self) -> None:
        self.server = FakeServer(width=4, height=2)
        self.factory = ReliableFactory()
        self.server.connect_and_accept(self.factory, "host", 5900, None)
        self.client = self.server.protocol

    def test_connect_starts_generation_one_and_no_frame(self) -> None:
        self.assertEqual(self.client.frames.generation, 1)
        self.assertIsNone(self.client.frames.latest)
        self.assertTrue(self.client.link_up)

    def test_each_painting_update_is_one_frame(self) -> None:
        seen = []
        self.client.onFrame(seen.append)
        self.client.refreshScreen(incremental=False)

        self.server.send_frame((10, 20, 30))
        self.server.send_frame((10, 20, 30))

        self.assertEqual([f.sequence for f in seen], [1, 2])
        self.assertFalse(seen[0].identical_to_previous)
        self.assertTrue(seen[1].identical_to_previous)
        self.assertFalse(seen[1].black)

    def test_an_update_that_paints_nothing_is_not_a_frame(self) -> None:
        self.client.refreshScreen(incremental=False)
        self.server.send_empty_update()

        self.assertIsNone(self.client.frames.latest)

    def test_black_frame_is_recorded_and_flagged(self) -> None:
        self.client.refreshScreen(incremental=False)
        self.server.send_frame((0, 0, 0))

        self.assertTrue(self.client.frames.latest.black)

    def test_desktop_resize_bumps_the_generation(self) -> None:
        self.client.refreshScreen(incremental=False)
        self.server.send_frame((1, 1, 1))
        first = self.client.frames.latest

        self.server.send_resize(8, 2)
        self.client.refreshScreen(incremental=False)
        self.server.send_frame((1, 1, 1))

        self.assertEqual(self.client.frames.generation, 2)
        self.assertFalse(self.client.frames.is_current(first))
        self.assertEqual(self.client.frames.latest.size, (8, 2))

    def test_input_events_are_counted_and_link_loss_is_reported_once(self) -> None:
        lost = []
        self.client.onLinkLost(lost.append)
        self.client.keyPress("a")
        self.client.mouseMove(1, 1)

        self.assertEqual(self.client.events_sent, 3)
        self.server.drop()
        self.server.drop()

        self.assertEqual(len(lost), 1)
        self.assertFalse(self.client.link_up)

    def test_clock_is_the_reactor_style_clock_the_tests_use(self) -> None:
        # Guards the fake: Clock.seconds() is what Session and perform() read.
        self.assertEqual(Clock().seconds(), 0)
