#!/usr/bin/env python3
"""cw_unit_test.py -- the centre colour module on synthetic PWV5 data.

Runs the REAL built module (tools/centre/cw.c -> cw.bin) in the sh4box
sandbox with a hand-made PWV5 section, so the header maths can be checked
without a track load. Covers the two 2026-10-27 fixes:

  1. entry count > 65535 (BE u32 @+16; the old code read 16 bits and the
     centre fell back to stock blue a while into any track past 7 min 17 s
     -- the "RGB turns back to blue, CUE brings it back" report)
  2. a rate byte (@+21) other than 150 scales the painter's 150/s index
     (a denser export would draw the colours off the beat grid)

Run: .venv/bin/python tools/centre/cw_unit_test.py
"""
import os
import struct
import subprocess
import sys

sys.path[:0] = [".", "tools"]
from sh4box import run_code                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TAG_PWV5 = struct.unpack("<I", b"PWV5")[0]
TAG_PWV7 = struct.unpack("<I", b"PWV7")[0]
# test addresses inside the box's RAM (0x0B000000 + 64 MB); the module is
# rebuilt for them, so nothing outside the sandbox is touched
CW_BASE, SLOT5 = 0x0B200000, 0x0B300000
MODE_ADDR, SNAP_HASH = 0x0B410000, 0x0B410100
TS_ADDR = 0x0B440000
N = 100000                                       # > 65535 entries
HDR = 24
C_CORE, C_MIDC, C_OUTER = 0xEF18, 0xE4A5, 0x1B1A


def build():
    out = os.path.join(ROOT, "build/centre_test")
    env = dict(os.environ, CW_BASE=hex(CW_BASE), CW_OUT=out,
               MODE_ADDR=hex(MODE_ADDR), SLOT5=hex(SLOT5),
               SNAP_HASH=hex(SNAP_HASH), CTX_TAB="0x0B410200",
               IDX_OFF="0x13B8", TAG_PWV5=hex(TAG_PWV5),
               TAG_PWV7=hex(TAG_PWV7),
               SLOT7="0x0B500000", SLOT7_MAX="0x10000",
               TS_DATA=hex(TS_ADDR), TS_END=hex(TS_ADDR + 0x8000))
    subprocess.run(["sh", "tools/centre/build.sh"], check=True, env=env,
                   capture_output=True, cwd=ROOT)
    syms = {n.lstrip("_"): int(a, 16) for a, _, n in
            (ln.split() for ln in open(os.path.join(out, "cw.syms")))}
    return open(os.path.join(out, "cw.bin"), "rb").read(), syms


def pwv5_section(entries, rate):
    body = b"".join(struct.pack(">H", w) for w in entries)
    hdr = (b"PWV5" + struct.pack(">IIII", HDR, HDR + len(body), 2,
                                 len(entries)) +
           bytes([0, rate, 3, 5]))
    return hdr + body


def pwv7_section(entries3, count=None):
    body = b"".join(bytes(e) for e in entries3)
    cnt = count if count is not None else len(entries3)
    hdr = (b"PWV7" + struct.pack(">IIII", HDR, HDR + len(body), 2, cnt) +
           bytes([0, 150, 0, 0]))
    return hdr + body


STUB = struct.pack("<4H", 0x400B, 0x0009, 0x000B, 0x0009)   # jsr @r0; nop; rts; nop


def theme_blob(maps, cur):
    """Fake ts.c blob: S = sorted stock colours, one D row per theme.
    maps = list of {stock_colour: theme_colour}."""
    s = sorted({c for m in maps for c in m})
    nt = len(maps)
    ns = len(s)
    hdr_len = 56
    s_off = hdr_len
    d_off = s_off + 2 * ns
    b = bytearray(b"\0" * (d_off + 2 * nt * ns))
    struct.pack_into("<I", b, 0, struct.unpack("<I", b"XTSW")[0])
    struct.pack_into("<III", b, 4, 1, ns, nt)
    struct.pack_into("<I", b, 16, s_off)
    struct.pack_into("<I", b, 24, d_off)
    b[32] = cur
    for i, v in enumerate(s):
        struct.pack_into("<H", b, s_off + 2 * i, v)
    for t, m in enumerate(maps):
        for i, v in enumerate(s):
            struct.pack_into("<H", b, d_off + 2 * (t * ns + i), m.get(v, v))
    return bytes(b)


CTX = 0x0B420000              # fake ANLZ ctx (IDX_OFF index inside)
CTX_TAB = 0x0B410200          # holds the pointer to CTX (snap7 reads it)
SEC = 0x0B520000              # PWV7 source section (snap7 copies to SLOT7)
IDX_OFF = 0x13B8


def anlz_ctx_with_pwv7(sec):
    """ctx with one index entry pointing at the PWV7 section (snap7 input).
    Entry e = idx+4: tag at e+4, raw section ptr at e+12 (cw.c snap7)."""
    b = bytearray(0x1600)
    struct.pack_into("<I", b, IDX_OFF, 1)              # n entries
    struct.pack_into("<I", b, IDX_OFF + 8, TAG_PWV7)   # e+4: tag
    struct.pack_into("<I", b, IDX_OFF + 16, SEC)       # e+12: section ptr
    return bytes(b)


