#!/usr/bin/env python3
"""hcpad_test.py -- EMULATOR: hot cue pads page by SWIPE across A B C, cues
fire on a tap (on lift) or a hold, like the BEAT JUMP row.

    .venv/bin/python tools/hcpad_test.py [FIXTURE]

FIXTURE = loaded 8-cue track (tools/hotcue8_emu.py; default
$TMPDIR/hc_s0/hc_rb_s0_loaded.pkl). Installs pads + 8 hot cues + the swipe
module (build/hcpad/hp.bin, chained FIRST in the touch pool as on the deck).

Build the module for the EMULATOR first: its 1 ms clock runs ~6x fast, so
the deck's 150 ms tap/hold window would close after ~25 ms of emulated time:
    HP_CFLAGS="-DHOLD_MS=900u -DREC_GAP_MS=2400u" sh tools/hcpad/build.sh
(then rebuild without it for the deck). PASS 2026-11-13: tap A, swipe left
-> D-F with no jump, tap D, hold E, swipe right -> A-C, tap A.
2026-11-18: REC on pages 2/3 arms the deck's record mode (no cue on the REC
tap alone, the pad press stores); a slide that starts on REC pages instead.
"""
import os
import sys
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402
import hotcue8_test as T                 # noqa: E402

FIX = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "hc_s0", "hc_rb_s0_loaded.pkl")
POS, ROW = 0x0C0E6D18, 264
POOL_TOUCH = 0x08D5278C
PAD_X = (165, 280, 394)


def syms(path):
    return {l.split()[2].lstrip("_"): int(l.split()[0], 16) for l in open(path)}


