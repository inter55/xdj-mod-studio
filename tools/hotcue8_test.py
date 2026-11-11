#!/usr/bin/env python3
"""hotcue8_test.py -- 8 hot cue pages on the emulated deck (PERFORM screen).

Needs the snapshot tools/hotcue8_emu.py saves (mini stick, track with hot
cues A-H at 5,13,..61 s loaded): $TMPDIR/hotcue8_loaded.pkl or argv[1].
Run it on BOTH layouts: HC_LAYOUT=rekordbox tools/hotcue8_emu.py saves
$TMPDIR/hc_rb_loaded.pkl (A-C in .DAT, A-H only in .EXT PCO2 -- the real
export split, GitHub #6; the 1.0.4 code fails it, D plays A).
Installs the PERFORM pads + the hotcue8 C module exactly like the deck
installers would, then with REAL touches:
  TAP the HOT CUE tab (in HOT CUE mode) -> the 8-pad view: all eight cues
  drawn at once, a press on E/H borrows a live slot and puts it back, an
  empty pad is swallowed; tap again -> the stock A B C REC CALL row.
  SWIPES page A-C -> D-F -> G-H both ways (2026-10-21); a swipe leaves the
  8-pad view. REC tap on page 2 is swallowed (stock REC never sees it);
  BEAT JUMP tab then HOT CUE tab = a mode change, not a view change;
  letters drawn (hc_draws/hc_draws8 > 0) + screenshots/hotcue8/*.png.
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
    hook, touch = 0x08C92E20, 0x08D5278C
    mem.w32(syms["hc_prev"], mem.r32(hook))
    mem.w32(syms["hc_tprev"], mem.r32(touch))
    mem.w32(syms["hc_armed"], 1)
    mem.w32(touch, syms["hc_tentry"])
    mem.w32(hook, syms["hc_entry"])


def main():
    ck = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hotcue8_loaded.pkl")
    if not os.path.exists(ck):
        print(f"SKIP: {ck} missing -- build it first with "
              "`.venv/bin/python tools/hotcue8_emu.py`")
        return 0
    os.makedirs(OUT, exist_ok=True)
    code, syms = build()
    m = Machine.load(ck)
    mem, dev = m.mem, m.mem.dev
    ST = mem.r16(0x0B6C4118 + 8)     # the state THIS deck uses for a set cue (6 = test
                                      # cues with status 4, 4 = real rekordbox cues)
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

    def model(base, k):                        # the pads read these, not
        r = base + k * 0x38                    # the live player slots
        return (mem.r16(r), mem.r16(r + 2), mem.r32(r + 4))

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

    def drag(dx, steps=3):                        # a SHORT drag from the middle
        for k in range(0, steps + 1):             # of the 92-px tab strip (k=0 is
                                                  # the touch-down: without it the
                                                  # deck records the down-x at the
                                                  # first moved position)
            dev.panel_frame = panl.build_frame(touch=(tab[0] + dx * k // steps, tab[1]))
            run(8_000_000)
        dev.panel_frame = panl.build_frame()
        run(30_000_000)
    # --- the 8-pad view (tap; integrated 2026-11-07) --------------------
    tap(*tab)
    chk("tap: 8-pad view on, page 1", mem.r32(syms["hc_view"]) == 1 and
        mem.r8(PP.HCPAGE) == 0)
    run(30_000_000)
    chk("8 pads drawn", mem.r32(syms["hc_draws8"]) > 0)
    fb_png(m, f"{OUT}/grid8.png")
    padx = [116 + 63 * i + 29 for i in range(8)]
    pady = 263
    page1 = slots()
    dev.panel_frame = panl.build_frame(touch=(padx[4], pady))   # pad E
    run(12_000_000)
    chk("grid: E held borrows slot 1 -> cue 5",
        (mem.r16(SLOT + 0x40 + 0x0A), mem.r32(SLOT + 0x40 + 0x0C)) == (5, TIMES[4]))
    chk("grid: E held borrows the pad models too",
        model(0x0BA28800, 1) == (ST, 5, TIMES[4]) and
        model(0x0BA2A52C, 1) == (ST, 5, TIMES[4]))
    chk("grid: E sets no stock zone (hit-tester untouched)",
        mem.r32(0x0C0E75BC) == 0x1E)
    fb_png(m, f"{OUT}/grid8_held_E.png")
    dev.panel_frame = panl.build_frame()
    run(40_000_000)
    chk("grid: E released restores slot 1", slots() == page1)
    chk("grid: E released restores the pad models",
        model(0x0BA28800, 1) == (ST, 2, TIMES[1]) and
        model(0x0BA2A52C, 1) == (ST, 2, TIMES[1]))
    dev.panel_frame = panl.build_frame(touch=(padx[7], pady))   # pad H = old REC
    run(12_000_000)
    chk("grid: H (old REC spot) borrows -> cue 8",
        (mem.r16(SLOT + 0x40 + 0x0A), mem.r32(SLOT + 0x40 + 0x0C)) == (8, TIMES[7]))
    dev.panel_frame = panl.build_frame()
    run(40_000_000)
    chk("grid: H released restores slot 1", slots() == page1)
    sw_before = mem.r32(syms["hc_swallowed"])
    dev.panel_frame = panl.build_frame(touch=(116 + 63 * 2 + 60, pady))  # gap
    run(12_000_000)
    chk("grid: gap swallowed", mem.r32(syms["hc_swallowed"]) == sw_before + 1 and
        slots() == page1)
    dev.panel_frame = panl.build_frame()
    run(30_000_000)
    # DELETE and a borrowed slot (measured 2026-11-07): MEMORY-DELETE is zone
    # 17 (0x11) and its own handler 0x08DD0B60 sets KEYB+84 bit 0x40 -- it is
    # NOT a panel key (holding the "Delete" frame bit does not move it). One
    # panel frame carries a SINGLE touch point (payload +22..+25), so DELETE
    # and a pad cannot be pressed together: the "delete the borrowed slot"
    # combination is unreachable. hc_touch still reads that bit and swallows
    # D-H presses as defence in depth; that guard cannot be driven through the
    # emulator's one point, so it is not claimed as emulator-tested here.
    # What IS tested: D (a borrowed pad) works normally afterwards.
    dev.panel_frame = panl.build_frame(touch=(padx[3], pady))      # pad D
    run(12_000_000)
    chk("grid: D works (slot 0 = cue 4)",
        (mem.r16(SLOT + 0x0A), mem.r32(SLOT + 0x0C)) == (4, TIMES[3]))
    dev.panel_frame = panl.build_frame()
    run(40_000_000)
    chk("grid: D released restores slot 0", slots() == page1)
    tap(*tab)
    chk("tap again: stock row back", mem.r32(syms["hc_view"]) == 0 and
        mem.r8(PP.HCPAGE) == 0)

    swipe(100, 30)                                # swipe -> page 2 (D E F)
    s = slots()
    print("page", mem.r8(PP.HCPAGE), "slots", s, "count", mem.r32(syms["hc_count"]))
    chk("page 2 = D E F", mem.r8(PP.HCPAGE) == 1 and
        [x[0] for x in s] == [4, 5, 6] and [x[1] for x in s] == TIMES[3:6])
    want = [(ST, 4, TIMES[3]), (ST, 5, TIMES[4]), (ST, 6, TIMES[5])]
    chk("page 2 pad models = D E F",
        [model(0x0BA28800, k) for k in range(3)] == want and
        [model(0x0BA2A52C, k) for k in range(3)] == want)

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
    # measured stock empty record (build/hc_model_probe.py): state 0, number
    # 0, ms 0, pos 0 -- loop 0xFFFFFFFF in model A but 0 in model B
    chk("page 3 empty pad = stock empty patterns (A -1 / B 0)",
        mem.r16(0x0BA28800 + 2 * 0x38) == 0 and
        mem.r32(0x0BA28800 + 2 * 0x38 + 4) == 0 and
        mem.r32(0x0BA28800 + 2 * 0x38 + 8) == 0xFFFFFFFF and
        mem.r16(0x0BA2A52C + 2 * 0x38) == 0 and
        mem.r32(0x0BA2A52C + 2 * 0x38 + 4) == 0 and
        mem.r32(0x0BA2A52C + 2 * 0x38 + 8) == 0)
    run(30_000_000)
    fb_png(m, f"{OUT}/page3.png")
    swipe(100, 30)                                # left from page 3 wraps -> page 1
    s = slots()
    chk("page 1 = A B C again", mem.r8(PP.HCPAGE) == 0 and
        [x[0] for x in s] == [1, 2, 3] and [x[1] for x in s] == TIMES[0:3])
    chk("pad models back to A B C",
        [model(0x0BA28800, k)[1] for k in range(3)] == [1, 2, 3] and
        [model(0x0BA2A52C, k)[1] for k in range(3)] == [1, 2, 3])
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
    tap(*tab)                                     # in mode now: tap = 8 pads
    chk("tap from the stock row: 8-pad view, page 1",
        mem.r32(syms["hc_view"]) == 1 and mem.r8(PP.HCPAGE) == 0 and
        slots()[0][0] == 1)
    tap(*tab)                                     # -> stock row
    chk("tap again: stock row, page 1", mem.r32(syms["hc_view"]) == 0 and
        mem.r8(PP.HCPAGE) == 0 and slots()[0][0] == 1)
    run(30_000_000)
    fb_png(m, f"{OUT}/back_to_hc.png")
    # 2026-11-06: the swipe threshold went 40 -> 24 px because the tab strip is
    # only TAB_W=92 px wide, so a 40-px drag rarely fit inside it. These run
    # LAST so they cannot shift the page for the legs above. The gate is
    # cmp/gt (strictly greater), hence a 32-px drag: 24 exactly does not pass.
    p = mem.r8(PP.HCPAGE)                         # 0 = A B C
    drag(+32)                                     # right = previous page
    chk("32 px swipe right pages back", mem.r8(PP.HCPAGE) == (p - 1) % 3)
    run(30_000_000)
    drag(-32)                                     # left = next page
    chk("32 px swipe left pages forward", mem.r8(PP.HCPAGE) == p)
    run(30_000_000)
    drag(-8)                                      # wobble: a TAP -> 8-pad view
    chk("8 px wobble is a tap (8-pad view)", mem.r8(PP.HCPAGE) == 0 and
        mem.r32(syms["hc_view"]) == 1)
    run(30_000_000)
    tap(*tab)                                     # tap back: stock row, page 1
    chk("tap back: page 1, stock row", mem.r8(PP.HCPAGE) == 0 and
        mem.r32(syms["hc_view"]) == 0 and slots()[0][0] == 1)
    mem.w8(MS.MSOFF, 1)                           # MODS OFF: no paging/view
    v = mem.r32(syms["hc_view"])
    tap(*tab)
    chk("MODS OFF: tab does nothing", mem.r8(PP.HCPAGE) == 0 and
        mem.r32(syms["hc_view"]) == v)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
