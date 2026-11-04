#!/usr/bin/env python3
"""theme_switch_emu_direct.py [themes..] -- on-deck theme switching in the
emulator WITHOUT the menu: the shipped main-screen snapshot (firmware home
mainscreen.pkl.gz, no stick needed), the switcher installed the way
theme_switch_apply.py does it (code, tables, lit button pictures moved to
their private colours while they read stock, entry word last), then the
REAL deck code ts_push() called on the emulated SH for every [THEME] push.

After every push the artwork in RAM must equal the Python model exactly
(chunks the firmware changed since boot: untouched); after the full cycle
RAM must be byte-identical to before the first push. Each step also taps
the lit-able touch buttons and saves the player screen with them lit:
screenshots/themesw/direct_<n>_<theme>.png.

  theme_switch_emu_direct.py 'crimson-red@#20d060' hot-pink 'ice-cyan@#ffe040'
(NAME@#rrggbb = theme NAME with lit buttons in that colour)
2026-11-04: tools/theme_switch.py emu needs cache/emu_mainscreen.pkl (stick
+ BROWSE open), which a fresh checkout does not have.
"""
import os
import sys

import numpy as np

sys.path[:0] = [".", "tools", "mods"]
import theme_switch as TS
import mysettings as MS
from emu.machine import Machine
from emu import panl as P, fwhome
from emu.fwhome import _fb

# the call returns into a parked "bra ." loop in the switcher's unused bss
# tail (between the work arrays and the tables): no per-instruction hook,
# so the fast C core runs the deck code (a hook drops to the Python core)
PARK = 0x09778F00
TAPS = {"vinyl": (50, 370), "slip": (50, 430), "sync": (675, 368), "master": (748, 368)}


def call(m, target, limit=4_000_000_000, step=20_000_000):
    c, mem = m.cpu, m.mem
    mem.w16(PARK, 0xAFFE)                  # bra PARK
    mem.w16(PARK + 2, 0x0009)              # nop (delay slot)
    st = {k: getattr(c, k) for k in ("pc", "pr", "sr", "gbr", "mach", "macl")}
    gr = [c.gr(i) for i in range(16)]      # the firmware was stopped mid-code
    c.sr |= 0x10000000                     # BL: no interrupts inside the call
    c.pr = PARK
    c.pc = target
    done, n = False, 0
    while n < limit:
        c.run(limit=step)
        n += step
        if PARK <= c.pc <= PARK + 4:
            done = True
            break
    for k, v in st.items():
        setattr(c, k, v)
    for i, v in enumerate(gr):
        c.set_gr(i, v)
    mem.w32(PARK, 0)
    return done


def rgb(px):
    v = px.astype(np.uint32)
    return np.dstack([((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63,
                      (v & 31) * 255 // 31]).astype(np.uint8)


def main():
    names = TS.theme_names(sys.argv[1:]) or TS.default_names()
    out = "screenshots/themesw"
    os.makedirs(out, exist_ok=True)
    m = Machine.load(fwhome.path("mainscreen.pkl.gz"))
    mem, dev, c = m.mem, m.mem.dev, m.cpu
    dev.panel_frame = P.build_frame()
    c.run(limit=20_000_000)

    def tap(xy):
        dev.panel_frame = P.build_frame(touch=xy)
        c.run(limit=12_000_000)
        dev.panel_frame = P.build_frame()
        c.run(limit=40_000_000)

    (cb, code), (db, data) = TS.plan(names)
    writes = TS.lit_writes(names)
    syms = TS.build_code()[1]
    # install: bss zero, code minus entry word, tables, lit pictures, entry
    for a in range(cb, db, 4):
        mem.w32(a, 0)
    for i, x in enumerate(code[4:]):
        mem.w8(cb + 4 + i, x)
    for i, x in enumerate(data):
        mem.w8(db + i, x)
    nlit = 0
    for a, st, pv in writes:
        if bytes(mem.r8(a + i) for i in range(len(st))) == st:
            for i, x in enumerate(pv):
                mem.w8(a + i, x)
            nlit += 1
    print(f"lit button pictures moved to private colours: {nlit}/{len(writes)}")
    for i, x in enumerate(b"OFF".ljust(16, b"\0")):
        mem.w8(MS.MSTNAME + i, x)
    mem.w32(cb, int.from_bytes(code[:4], "little"))

    wmod, regs, S, Ds, _, _ = TS.tables_full(names)
    lo = min(a for a, _ in regs)
    hi = max(a + 2 * n for a, n in regs)
    mask = np.zeros((hi - lo) // 2, bool)
    for a, n in regs:
        mask[(a - lo) // 2:(a - lo) // 2 + n] = True

    def ram_words():
        w = np.frombuffer(bytes(mem.ram0[lo - TS.IMG_BASE:hi - TS.IMG_BASE]), "<u2").copy()
        w[~mask] = 0
        return w

    def model(img, cur, tgt):
        lut = np.arange(65536, dtype=np.uint16)
        lut[S if cur == 0 else Ds[cur - 1]] = S if tgt == 0 else Ds[tgt - 1]
        o = img.copy()
        for a, n in regs:
            s = (a - lo) // 2
            o[s:s + n] = lut[img[s:s + n]]
        return o

    sw = wmod[(lo - TS.IMG_BASE) // 2:(hi - TS.IMG_BASE) // 2].copy()
    sw[~mask] = 0
    stock = ram_words()
    frozen = np.zeros(len(stock), bool)        # chunks the firmware changed
    for a, n in regs:
        k0 = (a - lo) // 2
        for k in range(0, n, TS.CHUNK):
            sl = slice(k0 + k, k0 + min(n, k + TS.CHUNK))
            if (stock[sl] != sw[sl]).any():
                frozen[sl] = True
    print(f"chunks differing from stock+private (deck must skip): "
          f"{int(frozen.sum()) // TS.CHUNK}+")

    ok, cur = True, 0
    drift = np.zeros(len(stock), bool)        # firmware writes between pushes
    prev = stock
    for step in range(len(names) + 1):
        tgt = (cur + 1) % (len(names) + 1)
        img = ram_words()
        drift |= img != prev
        if not call(m, syms["_ts_push"]):
            print("  ts_push did not return")
            ok = False
            break
        got = ram_words()
        exp = model(img, cur, tgt)
        exp[frozen] = img[frozen]
        bad = int((got != exp).sum())
        nm = names[tgt - 1] if tgt else "OFF"
        print(f"push {step + 1}: {cur} -> {tgt} ({nm}): cur={mem.r8(db + 32)} "
              f"px changed={int((got != img).sum()):,} mismatch vs model={bad}")
        ok &= bad == 0 and mem.r8(db + 32) == tgt
        cur, prev = tgt, got
        # light every lit-able button, screenshot, put them back
        for xy in TAPS.values():
            tap(xy)
        fb = _fb(m).copy()
        for xy in TAPS.values():
            tap(xy)
        from PIL import Image
        Image.fromarray(rgb(fb)).save(f"{out}/direct_{step + 1}_{nm}.png")
    end = ram_words()
    drift |= end != prev
    back = bool(np.array_equal(end[~drift], stock[~drift]))
    idx = np.nonzero(drift)[0]
    print(f"firmware wrote {len(idx)} artwork px between pushes"
          + (f" ({hex(lo + 2 * int(idx[0]))}..{hex(lo + 2 * int(idx[-1]))})" if len(idx) else ""))
    print("back to stock (+ private lit colours) byte-exact elsewhere:", back)
    ok &= back
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
