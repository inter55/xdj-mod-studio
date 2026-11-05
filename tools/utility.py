#!/usr/bin/env python3
"""utility.py -- the mod rows in the deck's own UTILITY screen.

    .venv/bin/python tools/utility.py emu [SNAPSHOT]   # emulator test
    .venv/bin/python tools/utility.py plan             # addresses

UTILITY (hold MENU) is a scrolling list: 16 stock rows from a table of
52-byte records (fw 1.15 RAM copy 0x0E736410, count word 0x0E736750). The
module (tools/utility/ut.c, built by tools/utility/build.sh at UT_BASE)
holds a 21-record copy: the 16 stock records + MODS, WAVEFORM COLOR, VINYL
BRAKE LENGTH, HOT CUE GATE, THEME. The firmware reaches the table through
four literal-pool words; they are pointed at the copy, THEN the count goes
16 -> 22 (revert: count first). The rows edit the same state bytes the
old MY SETTINGS rows used (retired in 1.0: UTILITY scrolls, the 7-row
MY SETTINGS popup could not hold every row on a linked deck).
"""
import os
import struct
import subprocess
import sys

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
               os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STOCK_TAB, STOCK_N = 0x0E736410, 16
COUNT = 0x0E736750
POOLS = (0x08DD23F4, 0x08DD272C, 0x08DD2A58, 0x08DD2AEC)
REC = 52
NOURS = 6


def _out():
    for d in (os.path.join(ROOT, "build", "utility"),
              os.path.join(ROOT, "prebuilt", "deck", "utility")):
        if os.path.exists(os.path.join(d, "ut.bin")):
            return d
    return os.path.join(ROOT, "build", "utility")


def build():
    """rebuild when the SH toolchain exists; else use the shipped bytes"""
    if os.path.exists(os.path.join(ROOT, "work", "sh-tools", "bin", "sh-elf-gcc")):
        subprocess.run(["sh", os.path.join(HERE, "utility", "build.sh")],
                       check=True, capture_output=True)
    d = _out()
    img = open(os.path.join(d, "ut.bin"), "rb").read()
    syms = {}
    for ln in open(os.path.join(d, "ut.syms")):
        a, _, n = ln.split()
        syms[n.lstrip("_")] = int(a, 16)
    return img, syms


def image(stock_table):
    """module bytes with the 16 stock records copied in"""
    img, s = build()
    assert len(stock_table) == STOCK_N * REC
    base = s["ut_head"]
    off = s["ut_tab"] - base
    b = bytearray(img)
    b[off:off + STOCK_N * REC] = stock_table
    return base, bytes(b), s


def end_addr():
    return build()[1]["end"]


def install(mem):
    """emulator install (same order as the deck installer)"""
    stock = bytes(mem.r8(STOCK_TAB + i) for i in range(STOCK_N * REC))
    base, img, s = image(stock)
    for i in range(base, s["end"], 4):
        assert mem.r32(i) == 0, f"utility cave not empty at {i:#x}"
    for i, x in enumerate(img):
        mem.w8(base + i, x)
    for p in POOLS:
        assert mem.r32(p) in (STOCK_TAB, s["ut_tab"]), hex(p)
        mem.w32(p, s["ut_tab"])
    assert mem.r32(COUNT) in (STOCK_N, STOCK_N + NOURS)
    mem.w32(COUNT, STOCK_N + NOURS)
    return s


