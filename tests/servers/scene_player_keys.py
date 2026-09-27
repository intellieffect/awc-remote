#!/usr/bin/env python3
"""Start the scene player after `xev -root` on a private display, press a key,
and print which scene the root window then shows, as JSON.

Runs inside the x11vnc image, where Xvfb, xev and python3-xlib live, and opens
no network connection: Xvfb listens on its unix socket alone. The scene
player is the checkout's, mounted over the one baked into the image.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from Xlib import X, XK, display
from Xlib.ext import xtest

from tests.goldens import scenes

DISPLAY = ":7"
DEADLINE = 10.0
POLL = 0.1


def wait_for(predicate, what: str):
    end = time.monotonic() + DEADLINE
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(POLL)
    raise SystemExit(f"scene_player_keys: timed out waiting for {what}")


def connect():
    try:
        return display.Display(DISPLAY)
    except Exception:
        return None


def root_selects(d: display.Display, mask: int) -> bool:
    return bool(d.screen().root.get_attributes().all_event_masks & mask)


def mapped_children(d: display.Display) -> int:
    root = d.screen().root
    return sum(1 for w in root.query_tree().children if w.get_attributes().map_state == X.IsViewable)


def shown_scene(d: display.Display):
    width, height = scenes.SIZE
    raw = d.screen().root.get_image(0, 0, width, height, X.ZPixmap, 0xFFFFFFFF).data
    from PIL import Image

    return scenes.read_patch(Image.frombytes("RGB", (width, height), raw, "raw", "BGRX"))


def main() -> None:
    os.environ["DISPLAY"] = DISPLAY
    width, height = scenes.SIZE
    xvfb = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    player = xev = None
    try:
        d = wait_for(connect, "Xvfb")
        # The container's event sink, started first, so it holds the root
        # window's ButtonPressMask before the player asks for anything.
        xev = subprocess.Popen(["xev", "-root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: root_selects(d, X.ButtonPressMask), "xev to select ButtonPress on the root window")

        player = subprocess.Popen(
            [sys.executable, "-m", "tests.goldens.scene_player"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_for(lambda: player.poll() is not None or mapped_children(d), "the scene player's window")
        wait_for(lambda: player.poll() is not None or shown_scene(d) == "0", "the startup scene")

        if player.poll() is None:
            keycode = d.keysym_to_keycode(XK.string_to_keysym("s"))
            xtest.fake_input(d, X.KeyPress, keycode)
            xtest.fake_input(d, X.KeyRelease, keycode)
            d.sync()
            end = time.monotonic() + DEADLINE
            while time.monotonic() < end and shown_scene(d) != "s":
                time.sleep(POLL)

        scene = shown_scene(d)
        running = player.poll() is None
    finally:
        for process in (player, xev, xvfb):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (player, xev, xvfb):
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    output = player.stdout.read() if player is not None and player.stdout else ""
    print(json.dumps({"scene": scene, "player_running": running, "player_output": output}))


if __name__ == "__main__":
    main()
