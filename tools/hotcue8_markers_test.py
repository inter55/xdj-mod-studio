#!/usr/bin/env python3
"""hotcue8_markers_test.py -- EMULATOR: all eight hot cue markers on the
overview bar, each letter in its own colour, on every page.

    .venv/bin/python tools/hotcue8_markers_test.py [FIXTURE]

FIXTURE = 8-cue track (HC_LAYOUT=rekordbox tools/hotcue8_emu.py; default
$TMPDIR/hc_rb_s0_loaded.pkl). Installs pads + 8 hot cues + the swipe module
like the deck, then checks the composed screen at every cue's marker spot
(triangle + 8x9 letter, tools/hotcue8/hc.c markers()):
  - BROWSE (the fixture's start screen): no markers drawn by us;
  - PERFORM page 1/2/3: every cue exactly once with its own letter (the
    deck's slot markers, which say A-C, are replaced), coloured per UTILITY
    HOT CUE COLORS (MSHCCOL): 8 COLORS / A-C STOCK / ALL STOCK;
  - the main screen (PERFORM closed on page 2): the same eight;
  - MODS OFF: back to the deck's three markers only.
Screenshots: screenshots/hotcue8/markers_p{1,2,3}.png (+ _bar crops).
"""
import os
import sys
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
import numpy as np                       # noqa: E402
from PIL import Image                    # noqa: E402
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
from ghosttap_emu import fb_png          # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402
import hotcue8_test as T                 # noqa: E402

FIX = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "hc_rb_s0_loaded.pkl")
OUT = "screenshots/hotcue8"
POOL_TOUCH = 0x08D5278C
TIMES = [5000 + 8000 * i for i in range(8)]
PAT = [(0x18, 0x18, 0x24, 0x24, 0x24, 0x7E, 0x42, 0x42, 0x42),
       (0x7C, 0x42, 0x42, 0x42, 0x7C, 0x42, 0x42, 0x42, 0x7C),
       (0x38, 0x44, 0x82, 0x80, 0x80, 0x80, 0x82, 0x44, 0x38),
       (0x78, 0x44, 0x42, 0x42, 0x42, 0x42, 0x42, 0x44, 0x78),
       (0x7E, 0x40, 0x40, 0x40, 0x7C, 0x40, 0x40, 0x40, 0x7E),
       (0x7E, 0x40, 0x40, 0x40, 0x7C, 0x40, 0x40, 0x40, 0x40),
       (0x38, 0x44, 0x82, 0x80, 0x80, 0x8E, 0x82, 0x46, 0x3A),
       (0x42, 0x42, 0x42, 0x42, 0x7E, 0x42, 0x42, 0x42, 0x42)]
COL8 = [0x07E0, 0xF8C3, 0x4C9F, 0xFC60, 0x07FF, 0xF8B6, 0xFFE0, 0xA41F]
MODES = {0: "8 COLORS", 1: "A-C STOCK", 2: "ALL STOCK"}


def syms(path):
    return {l.split()[2].lstrip("_"): int(l.split()[0], 16) for l in open(path)}


def screen(m):
    reg = m.mem.dev.reg
    base = sum(reg.get(0xFE940430 + i, 0) << (8 * i) for i in range(4))
    idx = 1 if (base & 0x1FFFFFFF) == 0x0E83B800 else 0
    buf = bytes(m.mem.fb[idx])
    if not any(buf[::97]):
        buf = bytes(m.mem.fb[1 - idx])
    return np.frombuffer(buf, dtype="<u2").reshape(480, 800)


