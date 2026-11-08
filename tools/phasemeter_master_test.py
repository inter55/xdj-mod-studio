#!/usr/bin/env python3
"""phasemeter_master_test.py -- the ORANGE (master / linked deck) row of
the Type 1 phase meter, emulator check. Nothing here touches a deck.

The scenario the user tests on hardware: THIS deck paused, the linked
master deck playing. pm.c taps the master's beat-in-bar from the link
bytes 0x0A8D042E / 0x0A8BE662 (deck-probed 2026-10-31) and times it on
the firmware's 1 ms system clock 0x0B704A80 (TMU ISR 0x08D632C0). The
first build timed it on POSL (this deck's track position), which stands
still while this deck is paused -> period never measured -> empty row.

From cache/emu_track_loaded.pkl: install pm (same as phasemeter_test),
PAUSE this deck (POSL frozen -- asserted), then play a fake master at
120 BPM by stepping the two link bytes 1,2,3,4,1.. every 500 system ms:
  - pm_mtap counts every beat
  - the orange row shows ticks ~(X1-X0)/8 px apart, red downbeats
  - mid-beat the ruler SCROLLS (tick columns move left as time passes)
  - master stops (bytes quiet) -> ruler FREEZES (two shots identical)
"""
import os
os.environ.setdefault("XDJ_HLE_DSP", "1")
import sys
sys.path[:0] = [".", "tools"]
from emu.machine import Machine
from ghosttap_emu import fb_png
import phasemeter_test as PT

SYS_MS = 0x0B704A80
MST = (0x0A8D042E, 0x0A8BE662)
OY = 124
SHOT = "screenshots/phasemeter_master_{}.png"


def row(path):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    red, tick = [], []
    for x in range(PT.X0, PT.X1 + 1):
        r, g, b = im.getpixel((x, OY - 5))
        if r > 180 and g < 90 and b < 90:
            red.append(x)
        elif 130 <= r <= 170 and abs(r - g) < 12 and abs(r - b) < 12:
            tick.append(x)
    red = [x for x in red if x + 1 not in red]      # 2 px wide: right col
    return red, tick


def main():
    PT.build()
    s = PT.syms()
    base = s["pm_entry"]
    img = open(f"{PT.OUT}/pm.bin", "rb").read()
    m = Machine.load("cache/emu_track_loaded.pkl")
    mem, c = m.mem, m.cpu
    end = s["pm_mode"] + 4
    assert all(mem.r8(a) == 0 for a in range(base, end, 4)), "cave not empty"
    for i, b in enumerate(img):
        mem.w8(base + i, b)
    mem.w32(s["pm_prev"], mem.r32(PT.HOOK))
    mem.w8(s["pm_mode"], 1)
    mem.w32(PT.HOOK, base)
    PT.install_grid(mem)
    mem.w32(PT.PREVIEW, mem.r32(PT.PREVIEW) ^ 1)
    mem.w32(PT.MODE, 3)                          # THIS deck paused
    for a in MST:
        mem.w8(a, 0)
    c.run(limit=20_000_000)

    def until(ms):                               # run to a system-ms time
        while (mem.r32(SYS_MS) - ms) & 0x80000000:
            c.run(limit=200_000)

    def setbar(v):
        for a in MST:
            mem.w8(a, v)

    ok = True
    posl0, t = mem.r32(PT.POSL), mem.r32(SYS_MS)
    tap0 = mem.r32(s["pm_mtap"])
    bar, beats = 1, 9
    for k in range(beats):                       # 120 BPM master
        setbar(bar)
        bar = bar % 4 + 1
        t += 500
        until(t)
    taps = mem.r32(s["pm_mtap"]) - tap0
    frozen = mem.r32(PT.POSL) == posl0
    print(f"this deck paused (POSL frozen): {frozen}; master taps {taps} "
          f"(want {2 * (beats - 1)}: each beat on BOTH mirrors)")
    ok &= frozen and taps == 2 * (beats - 1)
    # mid-beat: two shots 150 ms apart -> ticks move left
    setbar(bar)
    t0 = mem.r32(SYS_MS)
    until(t0 + 100)
    fb_png(m, SHOT.format("a"))
    until(t0 + 250)
    fb_png(m, SHOT.format("b"))
    ra, ta = row(SHOT.format("a"))
    rb, tb = row(SHOT.format("b"))
    allx = sorted(ra + ta)
    gaps = [b - a for a, b in zip(allx, allx[1:])]
    print(f"shot a: red {ra} tick {ta}")
    print(f"shot b: red {rb} tick {tb}")
    want_gap = (PT.X1 - PT.X0) / 8
    spaced = len(allx) >= 6 and all(abs(g - want_gap) <= 2 for g in gaps
                                   if g < 2 * want_gap)
    moved = bool(ra) and bool(rb) and rb[0] < ra[0]
    print(f"ticks present + ~{want_gap:.1f} px apart: {spaced}; "
          f"scrolls left: {moved} ({ra[:1]} -> {rb[:1]})")
    ok &= spaced and moved
    # master stops: bytes quiet -> frozen ruler
    until(t0 + 2500)
    fb_png(m, SHOT.format("c"))
    until(t0 + 3500)
    fb_png(m, SHOT.format("d"))
    rc, tc = row(SHOT.format("c"))
    rd, td = row(SHOT.format("d"))
    held = (rc, tc) == (rd, td) and bool(rc + tc)
    print(f"master stopped -> ruler frozen: {held}")
    ok &= held
    # 2026-11-08 DECK REPORT: on the deck that is NOT the master the two link
    # mirrors are two different players' bars, so they disagree -- the old
    # "mirrors must agree" test then rejected every frame and only the blue
    # half of the meter drew. Model exactly that: one mirror cycles, the
    # other sits on a DIFFERENT value. Each mirror is tapped separately now.
    # (mirror 1 keeps a DIFFERENT player's bar for the whole leg)
    t0 = mem.r32(SYS_MS)
    tap0 = mem.r32(s["pm_mtap"])
    bar, beats = mem.r8(MST[0]) % 4 + 1, 8      # start on a valid step
    mem.w8(MST[1], bar % 4 + 1)                 # the other player's bar
    lost = 0
    for k in range(beats):                       # 120 BPM on mirror 0 only
        mem.w8(MST[0], bar)
        bar = bar % 4 + 1
        t0 += 500
        until(t0)
        lost += mem.r8(MST[0]) != (bar + 2) % 4 + 1
    disagree = mem.r32(s["pm_mtap"]) - tap0
    until(t0 + 120)
    fb_png(m, SHOT.format("e"))
    re_, te = row(SHOT.format("e"))
    until(t0 + 300)
    fb_png(m, SHOT.format("f"))
    rf, tf = row(SHOT.format("f"))
    scroll = bool(re_) and bool(rf) and rf[0] < re_[0]
    print(f"mirrors disagree: taps {disagree} (want ~{beats - 1}, one mirror "
          f"only), followed tap {mem.r32(s['pm_msel'])}, bytes rewritten under "
          f"us {lost}, orange row red {re_} tick {te}, "
          f"scrolls {scroll} ({re_[:1]} -> {rf[:1]})")
    ok &= beats - 2 <= disagree <= beats and mem.r32(s["pm_msel"]) == 0 \
        and lost == 0 and scroll
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
