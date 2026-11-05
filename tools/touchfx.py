#!/usr/bin/env python3
"""touchfx.py -- TOUCH FX panel on the PERFORM screen (SH side, C module
tools/touchfx/tfx.c, cave build/touchfx/tfx.bin) + its emulator test.

    touchfx.py plan          addresses / sizes
    touchfx.py emu [SNAP]    emulator test (default cache/emu_perform_track.pkl):
                             installs the PERFORM pads + this panel, then taps
                             the TOUCH FX button, drags the X/Y pad, picks
                             effects, DEPTH, FX ON, HOLD, leaves via a pads tab,
                             and checks the SH->DSP word + the screen.

The audio side is the DSP FX engine (tools/fx_dsp/fx.c, installed in place
of real-time key shift -- tools/fx_engine_apply.py). This panel only writes
the tagged word to 0x09D24DD8 (-> DSP mailbox BD8); see tfx.c.
"""
import os
import subprocess
import sys

sys.path[:0] = [".", "tools"]
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "build", "touchfx")
BIN = os.path.join(OUT, "tfx.bin")
SYMS = os.path.join(OUT, "tfx.syms")
TFX_BASE = 0x09844000
TFX_LIMIT = 0x09850000
HOOK_BLIT, HOOK_TOUCH = 0x08C92E20, 0x08D5278C
KEYWORD, NOPSITE, MTW = 0x09D24DD8, 0x08D36B3A, 0x0B6C4308
WORD_OFF = 0x00100000
EFFECTS = ("filter", "crush", "gate")
# screen geometry (must match tfx.c)
TAB = (16 + 46, 297 + 6)
PAD = (116, 70, 521, 309)
RX, RW, GB_W, GB_H, GB_DX, GB_DY, GB_Y0 = 530, 256, 125, 24, 131, 28, 86
DEP_Y, BTN_Y, BTN_H, BTN_W = 214, 236, 34, 125


def build():
    subprocess.run(["sh", os.path.join(HERE, "touchfx", "build.sh")], check=True,
                   stdout=subprocess.DEVNULL)


def syms():
    if not os.path.exists(SYMS):
        build()
    d = {}
    for line in open(SYMS):
        a, _, n = line.split()
        d[n.lstrip("_")] = int(a, 16)
    return d


def blob():
    if not os.path.exists(BIN):
        build()
    return open(BIN, "rb").read()


def word(eff, x, y, depth, on=1):
    """the tagged SH->DSP word (same layout as tools/fx_dsp/fx.c)"""
    return (0x80000000 | (on << 29) | ((eff & 7) << 26) | ((x & 0xFF) << 18) |
            ((y & 0xFF) << 10) | ((depth & 0xFF) << 2))


def install(mem, engine=True):
    """RAM install for the emulator: code + zeroed bss, chained hooks.
    engine=True also marks the DSP engine as present (the deck installer
    sets tfx_engine only after the DSP FX engine reported success)."""
    s, b = syms(), blob()
    end = s["end"]
    assert TFX_BASE + len(b) <= end <= TFX_LIMIT, hex(end)
    for i, x in enumerate(b):
        mem.w8(TFX_BASE + i, x)
    for a in range(TFX_BASE + len(b), end):
        mem.w8(a, 0)
    import perform_pads as PP
    lit = PP.pbo_lit()                  # behind the pads (draws after them)
    mem.w32(s["tfx_prev_flush"], mem.r32(lit))
    mem.w32(s["tfx_prev_touch"], mem.r32(HOOK_TOUCH))
    if engine:
        mem.w32(s["tfx_engine"], 1)
    mem.w32(lit, s["tfx_flush_entry"])
    mem.w32(HOOK_TOUCH, s["tfx_touch_entry"])
    return s


def plan():
    s, b = syms(), blob()
    print(f"code {TFX_BASE:#010x} {len(b)} B, bss to {s['end']:#010x} (limit {TFX_LIMIT:#x})")
    print(f"flush entry {s['tfx_flush_entry']:#010x}  touch entry {s['tfx_touch_entry']:#010x}")


