#!/usr/bin/env python3
"""hotcue8_test.py -- 8 hot cue pages on the emulated deck (PERFORM screen).

Needs the snapshot tools/hotcue8_emu.py saves (mini stick, track with hot
cues A-H at 5,13,..61 s loaded): $TMPDIR/hotcue8_loaded.pkl or argv[1].
Installs the PERFORM pads + the hotcue8 C module exactly like the deck
installers would, then with REAL touches:
  HOT CUE tab tap (in HOT CUE mode) -> page 2 (slots = D,E,F), again ->
  page 3 (G,H,empty), again -> page 1 (A,B,C); swipes page both ways
  too (2026-10-21: tap AND swipe both work -- three testers naturally
  tapped); REC tap on page 2 is swallowed (stock REC never sees it);
  BEAT JUMP tab then HOT CUE tab = a mode change, not a page change;
  letters drawn (hc_draws > 0) + screenshots/hotcue8/*.png.
"""
import os
import subprocess
import sys

sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
from ghosttap_emu import fb_png          # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402

SLOT = 0x0B6C4118
TIMES = [5000 + 8000 * i for i in range(8)]
OUT = "screenshots/hotcue8"


def build():
    env = dict(os.environ, HC_BASE=hex(PP.HC_BASE), HCPAGE=hex(PP.HCPAGE),
               PMODE=hex(PP.PST + 2), MSOFF=hex(MS.MSOFF))
    subprocess.run(["sh", "tools/hotcue8/build.sh"], check=True, env=env,
                   capture_output=True)
    code = open("build/hotcue8/hc.bin", "rb").read()
    syms = {}
    for ln in open("build/hotcue8/hc.syms"):
        a, _, n = ln.split()
        syms[n.lstrip("_")] = int(a, 16)
    return code, syms


def install_hc(mem, code, syms):
    """same order as a deck installer: code (bss zeroed), chain, hook last"""
    base = syms["hc_table"]
    for a in range(base, syms.get("end", syms["hc_count"] + 4), 4):
        mem.w32(a, 0)
    for i, b in enumerate(code):
        mem.w8(base + i, b)
    hook = 0x08C92E20
    mem.w32(syms["hc_prev"], mem.r32(hook))
    mem.w32(hook, syms["hc_entry"])


