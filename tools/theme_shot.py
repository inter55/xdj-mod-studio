#!/usr/bin/env python3
"""theme_shot.py NAME [CKPT] -- screenshot of a themed deck on a real track.

theme_deck.py applies a theme the way the DECK has to: a one-shot flag that
the overview cave consumes on the next track load, recolouring the artwork in
RAM. That path needs the overview painter to run, which it does on the
snapshot theme_deck ships with -- but not on the snapshots that have other
tracks loaded (verified: the flag stays set, the cave is never entered).

For a SCREENSHOT we do not need the delivery mechanism, only its result: the
recoloured artwork block in RAM. So write exactly the words the theme's
colour map would write (same tables, same regions, same src->dst map), let
the deck redraw, and capture. The pixels are the pixels a themed deck shows.

The snapshot is used as it stands -- the baseline is captured BEFORE any key
press, because navigating away from a loaded track clears the overview.
Artwork words must still be stock before writing; if the firmware already
changed them the run reports it instead of overwriting blind.

  .venv/bin/python tools/theme_shot.py hot-pink cache/emu_rgb_daveamstrong.pkl
  PLAY_STEPS=40000000 ... (press PLAY first)
"""
import os
import sys

sys.path[:0] = [".", "tools", "mods"]

import numpy as np
from PIL import Image

import theme_deck as TD
from emu.machine import Machine
from emu import panl
from ghosttap_emu import fb_png

STRIP = (slice(400, 440), slice(110, 620))      # overview waveform band


def strip_sum(path):
    im = np.asarray(Image.open(path).convert("L")).astype(int)
    return int(im[STRIP].sum())


def run(name, ckpt):
    m = Machine.load(ckpt)
    mem, dev = m.mem, m.mem.dev

    def key(k, n=30_000_000):
        dev.panel_frame = panl.build_frame(keys=[k])
        m.cpu.run(limit=3_000_000)
        dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=n)

    if os.environ.get("LOAD_FIRST") == "1":
        # a nav snapshot: press the track and settle on the main screen, so
        # the strip and chrome are drawn from a freshly loaded track
        key("RotaryPush", 60_000_000)
        key("Browse", 40_000_000)
        key("Browse", 40_000_000)

    play = int(os.environ.get("PLAY_STEPS", "0"))
    if play:
        # start playback first: a playing deck is the shot worth taking (real
        # REMAIN, a drawn centre waveform) and the strip keeps animating
        key("PlayPause", play)

    before_png = "/tmp/theme_shot_before.png"
    fb_png(m, before_png)

    w = TD.stock_words()
    before = np.frombuffer(bytes(mem.ram0[:len(w) * 2]), "<u2").copy()
    regions, src, dst, new = TD.theme_tables(name, w)
    want = new != w
    still_stock = (before == w) & want
    drifted = int((want & ~still_stock).sum())
    print(f"theme {name}: {int(want.sum()):,} words to recolour in "
          f"{len(regions)} regions, {len(src)} colours in the map")
    if drifted:
        print(f"  {drifted:,} already differ from stock in RAM -- left alone")
    if not still_stock.any():
        print("RESULT: FAIL - nothing left to recolour")
        return 1

    arr = before.copy()
    arr[still_stock] = new[still_stock]
    mem.ram0[:len(w) * 2] = arr.tobytes()

    # one round trip through the browser overlay and back: the only redraw
    # that reaches the main screen without loading another track
    key("Browse")
    key("Browse")
    out = f"screenshots/theme_shot_{name}.png"
    fb_png(m, out)

    a = np.asarray(Image.open(before_png).convert("RGB")).astype(int)
    b = np.asarray(Image.open(out).convert("RGB")).astype(int)
    diff = int((np.abs(a - b).sum(2) > 0).sum())
    pct = 100.0 * diff / (a.shape[0] * a.shape[1])
    s0, s1 = strip_sum(before_png), strip_sum(out)
    print(f"screen changed {diff:,} px ({pct:.1f}%)")
    print(f"overview strip brightness {s0:,} -> {s1:,} "
          f"({'kept' if s1 > 0.9 * s0 else 'CHANGED/LOST'})")
    ok = diff > 100_000 and s1 > 0.9 * s0
    print("RESULT:", "PASS" if ok else "FAIL", "->", out)
    return 0 if ok else 1


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "hot-pink"
    ckpt = (sys.argv[2] if len(sys.argv) > 2
            else os.environ.get("CKPT", "cache/emu_rgb_daveamstrong.pkl"))
    return run(name, ckpt)


if __name__ == "__main__":
    sys.exit(main())