def main():
    code, hs = T.build()
    m = Machine.load(FIX)
    mem, dev = m.mem, m.mem.dev
    PP.install(mem)
    T.install_hc(mem, code, hs)
    mem.w8(MS.MSOFF, 0)
    s = syms("build/hcpad/hp.syms")
    img = open("build/hcpad/hp.bin", "rb").read()
    for i, b in enumerate(img):
        mem.w8(s["hp_touch_entry"] + i, b)
    for a in range(s["hp_touch_entry"] + len(img), s["end"]):
        mem.w8(a, 0)
    mem.w32(s["hp_prev_touch"], mem.r32(POOL_TOUCH))
    mem.w32(POOL_TOUCH, s["hp_touch_entry"])
    run = lambda n: m.cpu.run(limit=n)          # noqa: E731
    v = lambda n: mem.r32(s[n])                 # noqa: E731
    ok = True

    def touch(pts, step=6_000_000, after=150_000_000):
        for xy in pts:
            dev.panel_frame = panl.build_frame(touch=xy); run(step)
        dev.panel_frame = panl.build_frame(); run(after)

    def tap(x, y, hold=8_000_000, after=150_000_000):
        dev.panel_frame = panl.build_frame(touch=(x, y)); run(hold)
        dev.panel_frame = panl.build_frame(); run(after)

    STOCK_LBL = [bytes(mem.r8(a + i) for i in range(144))
                 for a in (0x083B9130, 0x083BA330, 0x083BA450)]
    STOCK_COLOURS = {mem.r16(0x083B9130 + 2 * k) for k in range(72)}
    run(20_000_000)
    tap(709, 37, after=30_000_000)              # PERFORM
    run(250_000_000)
    tap(576, ROW, hold=150_000_000)             # CALL (stock, not one of ours)

    def check(label, want=None, page=None):
        nonlocal ok
        p = mem.r32(POS) / 150.0
        good = True
        if want is not None:
            good &= -0.2 < p - want < 0.8
        if page is not None:
            good &= mem.r8(PP.HCPAGE) == page
        print(f"{label:34s} pos {p:6.2f}  page {mem.r8(PP.HCPAGE)}  taps {v('hp_taps')} "
              f"holds {v('hp_holds')} pages {v('hp_pages')}  {'ok' if good else 'WRONG'}", flush=True)
        ok &= good

    tap(PAD_X[0], ROW)
    check("tap pad 1 -> A (5 s)", 5, 0)
    p0 = mem.r32(POS) / 150.0
    touch([(330, ROW), (300, ROW), (270, ROW), (240, ROW), (210, ROW)])
    p1 = mem.r32(POS) / 150.0
    check("swipe left across pads -> page 2", page=1)
    print(f"   (no jump during the swipe: {p0:.2f} -> {p1:.2f})")
    ok &= p1 >= p0 - 0.05
    # cue marker labels: the three label pictures now read D E F
    from ghosttap_emu import fb_png
    LBL = (0x083B9130, 0x083BA330, 0x083BA450)

    def letters():
        out = []
        for a in LBL:
            bits = tuple(sum(1 << (7 - x) for x in range(8)
                             if mem.r16(a + 2 * (y * 8 + x)) != 0xF81F) for y in range(9))
            out.append(bits)
        return out
    PATS = {(0x78, 0x44, 0x42, 0x42, 0x42, 0x42, 0x42, 0x44, 0x78): "D",
            (0x7E, 0x40, 0x40, 0x40, 0x7C, 0x40, 0x40, 0x40, 0x7E): "E",
            (0x7E, 0x40, 0x40, 0x40, 0x7C, 0x40, 0x40, 0x40, 0x40): "F",
            (0x18, 0x18, 0x24, 0x24, 0x24, 0x7E, 0x42, 0x42, 0x42): "A",
            (0x7C, 0x42, 0x42, 0x42, 0x7C, 0x42, 0x42, 0x42, 0x7C): "B",
            (0x38, 0x44, 0x82, 0x80, 0x80, 0x80, 0x82, 0x44, 0x38): "C"}
    run(30_000_000)
    lab = "".join(PATS.get(b, "?") for b in letters())
    colour = {mem.r16(a + 2 * k) for a in LBL for k in range(72)}
    good = lab == "DEF" and colour == STOCK_COLOURS
    print(f"marker labels on page 2: {lab}  colours {sorted(hex(c) for c in colour)}  "
          f"{'ok' if good else 'WRONG'}")
    ok &= good
    fb_png(m, "screenshots/hotcue8/markers_page2.png")
    tap(PAD_X[0], ROW)
    check("tap pad 1 -> D (29 s)", 29, 1)
    tap(PAD_X[1], ROW, hold=200_000_000)
    check("hold pad 2 -> E (37 s)", 37, 1)
    # REC on pages D-F / G-H is the deck's own RECORD MODE (manual, Setting
    # Hot Cue: touch REC, the pads light red, then touch the pad at the point;
    # touch REC again to leave). 2026-11-18 deck report: "click REC and swipe
    # creates a D cue without clicking D" -- so a REC tap alone must store
    # NOTHING, and a slide from REC must page instead of pressing it.
    SLOTS = 0x0B6C4118
    arm = lambda: mem.r32(hs["hc_recarm"])      # noqa: E731

    def snap():                    # (state, ms) of the three live slots
        return [(mem.r16(SLOTS + k * 0x40 + 0x08),
                 mem.r32(SLOTS + k * 0x40 + 0x0C)) for k in range(3)]

    before, r0 = snap(), v("hp_recs")
    tap(500, ROW)                               # REC: record mode on
    good = arm() == 1 and snap() == before and v("hp_recs") == r0 + 1
    print(f"REC tap on page 2 -> record mode on, cues unchanged "
          f"{snap() == before}, recs +{v('hp_recs') - r0}  {'ok' if good else 'WRONG'}")
    ok &= good
    tap(500, ROW)                               # REC again: leave the mode
    good = arm() == 0 and snap() == before and v("hp_recs") == r0 + 2
    print(f"REC again -> record mode off, cues unchanged {snap() == before}  "
          f"{'ok' if good else 'WRONG'}")
    ok &= good
    # armed + pad press: THAT pad is stored at the playhead (RAM only)
    tap(500, ROW)                               # arm
    here, c0 = mem.r32(POS), mem.r32(hs["hc_recs"])
    tap(PAD_X[0], ROW)
    st, ms = snap()[0]
    rest = snap()[1:] == before[1:]
    good = (st not in (0, 0xFFFF) and abs(ms - here * 20 // 3) < 2500
            and mem.r32(hs["hc_recs"]) == c0 + 1 and arm() == 1 and rest)
    print(f"record mode + pad 1 -> D set at the playhead: state {st:#x} ms {ms} "
          f"(playhead {here * 20 // 3} ms) hc_recs +{mem.r32(hs['hc_recs']) - c0}, "
          f"E/F untouched {rest}  {'ok' if good else 'WRONG'}")
    ok &= good
    tap(500, ROW)                               # leave record mode
    # a swipe that STARTS on REC pages, and stores nothing
    r0, c0 = v("hp_recs"), mem.r32(hs["hc_recs"])
    touch([(500, ROW), (470, ROW), (440, ROW), (410, ROW)])
    good = (mem.r8(PP.HCPAGE) == 2 and arm() == 0 and v("hp_recs") == r0
            and mem.r32(hs["hc_recs"]) == c0)
    print(f"swipe starting on REC -> page index {mem.r8(PP.HCPAGE)}, no store "
          f"(recs +{v('hp_recs') - r0}, cues +{mem.r32(hs['hc_recs']) - c0})  "
          f"{'ok' if good else 'WRONG'}")
    ok &= good
    touch([(200, ROW), (230, ROW), (260, ROW), (290, ROW), (320, ROW)])
    check("swipe right -> page 2 again", page=1)
    # one REC press that the panel reports as three quick presses (deck
    # 2026-11-16: "it made 3 cues") must change the mode ONCE
    before, r0 = snap(), v("hp_recs")
    for _ in range(3):
        tap(500, ROW, hold=8_000_000, after=10_000_000)
    run(150_000_000)
    good = (arm() == 1 and snap() == before and v("hp_recs") == r0 + 1
            and v("hp_recbounce") >= 1)
    print(f"REC pressed 3x in a burst -> armed {arm()}, cues unchanged "
          f"{snap() == before}, recs +{v('hp_recs') - r0}, bounces ignored "
          f"{v('hp_recbounce')}  {'ok' if good else 'WRONG'}")
    ok &= good
    tap(500, ROW)                               # leave record mode again
    touch([(200, ROW), (230, ROW), (260, ROW), (290, ROW), (320, ROW)])
    check("swipe right -> page 1", page=0)
    run(30_000_000)
    back = [bytes(mem.r8(a + i) for i in range(144))
            for a in (0x083B9130, 0x083BA330, 0x083BA450)] == STOCK_LBL
    print(f"marker labels back to stock A B C byte for byte: {back}  "
          f"(rewrites {v('hp_lblw')}, refusals {v('hp_lblbad')})")
    ok &= back and v("hp_lblbad") == 0
    tap(PAD_X[0], ROW)
    # every pad was touched on page 2, so the return to page 1 must make it
    # reload (hcpad_test 2026-11-18: without that, A played D after a REC store)
    check("tap pad 1 -> A again", 5, 0)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