def main():
    if not os.path.exists(FIX):
        print(f"SKIP: {FIX} missing (HC_LAYOUT=rekordbox tools/hotcue8_emu.py)")
        return 0
    os.makedirs(OUT, exist_ok=True)
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
    stock_lbl = [bytes(mem.r8(a + i) for i in range(144))
                 for a in (0x083B9130, 0x083BA330, 0x083BA450)]
    run = lambda n: m.cpu.run(limit=n)          # noqa: E731
    green = mem.r16(0x083BA610)
    length = mem.r32(0x0C0E6D1C)
    cols = [(ms * 3 // 20) * 500 // length for ms in TIMES]
    ok = True

    def chk(label, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + label, flush=True)
        ok &= bool(cond)

    def tap(x, y, hold=12_000_000, after=40_000_000):
        dev.panel_frame = panl.build_frame(touch=(x, y)); run(hold)
        dev.panel_frame = panl.build_frame(); run(after)

    def marker(fb, n):
        """'ok' if cue n's triangle + letter sit at its spot in one colour,
        else a short reason; returns (verdict, colour)"""
        c = cols[n - 1]
        x0, y0 = 108 + c, 399
        on = [fb[y0 + y, x0 + 15 + x] for y in range(9) for x in range(8)
              if (PAT[n - 1][y] >> (7 - x)) & 1]
        col = on[0]
        if any(v != col for v in on):
            return "mixed colours", col
        off = [fb[y0 + y, x0 + 15 + x] for y in range(8) for x in range(8)
               if not (PAT[n - 1][y] >> (7 - x)) & 1]
        if any(v == col for v in off):
            return "another letter under it", col
        tri = [fb[y0 + 4 + r, x0 + 4 + x] for r in range(5) for x in range(r, 9 - r)]
        if any(v != col for v in tri):
            return "no triangle", col
        return "ok", col

    def page_check(name, page, mode=0):
        mem.w8(MS.MSHCCOL, mode)
        run(40_000_000)
        fb = screen(m)
        fb_png(m, f"{OUT}/markers_{name}.png")
        Image.open(f"{OUT}/markers_{name}.png").crop((100, 392, 640, 460)).resize(
            (1620, 204), Image.NEAREST).save(f"{OUT}/markers_{name}_bar.png")
        res = []
        for n in range(1, 9):
            v, col = marker(fb, n)
            want = green if mode == 2 or (mode == 1 and n <= 3) else COL8[n - 1]
            res.append(v == "ok" and col == want)
            if not res[-1]:
                print(f"      {'ABCDEFGH'[n - 1]}: {v}, colour {col:#06x} want {want:#06x}")
        chk(f"{name}: page {mem.r8(PP.HCPAGE) + 1}, {MODES[mode]}: all eight markers "
            f"A-H, right letters and colours ({sum(res)}/8)",
            mem.r8(PP.HCPAGE) == page and all(res))

    run(20_000_000)
    d0 = mem.r32(hs["hc_mkdraws"])
    run(40_000_000)
    chk(f"BROWSE (mode {mem.r32(0x0BA371D4)}): no markers drawn by us",
        mem.r32(0x0BA371D4) not in (1, 7) and mem.r32(hs["hc_mkdraws"]) == d0)
    tap(709, 37)                                  # PERFORM
    chk("PERFORM open", mem.r32(0x0BA371D4) == 7)
    page_check("p1", 0)
    page_check("p1_acstock", 0, 1)
    page_check("p1_allstock", 0, 2)
    tab = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2)
    tap(*tab)
    page_check("p2", 1)
    page_check("p2_acstock", 1, 1)
    page_check("p2_allstock", 1, 2)
    tap(*tab)
    page_check("p3", 2)
    tap(*tab)
    page_check("p1_again", 0)
    tap(*tab)                                     # page 2, then leave PERFORM
    tap(709, 37)
    if mem.r32(0x0BA371D4) == 2:                  # back in BROWSE: close it
        tap(165, 37)
    chk(f"main screen (mode {mem.r32(0x0BA371D4)})", mem.r32(0x0BA371D4) == 1)
    page_check("main_p2", 1)
    chk("stock label pictures never rewritten (shared artwork)",
        [bytes(mem.r8(a + i) for i in range(144))
         for a in (0x083B9130, 0x083BA330, 0x083BA450)] == stock_lbl)
    mem.w8(MS.MSOFF, 1)                           # MODS OFF: deck's own only
    run(60_000_000)
    fb = screen(m)
    v = [marker(fb, n) for n in range(1, 9)]
    chk("MODS OFF: only the deck's A-C markers",
        all(x[0] == "ok" for x in v[:3]) and all(x[0] != "ok" for x in v[3:]))
    mem.w8(MS.MSOFF, 0)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