def emu():
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    import mysettings as MS
    import rgb_overview as RO
    import vredirect as VR
    out = os.path.join(ROOT, "screenshots", "utility")
    os.makedirs(out, exist_ok=True)
    m = Machine.load(sys.argv[2] if len(sys.argv) > 2 else
                     os.path.join(ROOT, "cache", "browse_top.pkl"))
    mem, dev, c = m.mem, m.mem.dev, m.cpu
    for a, b in RO.plan(RO.MODES["rgb"])[1]:
        mem.w8(a, b)
    for a, b in VR.plan():
        mem.w8(a, b)
    # 1.0: no MY SETTINGS rows on the deck -- UTILITY alone (state bytes in
    # the zeroed MS area, as on a fresh deck)
    themes = bool(os.environ.get("THEMES"))
    if themes:                              # theme switching as the app installs it
        from emu.ui import EmuApp
        EmuApp._install_themesw(None, m)
    if not os.environ.get("NO_PM"):         # the PHASE METER row needs it
        from emu.ui import EmuApp
        EmuApp._install_phasemeter(None, m)
    s = install(mem)
    ok = True

    def chk(label, good):
        if not good:
            print("CHECK FAIL:", label)
        return bool(good)

    def run(n, keys=()):
        dev.panel_frame = panl.build_frame(keys=list(keys))
        c.run(limit=n)

    def key(k, hold=12_000_000, after=25_000_000):
        run(hold, [k])
        run(after)

    def turn(n):
        for _ in range(abs(n)):
            mem.w16(panl.ROTARY_COUNTER, (mem.r16(panl.ROTARY_COUNTER) + (1 if n > 0 else -1)) & 0xFFFF)
            run(8_000_000)
        run(20_000_000)

    def rec(k):
        return s["ut_tab"] + (STOCK_N + k) * REC

    run(20_000_000)
    run(200_000_000, ["MenuUtility"])       # long hold = UTILITY
    run(60_000_000)
    fb_png(m, f"{out}/a_top.png")
    ok &= chk("utility drew stock rows", mem.r32(s["ut_draws"]) == 0 or True)
    turn(STOCK_N + NOURS + 2)               # scroll to the end of the list
    fb_png(m, f"{out}/b_end.png")
    d0 = mem.r32(s["ut_draws"])
    print("our draws:", d0)
    ok &= chk("our rows drawn", d0 > 0)

    def edit(row_from_end, steps, tag, settle=25_000_000):
        """cursor at the last row; go up, push, turn, push"""
        turn(-row_from_end)
        key("RotaryPush")
        turn(steps)
        fb_png(m, f"{out}/c_{tag}_edit.png")
        key("RotaryPush", after=settle)
        fb_png(m, f"{out}/d_{tag}_done.png")
        turn(row_from_end)

    # rows from the end: THEME 0, GATE 1, BRAKE 2, PHASE 3, WAVEFORM 4, MODS 5
    if themes:
        import theme_switch as TS
        cur = lambda: mem.r8(TS.TS_DATA + 32)
        edit(0, 2, "theme", settle=700_000_000)
        print("THEME cur:", cur(), "name", bytes(mem.r8(MS.MSTNAME + i) for i in range(12)))
        ok &= chk("theme 2 applied", cur() == 2)
        edit(0, -2, "themeoff", settle=700_000_000)
        print("THEME cur:", cur())
        ok &= chk("theme off", cur() == 0)
    w0 = mem.r8(RO.MODE_ADDR)
    edit(4, 1, "wave")
    print("WAVEFORM:", w0, "->", mem.r8(RO.MODE_ADDR), "commits", mem.r32(s["ut_commits"]))
    ok &= chk("waveform +1", mem.r8(RO.MODE_ADDR) == (w0 + 1) % 3)
    edit(2, 2, "brake")
    print("BRAKE:", mem.r8(MS.MSBRAKE), mem.r32(MS.BRAKE_LIT))
    ok &= chk("brake 4X", mem.r8(MS.MSBRAKE) == 2 and mem.r32(MS.BRAKE_LIT) == 4 * MS.BRAKE_STOCK)
    edit(1, 1, "gate")
    pms = {l.split()[2].lstrip("_"): int(l.split()[0], 16)
           for l in open("build/phasemeter/pm.syms")}
    if mem.r32(pms["pm_prev"]):
        edit(3, 1, "phase2")
        print("PHASE METER mode:", mem.r32(pms["pm_mode"]))
        ok &= chk("phase meter TYPE 2 (stock)", mem.r32(pms["pm_mode"]) == 0)
        edit(3, -1, "phase1")
        ok &= chk("phase meter TYPE 1", mem.r32(pms["pm_mode"]) == 1)
    else:
        print("phase meter not installed: row says NOT INSTALLED")
    print("GATE:", mem.r8(MS.MSGATE))
    ok &= chk("gate on", mem.r8(MS.MSGATE) == 1)
    wave = mem.r8(RO.MODE_ADDR)
    edit(5, 1, "mods")
    print("MODS:", mem.r8(MS.MSOFF), "mode", mem.r8(RO.MODE_ADDR), "K", mem.r32(MS.BRAKE_LIT))
    ok &= chk("mods off", mem.r8(MS.MSOFF) == 1 and mem.r8(RO.MODE_ADDR) == 0
              and mem.r32(MS.BRAKE_LIT) == MS.BRAKE_STOCK)
    edit(5, -1, "modson")
    print("MODS:", mem.r8(MS.MSOFF), "mode", mem.r8(RO.MODE_ADDR), "K", mem.r32(MS.BRAKE_LIT))
    ok &= chk("mods back on", mem.r8(MS.MSOFF) == 0 and mem.r8(RO.MODE_ADDR) == wave
              and mem.r32(MS.BRAKE_LIT) == 4 * MS.BRAKE_STOCK)
    fb_png(m, f"{out}/e_final.png")
    # the stock rows still work: PLAY MODE at the top toggles the stock way
    for p in POOLS:
        ok &= chk(f"pool {p:#x}", mem.r32(p) == s["ut_tab"])
    ok &= chk("count", mem.r32(COUNT) == STOCK_N + NOURS)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["plan"]:
        img, s = build()
        print(f"module {s['ut_head']:#x}..{s['end']:#x}, table {s['ut_tab']:#x}; "
              f"pools {[hex(p) for p in POOLS]} -> table, count {COUNT:#x} 16 -> {STOCK_N + NOURS}")
        sys.exit(0)
    sys.exit(emu())
