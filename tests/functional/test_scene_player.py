"""The in-container scene player, exercised through a real server."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import TestCase

from PIL import Image

from tests.goldens import click_targets, scenes

from .utils import (
    HOST,
    SCENE_SERVERS,
    TIGERVNC,
    X11VNC,
    FleetTestCase,
    VNCServer,
    assert_fleet_current,
    fleet_tag,
    port_open,
    run_vncdo,
)


class TestScenePlayer(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert_fleet_current(TIGERVNC)

    def setUp(self) -> None:
        if not port_open(HOST, TIGERVNC.port):
            self.fail(f"{TIGERVNC.name} is not listening on {TIGERVNC.port}; {TIGERVNC.how_to_start}")

    def _capture(self, *args: str) -> Image.Image:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screen.png"
            # The player repaints asynchronously to the key event reaching
            # the X server, so an immediate capture can still see the prior frame.
            result = run_vncdo(TIGERVNC, *args, "pause", "0.3", "capture", str(path))
            if result.returncode != 0:
                self.fail(f"vncdo failed ({result.returncode}): {result.stderr}")
            return Image.open(path).copy()

    def test_serves_the_golden_geometry(self) -> None:
        self.assertEqual(self._capture("key", "0").size, scenes.SIZE)

    def test_a_scene_key_changes_the_screen(self) -> None:
        reset = self._capture("key", "0")
        solid = self._capture("key", "s")
        self.assertNotEqual(reset.tobytes(), solid.tobytes())

    def test_the_patch_names_the_key_that_was_pressed(self) -> None:
        for key in ("0", "s", "d", "g"):
            with self.subTest(key=key):
                self.assertEqual(scenes.read_patch(self._capture("key", key)), key)

    def test_a_shifted_key_selects_the_same_scene(self) -> None:
        self.assertEqual(scenes.read_patch(self._capture("key", "0")), "0")
        self.assertEqual(scenes.read_patch(self._capture("key", "S")), "s")


TESTS_DIR = Path(__file__).resolve().parents[1]
BESIDE_XEV_TIMEOUT = 60.0


class TestScenePlayerBesideXev(TestCase):
    """The x11vnc container runs `xev -root` beside the player, and X lets one
    client at a time select ButtonPress on a window."""

    def test_a_key_changes_the_scene_when_xev_already_watches_the_root_window(self) -> None:
        prefix = os.environ.get("FLEET_IMAGE_PREFIX", "vncdotool-test")
        image = f"{prefix}-x11vnc:{fleet_tag() or 'dev'}"
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
            self.fail(f"image {image} is not built; {X11VNC.how_to_start}")

        result = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{TESTS_DIR}:/src/tests:ro", "-w", "/src", "-e", "PYTHONPATH=/src",
                "--entrypoint", "python3", image, "-m", "tests.servers.scene_player_keys",
            ],
            capture_output=True,
            text=True,
            timeout=BESIDE_XEV_TIMEOUT,
        )
        if result.returncode != 0:
            self.fail(f"scene_player_keys failed ({result.returncode}): {result.stdout}{result.stderr}")
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["scene"], "s", report["player_output"])


CLICKED_SCENE = "s"
REPAINT_DEADLINE = 10.0
REPAINT_POLL = 0.3


class ClickSelectsAScene:
    server: VNCServer

    def _capture(self, *args: str) -> Image.Image:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screen.png"
            result = run_vncdo(self.server, *args, "pause", "0.3", "capture", str(path))
            if result.returncode != 0:
                self.fail(f"{self.server.name}: vncdo failed ({result.returncode}): {result.stderr}")
            return Image.open(path).copy()

    def test_a_click_selects_the_scene_under_it(self) -> None:
        self._capture("key", "0")
        x, y = click_targets.click_target(CLICKED_SCENE)
        self._capture("move", str(x), str(y), "click", "1")

        # x11vnc polls the X framebuffer rather than being the X server, so a
        # repaint can reach a client a poll interval after the click.
        seen = None
        deadline = time.monotonic() + REPAINT_DEADLINE
        while time.monotonic() < deadline:
            seen = scenes.read_patch(self._capture("pause", "0"))
            if seen == CLICKED_SCENE:
                return
            time.sleep(REPAINT_POLL)

        self.fail(
            f"{self.server.name}: clicking ({x}, {y}) left scene {seen!r} rather "
            f"than {CLICKED_SCENE!r} after {REPAINT_DEADLINE}s; the button event "
            "did not reach the server"
        )


for _server in SCENE_SERVERS:
    if _server.skip_pointer_tests:
        continue
    _name = "TestClick_" + _server.name.replace("-", "_")
    globals()[_name] = type(
        _name, (ClickSelectsAScene, FleetTestCase), {"server": _server, "__module__": __name__}
    )
