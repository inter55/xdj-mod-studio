#!/usr/bin/env python3
"""phasemeter_dsp_test.py -- phase meter on the REAL-audio-chip emulator
(cache/emu_dsp_playing7.pkl: Get Lucky playing, rekordbox beat grid). The
position word 0x0C0E6D18 is driven by the emulated DSP exactly as on the
deck (2026-10-25 deck reading), and pm.c reads it through the uncached P2
alias. The emulator has no cache, so this gate cannot reproduce a
stale-cache freeze; a real deck is still required for that. It checks the
meter end to end: the
red/grey tick columns on the blue row == the model from the track's own
grid at the position the meter read, a few frames apart while it plays.
PASS = every checked frame matches within 2 px."""
import os
import sys
sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402
from ghosttap_emu import fb_png          # noqa: E402
import phasemeter_test as T              # noqa: E402

POS = 0x0C0E6D18


def main():
    T.build()                 # never test a pm.bin older than pm.c
    s = T.syms()
    img = open(f"{T.OUT}/pm.bin", "rb").read()
    m = Machine.load(sys.argv[1] if len(sys.argv) > 1 else "cache/emu_dsp_playing7.pkl")
    mem = m.mem
    base = s["pm_entry"]
    for i, b in enumerate(img):
        mem.w8(base + i, b)
    mem.w32(s["pm_prev"], mem.r32(T.HOOK))
    mem.w8(s["pm_mode"], 1)
    mem.w32(T.HOOK, base)
    m.cpu.run(limit=30_000_000)
    g = T.read_grid(mem, s)
    print(f"grid {len(g)} beats from the track, first {g[:2]}")
    ok = len(g) > 100
    from PIL import Image
    for k in range(4):
        m.cpu.run(limit=25_000_000)
        p = mem.r32(POS)
        fb_png(m, f"screenshots/phasemeter_dsp_{k}.png")
        im = Image.open(f"screenshots/phasemeter_dsp_{k}.png").convert("RGB")
        got = {}
        for x in range(T.X0, T.X1 + 1):
            r, g_, b = im.getpixel((x, T.BY - 5))
            if r > 180 and g_ < 90 and b < 90:
                got[x] = "red"
            elif 130 <= r <= 170 and abs(r - g_) < 12 and abs(r - b) < 12:
                got[x] = "tick"
        got = {x: v for x, v in got.items() if not (v == "red" and got.get(x + 1) == "red")}
        want = T.model(g, p * 20 // 3)
        near = lambda x: abs(x - T.CX) <= 3 or x <= T.X0 + 5 or x >= T.X1 - 5  # line + edges: 1-frame timing
        got = {x: v for x, v in got.items() if not near(x)}
        want = {x: v for x, v in want.items() if not near(x)}
        # the screen may be one frame older than the position read: 2 px slack
        match = len(got) == len(want) and all(
            any(abs(x - y) <= 2 and got[x] == want[y] for y in want) for x in got)
        print(f"pos {p} ({p * 20 // 3} ms): {'MATCH' if match else 'DIFF'} "
              f"got {sorted(got.items())} want {sorted(want.items())}")
        ok &= match
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
