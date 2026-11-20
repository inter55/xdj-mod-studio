#!/usr/bin/env python3
"""hotcue8_repaint_test.py -- EMULATOR: the A-C pads come back after paging.

Deck report 2026-11-18 (LingonberryMurky5767): "switching pages on the hot
cues causes the ABC cues to disappear". Page 2/3 are repainted by the hot
cue module; page 1 used to be left to the stock firmware, so on a deck that
does not redraw the pad row by itself the empty paged faces stayed on
screen after the page went back to A-C. The module now repaints the stock
A-C row on the way back (hc.c, hc_pagewas).

    .venv/bin/python tools/hotcue8_repaint_test.py [FIXTURE]

FIXTURE defaults to $TMPDIR/hc_s0/hc_rb_s0_loaded.pkl (tools/hotcue8_emu.py
HC_LAYOUT=rekordbox). Checks, with real touches:
  1. the A-C row of a fresh PERFORM screen (the baseline);
  2. page 2, page 3, page 1: the row after the round trip is PIXEL-IDENTICAL
     to the baseline (our repaint reproduces the stock row exactly, so the
     result is right whether or not the deck would have redrawn it);
  3. the return painted (hc_draws went up) and the paint is one-shot: it
     does not run again on later frames.
"""
import os
import sys

sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
from ghosttap_emu import fb_png          # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402
import hotcue8_test as T                 # noqa: E402
from PIL import Image                    # noqa: E402

BOX = (112, 228, 620, 300)               # the A B C / D E F / G H pad row
OUT = "screenshots/hotcue8"


def row_png(m, path):
    fb_png(m, path)
    return Image.open(path).convert("RGB").crop(BOX).tobytes()


def main():
    fix = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hc_s0", "hc_rb_s0_loaded.pkl")
    if not os.path.exists(fix):
        print(f"SKIP: {fix} missing -- build it first with "
              "`.venv/bin/python tools/hotcue8_emu.py` (HC_LAYOUT=rekordbox)")
        return 0
    os.makedirs(OUT, exist_ok=True)
    code, syms = T.build()
    m = Machine.load(fix)
    mem, dev = m.mem, m.mem.dev
    PP.install(mem)
    T.install_hc(mem, code, syms)
    mem.w8(MS.MSOFF, 0)

    def run(n):
        m.cpu.run(limit=n)

    def tap(x, y, hold=12_000_000, after=30_000_000):
        dev.panel_frame = panl.build_frame(touch=(x, y))
        run(hold)
        dev.panel_frame = panl.build_frame()
        run(after)

    ok = True

    def chk(label, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + label, flush=True)
        ok &= bool(cond)

    run(20_000_000)
    tap(709, 37)                              # PERFORM
    run(60_000_000)
    chk("PERFORM open", mem.r32(0x0BA371D4) == 7)
    chk("cues seen: 8", mem.r32(syms["hc_count"]) == 8)
    tab = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2)
    base = row_png(m, f"{OUT}/page1_repaint_base.png")
    PADST = 0x0B6C3C78
    st0 = [mem.r32(PADST + 4 * k) for k in range(3)]
    chk(f"page-1 pads start settled {st0}", all(s in (0, 1) for s in st0))

    tap(*tab)                                 # page 2 (D E F)
    chk("page 2", mem.r8(PP.HCPAGE) == 1)
    row_png(m, f"{OUT}/page2_repaint.png")
    tap(*tab)                                 # page 3 (G H -)
    chk("page 3", mem.r8(PP.HCPAGE) == 2)
    d0 = mem.r32(syms["hc_draws"])
    tap(*tab)                                 # page 1 (A B C)
    chk("page 1", mem.r8(PP.HCPAGE) == 0)
    d1 = mem.r32(syms["hc_draws"])
    chk(f"the return repainted the row (draws {d0} -> {d1})", d1 > d0)
    after = row_png(m, f"{OUT}/page1_repaint_back.png")
    diff = sum(1 for a, b in zip(base, after) if a != b)
    chk(f"A-C row is pixel-identical to a fresh one ({diff} px differ)", diff == 0)
    run(60_000_000)                           # ... and it is one-shot
    chk(f"no repaint on later frames (draws {mem.r32(syms['hc_draws'])})",
        mem.r32(syms["hc_draws"]) == d1)
    chk("the pads still hold A-C", mem.r16(0x0B6C4118 + 0x0C) == 5000 and
        mem.r16(0x0B6C4118 + 0x40 + 0x0C) == 13000)
    # Deck report 2026-11-18b: "after paging back to A-B-C all three pads do
    # the pause when pressed, then the next press is normal". State 3 makes the
    # first press load (re-cue) -- and unused pads never lost their cue, so
    # they must come back at the state they had (1), not 3.
    st1 = [mem.r32(PADST + 4 * k) for k in range(3)]
    chk(f"unused pads came back loaded, not 3 {st1}", st1 == st0)
    # A pad that WAS used while away holds the other page's cue, so it must
    # ask for a load (3); its neighbours still come back loaded.
    tap(*tab)                                 # page 2
    tap(*tab)                                 # page 3 (G H -, pad 3 empty)
    mem.w32(PADST + 4, 1)                     # pad 2 as if pressed+loaded there
    tap(*tab)                                 # back to page 1
    st2 = [mem.r32(PADST + 4 * k) for k in range(3)]
    chk(f"only the used pad reloads {st2}", st2 == [st0[0], 3, st0[2]])
    run(60_000_000)
    chk("still holds A-C after that round trip",
        mem.r16(0x0B6C4118 + 0x0C) == 5000 and
        mem.r16(0x0B6C4118 + 0x40 + 0x0C) == 13000)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
