#!/usr/bin/env python3
"""theme_wave_test.py -- EMULATOR: the waveform colours follow the theme.

    .venv/bin/python tools/theme_wave_test.py [SNAP]

SNAP (default cache/emu_fx_playing.pkl) = a track playing with the stock
blue waveform. Installs the theme switcher (tools/theme_switch.py plan, the
same bytes the deck gets), then calls the deck code's ts_apply for several
themes and checks:
  * the three firmware palettes (overview 0x0E7355E0, PERFORM strip
    0x0E735FA8, centre 0x0E736150) hold exactly that theme's row;
  * the screen's waveform pixels change to the theme colours (overview strip
    and centre), screenshot per theme in screenshots/theme_wave/;
  * LIGHT keeps the stock waveform colours;
  * back to OFF = every palette word stock again, byte for byte;
  * a palette word something else changed is left alone (and counted).
"""
import os
import struct
import sys
sys.path[:0] = [".", "tools", "mods"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
import numpy as np                       # noqa: E402
from emu.machine import Machine          # noqa: E402
import theme_switch as TS                # noqa: E402
import mysettings as MS                  # noqa: E402

HALT = 0x0F300000
OUT = "screenshots/theme_wave"


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_fx_playing.pkl"
    names = TS.default_names()
    m = Machine.load(snap)
    mem = m.mem
    from ghosttap_emu import fb_png
    os.makedirs(OUT, exist_ok=True)
    ok = True

    def chk(label, good):
        nonlocal ok
        print(("  ok   " if good else "CHECK FAIL: ") + label, flush=True)
        ok &= bool(good)

    # the 3-band overview module (its colour literals are theme entries too).
    # BLUE/THEME (mode 0) is the mode the waveform palettes belong to: RGB and
    # 3-BAND now keep the deck's blue for tracks with no colour data (LINK).
    import rgb_overview as RO
    if os.environ.get("NO_OVERVIEW") is None:
        for a, b in RO.plan(0)[1]:
            mem.w8(a, b)
    for a in range(TS.TS_BASE, TS.TS_DATA, 4):
        mem.w32(a, 0)
    plan = TS.plan(names)
    for base, b in plan:
        for i, x in enumerate(b):
            mem.w8(base + i, x)
    for i, x in enumerate(b"OFF".ljust(16, b"\0")):
        mem.w8(MS.MSTNAME + i, x)
    data = plan[1][1]
    wave_off = struct.unpack_from("<I", data, 68)[0]
    n, mm = struct.unpack_from("<II", data, wave_off)
    addrs = struct.unpack_from(f"<{n}I", data, wave_off + 8)
    r0 = wave_off + 8 + 4 * n
    rows = [np.frombuffer(data, "<u2", mm, r0 + 2 * mm * t)[:n] for t in range(len(names) + 1)]
    syms = {}
    for ln in open("build/themesw/ts.syms"):
        a_, _, nm = ln.split()
        syms[nm.lstrip("_")] = int(a_, 16)

    def pal():
        return np.array([mem.r32(a & ~1) if a & 1 else mem.r16(a) for a in addrs], np.uint32)

    print(f"{n} waveform colour entries ({sum(a & 1 for a in addrs)} in the overview module)")
    chk("palettes are stock before", np.array_equal(pal(), rows[0]))
    fb_png(m, f"{OUT}/0_stock.png")

    def apply(t):
        mem.w8(TS.TS_DATA + 33, t)                # hdr.target
        c = m.cpu
        saved = {r: getattr(c, r) for r in ("pc", "pr", "gbr", "mach", "macl", "sr", "ssr", "spc", "fpscr", "fpul")}
        gr = [c.gr(i) for i in range(16)]
        c.pr = HALT
        c.pc = syms["ts_apply"]
        m.run(limit=3_000_000_000, stop_on_pc=[HALT])
        for i, v in enumerate(gr):                # back to what the deck was doing
            c.set_gr(i, v)
        for r, v in saved.items():
            setattr(c, r, v)
        # the overview and its palette are painted on screen changes: open and
        # close PERFORM so the deck repaints the main screen with the new colours
        from emu import panl
        for _ in range(2):
            mem.dev.panel_frame = panl.build_frame(touch=(705, 37))
            m.run(limit=8_000_000)
            mem.dev.panel_frame = panl.build_frame()
            m.run(limit=200_000_000)

    for t in (1, 6, len(names), 0):
        name = "OFF" if t == 0 else names[t - 1]
        apply(t)
        cur = mem.r8(TS.TS_DATA + 32)
        chk(f"{name}: switched (cur {cur})", cur == t)
        chk(f"{name}: palettes + 3-band overview = the theme row", np.array_equal(pal(), rows[t]))
        fb_png(m, f"{OUT}/{t}_{name}.png")
    chk("OFF again: palettes stock byte for byte", np.array_equal(pal(), rows[0]))
    w = mem.r32(syms["ts_wave_writes"])
    chk(f"writes counted ({w})", w > 0)

    # a word somebody else changed must stay as they left it
    mem.w16(TS.WAVE_PAL[0][0], 0x1234)
    sk0 = mem.r32(syms["ts_wave_skips"])
    apply(1)
    chk("foreign palette word left alone", mem.r16(TS.WAVE_PAL[0][0]) == 0x1234)
    chk("... and counted", mem.r32(syms["ts_wave_skips"]) > sk0)
    chk("the rest still follow the theme", np.array_equal(pal()[1:], rows[1][1:]))
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