def main():
    ck = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hotcue8_loaded.pkl")
    os.makedirs(OUT, exist_ok=True)
    code, syms = build()
    m = Machine.load(ck)
    mem, dev = m.mem, m.mem.dev
    PP.install(mem)
    install_hc(mem, code, syms)
    mem.w8(MS.MSOFF, 0)

    def run(n):
        m.cpu.run(limit=n)

    def tap(x, y, hold=12_000_000, after=30_000_000):
        dev.panel_frame = panl.build_frame(touch=(x, y))
        run(hold)
        dev.panel_frame = panl.build_frame()
        run(after)

    def slots():
        return [(mem.r16(SLOT + k * 0x40 + 0x0A), mem.r32(SLOT + k * 0x40 + 0x0C),
                 mem.r32(SLOT + k * 0x40 + 0x08)) for k in range(3)]

    ok = True

    def chk(label, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + label)
        ok &= bool(cond)

    run(20_000_000)
    tap(709, 37)                                  # PERFORM
    chk("PERFORM open", mem.r32(0x0BA371D4) == 7)
    run(30_000_000)                               # a flush -> hc_count refresh
    s = slots()
    chk("cues seen: 8", mem.r32(syms["hc_count"]) == 8 and
        [x[0] for x in s] == [1, 2, 3] and [x[1] for x in s] == TIMES[0:3])
    print("slots page 1:", s)
    tab = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2)
    # pages step on a TAP of the tab (2026-10-21) and on a horizontal
    # SWIPE along the tab row; taps from another mode only switch modes.
    def swipe(x0, x1, y=tab[1]):                  # finger slides along the tab
        for x in (x0, (x0 + x1) // 2, x1):
            dev.panel_frame = panl.build_frame(touch=(x, y))
            run(8_000_000)
        dev.panel_frame = panl.build_frame()
        run(30_000_000)
    tap(*tab)                                     # tap -> page 2 (D E F)
    s = slots()
    print("page", mem.r8(PP.HCPAGE), "slots", s, "count", mem.r32(syms["hc_count"]))
    chk("page 2 = D E F", mem.r8(PP.HCPAGE) == 1 and
        [x[0] for x in s] == [4, 5, 6] and [x[1] for x in s] == TIMES[3:6])
    run(30_000_000)
    fb_png(m, f"{OUT}/page2.png")
    chk("letters drawn", mem.r32(syms["hc_draws"]) > 0)
    # REC on page 2: held press must be latched by the pads (code 50)
    dev.panel_frame = panl.build_frame(touch=(PP.REC_X + 30, PP.HC_Y + 30))
    run(12_000_000)
    chk("REC swallowed on page 2", mem.r8(PP.PST) == 50)
    dev.panel_frame = panl.build_frame()
    run(30_000_000)
    swipe(100, 30)                                # another left -> page 3 (G H -)
    s = slots()
    print("page", mem.r8(PP.HCPAGE), "slots", s)
    chk("page 3 = G H -", mem.r8(PP.HCPAGE) == 2 and [x[0] for x in s[:2]] == [7, 8]
        and [x[1] for x in s[:2]] == TIMES[6:8] and s[2][2] == 0xFFFFFFFF)
    run(30_000_000)
    fb_png(m, f"{OUT}/page3.png")
    swipe(100, 30)                                # left from page 3 wraps -> page 1
    s = slots()
    chk("page 1 = A B C again", mem.r8(PP.HCPAGE) == 0 and
        [x[0] for x in s] == [1, 2, 3] and [x[1] for x in s] == TIMES[0:3])
    swipe(30, 100)                                # right from page 1 wraps -> page 3
    chk("swipe right wraps: page 3", mem.r8(PP.HCPAGE) == 2 and slots()[0][0] == 7)
    swipe(100, 30)                                # left -> page 1
    chk("swipe left: page 1", mem.r8(PP.HCPAGE) == 0 and slots()[0][0] == 1)
    swipe(30, 100)                                # right wraps -> page 3 again
    chk("swipe right wraps again: page 3", mem.r8(PP.HCPAGE) == 2 and slots()[0][0] == 7)
    swipe(100, 30)                                # left -> page 1
    chk("swipe left to page 1", mem.r8(PP.HCPAGE) == 0 and slots()[0][0] == 1)
    swipe(100, 30)                                # left -> page 2 (D E F) for the BJ leg
    chk("swipe left: page 2", mem.r8(PP.HCPAGE) == 1 and slots()[0][0] == 4)
    chk("latch dropped after swipes", mem.r8(PP.PST) == 0)
    bj = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[1] + PP.TAB_H // 2)
    tap(*bj)                                      # BEAT JUMP mode
    chk("BEAT JUMP mode", mem.r8(PP.PST + 2) == 1)
    tap(*tab)                                     # back to HOT CUE: mode, no page step
    chk("HOT CUE tab from other mode = mode change only",
        mem.r8(PP.PST + 2) == 0 and mem.r8(PP.HCPAGE) == 1)
    tap(*tab)                                     # in mode now: tap pages -> page 3
    chk("tap pages forward", mem.r8(PP.HCPAGE) == 2 and slots()[0][0] == 7)
    tap(*tab)                                     # -> wraps to page 1
    chk("tap wraps to page 1", mem.r8(PP.HCPAGE) == 0 and slots()[0][0] == 1)
    run(30_000_000)
    fb_png(m, f"{OUT}/back_to_hc.png")
    mem.w8(MS.MSOFF, 1)                           # MODS OFF: no paging
    tap(*tab)
    chk("MODS OFF: tab does not page", mem.r8(PP.HCPAGE) == 0)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
