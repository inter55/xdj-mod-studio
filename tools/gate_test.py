#!/usr/bin/env python3
"""gate_test.py -- hot-cue GATE mode end-to-end in the emulator.

Flow: load the session track, open PERFORM (native touch), PLAY
(mode 5), arm the gate (MY SETTINGS [HOT CUE] -> GATE), press +
release stock hot-cue A by native touch, then check: GATELATCH
cleared, PlayPause pulsed, player paused (mode 5 -> 3).

Caveat (known emu gap, §48): a stock hot-cue press can stall the
emulated panel link (TX DMA never re-armed without the DSP). If the
release never processes, the RESULT FAILs in emu but the logic is
still deck-testable (deck link is fine).
"""
import os
import sys

sys.path[:0] = [".", "tools"]
from emu.machine import Machine
from emu import panl
import mysettings as MSET
import perform_pads as PP
from ghosttap_emu import fb_png

MODEW = 0x0B6C4264
CKPT = os.environ.get("CKPT", "cache/emu_nav_track.pkl")
OUT = "screenshots/gate"


def key(m, name, hold=3_000_000, after=25_000_000):
    dev = m.mem.dev
    dev.panel_frame = panl.build_frame(keys=[name])
    m.cpu.run(limit=hold)
    dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=after)


def touch_raw(m, x, y, hold=3_000_000, after=10_000_000):
    dev = m.mem.dev
    fr = bytearray(panl.build_frame())
    fr[22], fr[23] = (x >> 8) & 0xFF, x & 0xFF
    fr[24], fr[25] = (y >> 8) & 0xFF, y & 0xFF
    fr[30] = panl.checksum(fr[:30])
    dev.panel_frame = bytes(fr)
    m.cpu.run(limit=hold)
    dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=after)


def touch_down(m, xy, hold=3_000_000):
    m.mem.dev.panel_frame = panl.build_frame(touch=xy)
    m.cpu.run(limit=hold)


def touch_up(m, after=10_000_000):
    m.mem.dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=after)


def main():
    os.makedirs(OUT, exist_ok=True)
    m = Machine.load(CKPT)
    mem = m.mem
    PP.install(mem)
    MSET.install(mem)
    key(m, "RotaryPush")              # load the track
    m.cpu.run(limit=300_000_000)
    touch_raw(m, 0x090, 0x06A)        # PERFORM tab (touch)
    m.cpu.run(limit=50_000_000)
    print("screen mode:", hex(mem.r32(PP.MODEW)))
    fb_png(m, f"{OUT}/perform.png")
    key(m, "PlayPause")               # play
    m.cpu.run(limit=50_000_000)
    mode = mem.r32(MODEW)
    print("mode after PLAY:", hex(mode))
    ok = (mode == 5)
    mem.w8(PP.MSGATE, 1)              # arm the gate (row UI in mysettings)
    if mem.r32(PP.MODEW) != 7:        # PERFORM's top-right button is a TOGGLE:
        touch_raw(m, 0x090, 0x06A)    # re-tapping it while open closes PERFORM
    # stock hot cue A pad, HOT CUE tab active: press, then release
    touch_down(m, (165, 264))
    held = mem.r8(PP.PST + 3)
    print("latch while held:", hex(held), "gate:", hex(mem.r8(PP.MSGATE)))
    fb_png(m, f"{OUT}/held.png")
    touch_up(m)
    m.cpu.run(limit=50_000_000)
    released = mem.r8(PP.PST + 3)
    print("latch after release:", hex(released),
          "mode:", hex(mem.r32(MODEW)),
          "PAY16:", hex(mem.r8(PP.PAY16)),
          "PLAYPP:", hex(mem.r8(PP.PLAYPP)))
    fb_png(m, f"{OUT}/released.png")
    ok &= held == 1
    ok &= released == 0
    ok &= mem.r32(MODEW) == 3
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