def col_mode2(binimg, syms, sec, ts=None):
    """cw_col(10*8, 4, 0) in 3-band mode; snap7 runs (SNAP_HASH != 0)."""
    mem_data = {CW_BASE: binimg, MODE_ADDR: bytes([2]),
                SNAP_HASH: struct.pack("<I", 0x1234),
                CTX_TAB: struct.pack("<I", CTX),
                CTX: anlz_ctx_with_pwv7(sec), SEC: sec}
    if ts:
        mem_data[TS_ADDR] = ts
    c, mem, _ = run_code(STUB, regs={0: syms["cw_col"], 4: 10 * 8,
                                     5: 4, 6: 0}, data=mem_data,
                         limit=5_000_000)
    rows = c.b0[0]
    tbl = syms["cw_tbl"]
    return rows, [mem.r16(tbl + 2 * k) for k in range(min(rows or 0, 64))]


def col(binimg, syms, entries, rate, first, step):
    """cw_col(first, step, 0) on a fresh module image; returns rows."""
    mem_data = {CW_BASE: binimg, SLOT5: pwv5_section(entries, rate),
                MODE_ADDR: bytes([1])}       # SNAP_HASH stays 0 == last_hash
    c, mem, _ = run_code(STUB, regs={0: syms["cw_col"], 4: first * 8,
                                     5: step, 6: 0}, data=mem_data,
                         limit=5_000_000)
    rows = c.b0[0]
    tbl = syms["cw_tbl"]
    colours = [mem.r16(tbl + 2 * k) for k in range(min(rows or 0, 64))]
    return rows, colours


def expected(entries, first, step):
    best, bw = -1, 0
    for j in range(first, min(first + step, len(entries))):
        w = entries[j]
        if ((w >> 2) & 31) > best:
            best, bw = (w >> 2) & 31, w
    colour = (bw & 0xE000) | ((bw & 0x1C00) >> 2) | ((bw & 0x380) >> 5)
    return max(1, (best * 3) >> 1), colour


def main():
    binimg, syms = build()
    entries = [(j * 40503 + 12345) & 0xFFFF for j in range(N)]
    bad = 0
    # 1. count > 65535 at rate 150: columns past the old 16-bit wrap
    #    (n & 0xFFFF = 34464) must still colour; index = painter index
    for k in (0, 5, 34462, 34463, 34464, 60000, N - 1):
        rows, colours = col(binimg, syms, entries, 150, k, 2)
        erows, ecol = expected(entries, k, 2)
        ok = rows == erows and colours and colours[0] == ecol
        if not ok:
            bad += 1
            print(f"FAIL rate150 k={k}: rows {rows} != {erows} or colour "
                  f"{colours[:1]} != {ecol:#06x}")
    # 2. rate 200: painter index k maps to entry k*200/150
    for k in (0, 30, 999, 74999 // 4 * 3):
        j = k * 200 // 150
        if j >= N:
            continue
        rows, colours = col(binimg, syms, entries, 200, k, 3)
        erows, ecol = expected(entries, j, 3 * 200 // 150)
        ok = rows == erows and colours and colours[0] == ecol
        if not ok:
            bad += 1
            print(f"FAIL rate200 k={k}->j={j}: rows {rows} != {erows} or "
                  f"colour {colours[:1]} != {ecol:#06x}")
    # 3. 3-band palette follows the theme (tools/themesw blob at TS_DATA):
    #    same column, three blob states -- absent, theme 1 (tinted), cur=0
    sec = pwv7_section([(50, 100, 200)] * 64)
    r0, c0 = col_mode2(binimg, syms, sec)                   # no blob
    r0b, c0b = col_mode2(binimg, syms, sec,
                         ts=b"\0" * 64)                     # blob bad magic
    tint = {C_CORE: 0x1234, C_MIDC: 0x5678, C_OUTER: 0x9ABC}
    r1, c1 = col_mode2(binimg, syms, sec, ts=theme_blob([tint], 1))
    r2, c2 = col_mode2(binimg, syms, sec, ts=theme_blob([tint], 0))
    m = tint
    for tag, got, want in ((f"3band stock {c0[:3]}", c0, c0),
                           (f"3band badmagic", c0b, c0),
                           ("3band themed", c1, [m.get(v, v) for v in c0]),
                           ("3band cur=0", c2, c0)):
        if got != want or r0 != r1 or r0 != r2 or not c0:
            bad += 1
            print(f"FAIL {tag}: got {got[:6]} rows {r0}/{r1}/{r2}")
    print(f"cw unit: {'PASS' if not bad else f'{bad} FAILURES'} "
          f"(count {N}, wrap 34464, theme tint {'on' if c1 != c0 else 'OFF'})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