# ----------------------------------------------------------------- emu -----
def emu():
    import numpy as np
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    import perform_pads as PP
    ck = sys.argv[2] if len(sys.argv) > 2 else "cache/emu_perform_track.pkl"
    m = Machine.load(ck)
    mem, c = m.mem, m.cpu
    PP.install(mem)
    s = install(mem)
    mem.w16(NOPSITE, 0x0009)                 # what the engine installer does
    ok = True
    os.makedirs("screenshots/touchfx", exist_ok=True)

    def chk(label, good):
        nonlocal ok
        if not good:
            print(f"CHECK FAIL: {label}")
        ok &= bool(good)

    def run(n, frame=None, keys=()):
        mem.dev.panel_frame = frame or panl.build_frame(keys=list(keys))
        r = c.run(limit=n)
        if "limit" not in str(r):
            print("RUN STOPPED:", r)

    def v(name):
        return mem.r32(s[name])

    def px(buf, x, y):
        return int.from_bytes(buf[(y * 800 + x) * 2:(y * 800 + x) * 2 + 2], "little")

    def log(tag):
        base, n = s["tfx_log"], v("tfx_logn")
        ev = [mem.r32(base + 4 * (i & 31)) for i in range(max(0, n - 12), n)]
        print(f"  log {tag}: " + " ".join(f"{'D' if e >> 31 else 'u'}({(e >> 16) & 0x7FFF},{e & 0xFFFF})" for e in ev))

    def released():
        n = v("tfx_logn")
        return n and not mem.r32(s["tfx_log"] + 4 * ((n - 1) & 31)) >> 31

    def last():
        n = v("tfx_logn")
        return mem.r32(s["tfx_log"] + 4 * ((n - 1) & 31)) if n else 0

    def press(xy, n=8):
        """hold xy until the hook has seen it n times (the emulated UI samples
        touch slowly and with a variable delay)"""
        want = 1 << 31 | (xy[0] & 0x7FFF) << 16 | xy[1]
        seen = 0
        for _ in range(200):
            run(250_000, panl.build_frame(touch=xy))
            if last() == want:
                seen += 1
                if seen >= n:
                    return True
        print("  press never arrived:", xy)
        return False

    def lift():
        for _ in range(200):
            run(250_000)
            if released():
                break
        run(500_000)

    def tap(xy, tag=None, n=4):
        press(xy, n)
        if tag:
            log(tag + " held")
        lift()
        if tag:
            log(tag + " after")

    def state(tag):
        print(f"[{tag}] mode={v('tfx_mode')} eff={v('tfx_eff')} x={v('tfx_x')} "
              f"y={v('tfx_y')} depth={v('tfx_depth')} on={v('tfx_on')} hold={v('tfx_hold')} "
              f"touching={v('tfx_touching')} active={v('tfx_active')} "
              f"word={mem.r32(KEYWORD):#010x} draws={v('tfx_draws')} full={v('tfx_full')} "
              f"pads mode={mem.r8(PP.PST + 2)}", flush=True)

    run(6_000_000)
    # MASTER TEMPO on (the engine is MT-only)
    run(5_000_000, keys=["MasterTempo"])
    run(30_000_000)
    chk("MT on", mem.r8(MTW) == 1)
    chk("PERFORM screen", mem.r32(PP.MODEW) == 7)
    buf = fb_png(m, "screenshots/touchfx/0_perform.png")
    # the TOUCH FX button under the tab stack
    global TAB                       # KEY SHIFT slot when that tab is hidden
    TAB = (16 + 46, 264 + 7) if mem.r8(PP.FEATB) & 1 else (16 + 46, 297 + 6)
    chk("tab drawn", px(buf, 20, TAB[1]) != 0 and v("tfx_draws") > 0)
    state("start")

    # 1. open the panel
    tap(TAB)
    state("open")
    chk("panel open", v("tfx_mode") == 1)
    chk("pads mode HOT CUE", mem.r8(PP.PST + 2) == 0)
    run(4_000_000)
    buf = fb_png(m, "screenshots/touchfx/1_open.png")
    pad_c = px(buf, 200, 200)
    chk("pad drawn", pad_c not in (0,) and v("tfx_full") >= 1)
    chk("word idle", mem.r32(KEYWORD) != word(0, 0, 0, 0) and v("tfx_active") == 0)

    # 2. drag on the pad (FILTER): word follows the finger
    x0, y0, x1, y1 = PAD
    press((150, 280))
    st1 = (v("tfx_x"), v("tfx_y"), mem.r32(KEYWORD))
    state("drag a")
    press((450, 120))
    st2 = (v("tfx_x"), v("tfx_y"), mem.r32(KEYWORD))
    state("drag b")
    d0 = v("tfx_draws")
    for _ in range(40):                      # hold until two more presents
        if v("tfx_draws") >= d0 + 2:
            break
        run(1_000_000, panl.build_frame(touch=(450, 120)))
    buf = fb_png(m, "screenshots/touchfx/2_drag_filter.png")
    chk("finger moved x up", st2[0] > st1[0] + 100)
    chk("finger moved y up", st2[1] > st1[1] + 80)
    chk("active while touching", v("tfx_active") == 1 and v("tfx_touching") == 1)
    chk("word = filter", st2[2] == word(0, st2[0], st2[1], v("tfx_depth")))
    # crosshair: a pixel on the vertical line through the finger is the line colour
    cx = x0 + 1 + v("tfx_x") * (x1 - x0 - 3) // 255
    chk("crosshair drawn", px(buf, cx, 200) != px(buf, cx + 6, 200))
    chk("stock strip suppressed", v("tfx_stripped") > 0)
    # 3. release -> off
    lift()
    state("release")
    chk("off after lift", v("tfx_active") == 0 and mem.r32(KEYWORD) == WORD_OFF)

    # 4. pick CRUSH, then GATE; an unimplemented one (ECHO) stays dimmed/inert
    def grid(i):
        return (RX + (i & 1) * GB_DX + GB_W // 2, GB_Y0 + (i >> 1) * GB_DY + GB_H // 2)
    tap(grid(1))
    chk("crush picked", v("tfx_eff") == 1)
    tap(grid(3))
    chk("ECHO inert", v("tfx_eff") == 1)
    tap(grid(2))
    chk("gate picked", v("tfx_eff") == 2)
    state("gate")
    # 5. DEPTH bar to ~half
    tap((RX + RW // 2, DEP_Y + 4), tag="depth")
    chk("depth ~half", 115 <= v("tfx_depth") <= 140)
    # 6. HOLD on, touch + lift -> stays active
    tap((RX + RW - BTN_W // 2, BTN_Y + BTN_H // 2), tag="hold")
    chk("hold on", v("tfx_hold") == 1)
    tap((250, 200))
    state("hold")
    chk("held after lift", v("tfx_active") == 1 and
        mem.r32(KEYWORD) == word(2, v("tfx_x"), v("tfx_y"), v("tfx_depth")))
    buf = fb_png(m, "screenshots/touchfx/3_gate_hold.png")
    # 7. FX ON off -> silent word
    tap((RX + BTN_W // 2, BTN_Y + BTN_H // 2))
    chk("fx off", v("tfx_on") == 0 and v("tfx_active") == 0 and mem.r32(KEYWORD) == WORD_OFF)
    tap((RX + BTN_W // 2, BTN_Y + BTN_H // 2), tag="fxon2")
    chk("fx on again (hold)", v("tfx_on") == 1 and v("tfx_active") == 1)
    # 8. MT off -> inert, word off
    run(5_000_000, keys=["MasterTempo"])
    run(30_000_000)
    state("mt off")
    chk("MT off -> off", mem.r8(MTW) != 1 and v("tfx_active") == 0 and mem.r32(KEYWORD) == WORD_OFF)
    buf = fb_png(m, "screenshots/touchfx/4_mt_off.png")
    run(5_000_000, keys=["MasterTempo"])
    run(30_000_000)
    chk("MT on -> back (hold)", v("tfx_active") == 1)
    # 9. a pads tab closes the panel and the pads take it (BEAT JUMP)
    tap((16 + 46, PP.TAB_Y[1] + 7))
    state("beat jump tab")
    chk("panel closed", v("tfx_mode") == 0)
    chk("pads took the tap", mem.r8(PP.PST + 2) == 1)
    chk("word off after close", v("tfx_active") == 0 and mem.r32(KEYWORD) == WORD_OFF)
    buf = fb_png(m, "screenshots/touchfx/5_closed_bj.png")
    tap((16 + 46, PP.TAB_Y[0] + 7))                  # back to HOT CUE
    chk("pads HOT CUE", mem.r8(PP.PST + 2) == 0)
    t0 = v("tfx_touches")
    # 11. reopen + close with the button itself
    tap(TAB)
    chk("reopen", v("tfx_mode") == 1)
    run(4_000_000)
    fb_png(m, "screenshots/touchfx/6_reopen.png")
    tap(TAB)
    chk("close with the button", v("tfx_mode") == 0)
    run(40_000_000)
    buf = fb_png(m, "screenshots/touchfx/8_closed_later.png")
    chk("stock back after close (REV button)", px(buf, 50, 100) != 0)
    # 12. FEATB bit 4 (TOUCH FX hidden): the button is no longer drawn
    mem.w8(PP.FEATB, mem.r8(PP.FEATB) | 4)
    d0 = v("tfx_draws")
    run(20_000_000)
    chk("hidden: not drawn", v("tfx_draws") == d0)
    # 13. hidden + MODS OFF: a tap on the spot goes to the firmware (in the
    #     emulator a stock hot-cue-row press then stalls the panel link --
    #     known emulator gap, CLAUDE.md sec. 48 -- so this stays last)
    mem.w8(PP.MSOFF, 1)
    press(TAB, 2)
    chk("MODS OFF + hidden: no open", v("tfx_mode") == 0)
    mem.w8(PP.MSOFF, 0)
    mem.w8(PP.FEATB, mem.r8(PP.FEATB) & ~4 & 0xFF)
    print(f"touches seen {v('tfx_touches') - t0}, full redraws {v('tfx_full')}, flushes drawn {v('tfx_draws')}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "plan":
        plan()
    elif cmd == "emu":
        sys.exit(0 if emu() else 1)
