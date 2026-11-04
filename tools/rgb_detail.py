#!/usr/bin/env python3
"""rgb_detail.py -- RGB CENTRE (detail) waveform for the XDJ-700.

Stock painter FUN_08DC39B6 draws each screen column in palette[whiteness]
(8 RGB565 colours @0x0E736150) from a decoded-PWV3 buffer of 150/s
entries. Colour fetch:
    0x8DC3B7E  mov.l [0x8DC3D94],r4   ; palette base (pool used ONLY here)
    0x8DC3B80  shll r8                ; r8 = entry index * 8
    0x8DC3B86  shll r2                ; r2 = min(whiteness,7) * 2
    0x8DC3B88  mov.l r11,@(8,r15)
    0x8DC3B8E  add r4,r2              ; r2 -> palette entry
    ...        rows drawn with mov.w @r9 (r9 = r2)
Hook (6 bytes): pool 0x8DC3D94 -> DCAVE; insn 0x8DC3B86 shll r2 (0x4200)
-> jsr @r4 (0x440B; the store at B88 becomes its delay slot). DCAVE
returns r2 = pointer to the colour, r4 = 0 (so `add r4,r2` keeps it):
  mode 1 (RGB): colour of PWV5[index] (rekordbox colour detail, same
    150/s indexing, already in RAM since track load: ANLZ extra-section
    index, like the overview) -> RGB565 in a scratch word.
  other modes / no PWV5 / index out of range: the stock palette entry.
DCAVE clobbers only r0 r2 r3 r4 r6 r7 (the painter reloads r6/r7; r1,
r5, r8-r14 are live and preserved).

SAFE INSTALL ORDER (deck): the painter runs every frame while a track is
loaded and NOT AT ALL with no track loaded (emu-verified) -> install
right after a reboot, BEFORE loading any track. Then no half-written
state can execute. Write DCAVE, then the pool, then the instruction.

  rgb_detail.py emu     (fake detail inputs in emu, check drawn colours)
  rgb_detail.py plan
"""
import functools
import os
import struct
import sys

sys.path[:0] = [".", "tools"]
from shasm import assemble
import rgb_overview as RO

POOL = 0x08DC3D94
PALETTE = 0x0E736150           # stock value of POOL
INSN = 0x08DC3B86
INSN_OLD, INSN_NEW = 0x4200, 0x440B
DCAVE = RO.CAVE + 0x2800        # inside the deck-surveyed zero run (survey!)
DSCRATCH = DCAVE + 0x1FC        # u16 colour for the current column
TAG_PWV5 = struct.unpack("<I", b"PWV5")[0]
COPY5 = DCAVE + 0x100           # snapshot routine (called by the overview cave)
PREVIEW = 0x0BA3BCCC            # overview painter input (LOADED track's PWAV)
PREVIEW_WORDS = 200             # 800 B hashed
TAG_PWV4 = RO.TAG_PWV4
TAG_PWV6 = RO.TAG_PWV6

# ---- centre colours in C (tools/centre/cw.c): RGB, stacked 3-BAND, stock
TAG_PWV7 = struct.unpack("<I", b"PWV7")[0]
CW_BASE = int(os.environ.get("CW_BASE", "0x098A0000"), 0)   # deck-free all states
SLOT7 = 0x098B0000                      # PWV7 snapshot (3 B x 150/s)
SLOT7_MAX = 0xB0000                     # ~26 min; ends 0x09960000
ROWINC = (0x08DC3B9E, 0x6291, 0x6295)   # row loop: mov.w @r9,r2 -> @r9+,r2
# 2026-10-18: the PERFORM-screen strip = twin painter FUN_08DC2754 (50 rows,
# own palette 0x0E735FA8 via pool 0x08DC2A18, single user 0x08DC2922; same
# shll r2 / add r6,r2 / row loop shape; index*8 is copied to r8 by the
# jsr's delay slot `mov r7,r8`; it reads r1/r4 later -> DCAVE2 keeps them,
# returns r6 = 0). Stayed stock blue until now (user photo).
POOL2, PALETTE2 = 0x08DC2A18, 0x0E735FA8
INSN2 = 0x08DC292A                      # shll r2 -> jsr @r6
INSN2_NEW = 0x460B
ROWINC2 = (0x08DC2946, 0x6291, 0x6295)
DCAVE2 = DCAVE + 0x80


def cw_build():
    try:                                    # canonical home (dev + frozen app)
        from emu.paths import repo_root
        R = repo_root()
    except Exception:                       # bare tools/ checkout: this file's repo
        R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, CW_BASE=hex(CW_BASE), MODE_ADDR=hex(RO.MODE_ADDR),
               SLOT5=hex(RO.SLOT5), SNAP_HASH=hex(RO.SNAP_HASH),
               CTX_TAB=hex(RO.CTX_TAB), IDX_OFF=hex(RO.IDX_OFF),
               TAG_PWV5=hex(TAG_PWV5), TAG_PWV7=hex(TAG_PWV7),
               SLOT7=hex(SLOT7), SLOT7_MAX=hex(SLOT7_MAX))
    import subprocess
    gcc = os.path.join(R, "work/sh-tools/bin/sh-elf-gcc")
    pre, presyms = (os.path.join(R, "build/centre/cw.bin"),
                    os.path.join(R, "build/centre/cw.syms"))
    if os.path.exists(gcc):
        subprocess.run(["sh", "tools/centre/build.sh"], check=True, env=env,
                       capture_output=True, cwd=R)
    elif not os.path.exists(pre):
        # last resort: a checkout of the public repo carries the compiled
        # module in prebuilt/deck (build/ is gitignored there)
        alt = [os.path.join(R, "prebuilt/deck/centre", f)
               for f in ("cw.bin", "cw.syms")]
        if all(os.path.exists(a) for a in alt):
            pre, presyms = alt
        else:
            # shipped apps carry the prebuilt module (tools/build_studio.sh)
            raise FileNotFoundError("centre colour module not built "
                                    "(needs work/sh-tools or build/centre)")
    syms = {}
    for ln in open(presyms):
        a_, _, n_ = ln.split()
        syms[n_.lstrip("_")] = int(a_, 16)
    return open(pre, "rb").read(), syms


def _src(syms):
    return f"""
dcave:                      ; colour fetch hook: the painter calls this per
  sts.l pr,@-r15            ; column (jsr @r4 at 0x8DC3B86); it needs r1, r5,
  sts.l macl,@-r15          ; r8-r14 and MAC kept. C (cw_col) fills the per-
  sts.l mach,@-r15          ; row colour table cw_tbl; the row loop reads it
  mov.l r1,@-r15            ; with post-increment (ROWINC patch).
  mov.l r5,@-r15
  mov r8,r4                 ; arg0: entry index * 8
  mov.l @(36,r15),r5        ; arg1: entries per column (painter @(16,r15))
  mov r2,r6                 ; arg2: stock whiteness 0..7
  mov.l @({syms["cw_col"]:#x},pc),r0
  jsr @r0
  nop
  tst r0,r0                 ; 0 = keep the painter's own height
  bt dkeep
  mov r0,r10                ; rows to draw (RGB / 3-band)
dkeep:
  mov.l @r15+,r5
  mov.l @r15+,r1
  lds.l @r15+,mach
  lds.l @r15+,macl
  lds.l @r15+,pr
  mov.l @({syms["cw_tbl"]:#x},pc),r2
  rts
  mov #0,r4
"""


def _src2(syms):
    return f"""
dcave2:                     ; twin (PERFORM strip) colour fetch: jsr @r6 at
  sts.l pr,@-r15            ; 0x8DC292A; keeps r1 r4 r5 r8-r14 + MAC
  sts.l macl,@-r15
  sts.l mach,@-r15
  mov.l r1,@-r15
  mov.l r4,@-r15
  mov.l r5,@-r15
  mov r8,r4                 ; arg0: entry index * 8 (delay slot mov r7,r8)
  mov.l @(40,r15),r5        ; arg1: entries per column (painter @(16,r15))
  mov r2,r6                 ; arg2: stock whiteness 0..7
  mov.l @({syms["cw_col2"]:#x},pc),r0
  jsr @r0
  nop
  tst r0,r0
  bt d2keep
  mov r0,r10                ; rows to draw
d2keep:
  mov.l @r15+,r5
  mov.l @r15+,r4
  mov.l @r15+,r1
  lds.l @r15+,mach
  lds.l @r15+,macl
  lds.l @r15+,pr
  mov.l @({syms["cw_tbl"]:#x},pc),r2
  rts
  mov #0,r6
"""


def dcave2_bytes():
    code = assemble(_src2(cw_build()[1]))
    assert DCAVE2 + len(code) <= COPY5, len(code)
    return code


# ---- custom colours (mode 3, 2026-11-04) ----------------------------------
# The stock centre palette (8 RGB565 by "whiteness", RD.PALETTE at boot) --
# the Studio preview and the deck's 1-colour tint share this one copy.
STOCK_DETAIL = (0x09F1, 0x0AF7, 0x037F, 0x0D5E, 0x55BE, 0x757F, 0x757F, 0xCF3F)
CW_ROWS = 55                            # cw.c ROWS: centre line -> peak


def tint_spec(c565):
    """A theme spec that moves the stock blue onto colour c565 (the
    1-colour custom centre: the stock palette tinted, not flat)."""
    import colorsys
    r, g, b = ((c565 >> 11) & 31) / 31, ((c565 >> 5) & 63) / 63, (c565 & 31) / 31
    h, sat, v = colorsys.rgb_to_hsv(r, g, b)
    return {"hue": h * 360, "sat": 1.0 + sat, "val": 0.6 + v * 0.5,
            "spread": 0.3, "vivid": sat * 0.6}


def custom_tables(cols):
    """-> (kind, ramp[CW_ROWS], pal[8]) for 1-3 RGB565 colours, exactly the
    preview's maths: 2-3 colours = a centre-line -> peak ramp (kind 1),
    1 colour = the stock palette tinted toward it (kind 0)."""
    cols = list(cols)[:3]
    if len(cols) > 1:
        ramp = [RO.grad565(cols, k / (CW_ROWS - 1)) for k in range(CW_ROWS)]
        return 1, ramp, list(STOCK_DETAIL)
    import numpy as np
    import themes as T
    pal = [int(c) for c in T.map_colours(np.array(STOCK_DETAIL, np.uint16),
                                         tint_spec(cols[0] if cols else RO.STOCK_BLUE))]
    return 0, [pal[7]] * CW_ROWS, pal


def custom_writes(cols, syms=None):
    """(addr, byte) writes that fill the module's custom tables (inside
    CW_BASE..end, so every installer's region already covers them)"""
    syms = syms or cw_build()[1]
    kind, ramp, pal = custom_tables(cols)
    out = []
    for i, b in enumerate(struct.pack("<I", kind)):
        out.append((syms["cw_cust_kind"] + i, b))
    for i, b in enumerate(struct.pack(f"<{CW_ROWS}H", *ramp)):
        out.append((syms["cw_cust_ramp"] + i, b))
    for i, b in enumerate(struct.pack("<8H", *pal)):
        out.append((syms["cw_cust_pal"] + i, b))
    return out


def install_custom(mem, cols):
    for a_, b_ in custom_writes(cols):
        mem.w8(a_, b_)


def install(mem):
    """emulator install (same bytes/order as the deck installer): C module,
    shim, then pool + the two painter instructions"""
    cw, syms = cw_build()
    for a_ in range(CW_BASE, syms["end"], 4):
        mem.w32(a_, 0)
    for i, b_ in enumerate(cw):
        mem.w8(CW_BASE + i, b_)
    mem.w32(SLOT7, 0)
    for i, b_ in enumerate(dcave_bytes()):
        mem.w8(DCAVE + i, b_)
    assert mem.r32(POOL) in (PALETTE, DCAVE)
    mem.w32(POOL, DCAVE)
    mem.w16(INSN, INSN_NEW)
    mem.w16(ROWINC[0], ROWINC[2])
    for i, b_ in enumerate(dcave2_bytes()):
        mem.w8(DCAVE2 + i, b_)
    if mem.r32(POOL2) in (PALETTE2, DCAVE2):          # PERFORM strip
        mem.w32(POOL2, DCAVE2)
        mem.w16(INSN2, INSN2_NEW)
        mem.w16(ROWINC2[0], ROWINC2[2])


COPY_SRC = f"""
snap:                       ; called at overview-cave entry (every overview
  sts.l pr,@-r15            ; paint). Only on a NEW track (hash of the
  mov.l @({PREVIEW:#x},pc),r3   ; loaded track's preview changed) copy the
  mov.l @({PREVIEW_WORDS:#x},pc),r4  ; raw PWV4/PWV6/PWV5 sections into
  mov #0,r5                 ; private slots -- browsing later reuses the
hsh:                        ; ANLZ cache for OTHER tracks.
  mov.l @r3+,r0
  shll r5                   ; h = 2h + w (only has to change with the
  add r0,r5                 ; track; 2026-10-25: bytes for the LINK guard)
  dt r4
  bf hsh
  mov.l @({RO.SNAP_HASH:#x},pc),r3
  mov.l @r3,r0
  cmp/eq r5,r0
  bt sdone                  ; same track: keep snapshots
  mov.l r5,@r3
  mov.l @({TAG_PWV4:#x},pc),r4
  mov.l @({RO.SLOT4:#x},pc),r5
  mov.l @({RO.SLOT4_MAX:#x},pc),r6
  bsr cp1
  nop
  mov.l @({TAG_PWV6:#x},pc),r4
  mov.l @({RO.SLOT6:#x},pc),r5
  mov.l @({RO.SLOT6_MAX:#x},pc),r6
  bsr cp1
  nop
  mov.l @({TAG_PWV5:#x},pc),r4
  mov.l @({RO.SLOT5:#x},pc),r5
  mov.l @({RO.SLOT5_MAX:#x},pc),r6
  bsr cp1
  nop
sdone:
  lds.l @r15+,pr
  rts
  nop
cp1:                        ; r4 tag, r5 slot, r6 max bytes
  sts.l pr,@-r15
  mov #0,r0
  mov.l r0,@r5              ; invalidate slot first
  mov.l @({RO.CTX_TAB:#x},pc),r3
  bsr c1ok                  ; 2026-10-25 LINK guard: the ANLZ context is
  mov.l @r3,r3              ; shared with the Pro DJ Link server (a linked
  bf c1x                    ; deck's track): range-check every pointer
  mov.l @({RO.IDX_OFF:#x},pc),r0
  add r0,r3
  mov.l @r3+,r7
  mov #32,r0
  cmp/hi r0,r7
  bt c1x
c1l:
  tst r7,r7
  bt c1x
  mov.l @(4,r3),r0
  cmp/eq r4,r0
  bt c1f
  add #16,r3
  bra c1l
  add #-1,r7
c1f:
  bsr c1ok
  mov.l @(12,r3),r3         ; raw section
  bf c1x
  mov.l @r3,r0
  cmp/eq r4,r0
  bf c1x
  mov.b @(10,r3),r0         ; total length, BE u32 @+8 (low 16 bits;
  extu.b r0,r7              ; sections here are < 16 MB, high byte 0)
  shll8 r7
  mov.b @(11,r3),r0
  extu.b r0,r0
  or r0,r7
  mov.b @(9,r3),r0
  extu.b r0,r0
  shll16 r0
  or r0,r7
  cmp/hi r6,r7              ; too big for the slot -> leave invalid
  bt c1x
  add #3,r7
  shlr2 r7                  ; longwords
  mov r5,r1
  mov.l @r3+,r0             ; first word = tag: write LAST (publish)
  add #4,r1
  add #-1,r7
c1c:
  tst r7,r7
  bt c1p
  mov.l @r3+,r2
  mov.l r2,@r1
  add #4,r1
  bra c1c
  add #-1,r7
c1p:
  mov.l r0,@r5              ; publish (tag word)
c1x:
  lds.l @r15+,pr
  rts
  nop
c1ok:                       ; T = r3 is a 4-aligned address in 0x08..0x0F
  mov r3,r0                 ; (rotate the 2 low bits to the top: unaligned
  rotr r0                   ; -> huge; then the top 7 bits must be 0b0000001)
  rotr r0
  shlr16 r0
  shlr8 r0
  shlr r0
  rts
  cmp/eq #1,r0
"""


def copy_bytes():
    code = assemble(COPY_SRC)
    assert COPY5 + len(code) <= DSCRATCH, len(code)
    assert RO.COPY5 == COPY5
    return code


def dcave_bytes():
    code = assemble(_src(cw_build()[1]))
    assert len(code) <= DSCRATCH - DCAVE
    return code


def expected_colour(w):
    return (w & 0xE000) | ((w & 0x1C00) >> 2) | ((w & 0x380) >> 5)


def expected_column(pwv5, first, step):
    """(colour, rows) the cave returns for a column (1000 MK2 maths)."""
    n = len(pwv5) // 2
    best, bw = -1, 0
    for j in range(first, min(first + step, n)):
        w = (pwv5[2 * j] << 8) | pwv5[2 * j + 1]
        if ((w >> 2) & 31) > best:
            best, bw = (w >> 2) & 31, w
    return expected_colour(bw), max(1, (best * 3) >> 1)


def section_raw(path, tag):
    """whole ANLZ section (header included) as the deck caches it;
    `path` may also be the file's bytes"""
    b = path if isinstance(path, (bytes, bytearray)) else open(path, "rb").read()
    if len(b) < 8:
        raise SystemExit("no ANLZ data")
    p = struct.unpack(">I", b[4:8])[0]
    while p + 12 <= len(b):
        t = struct.unpack(">I", b[p + 8:p + 12])[0]
        if b[p:p + 4] == tag:
            return b[p:p + t]
        if t == 0:
            break
        p += t
    raise SystemExit(f"{tag} not in {path}")


C_CORE, C_MIDC, C_OUTER = 0xEF18, 0xE4A5, 0x1B1A


def expected_band_count(sec):
    return struct.unpack(">I", sec[16:20])[0]


@functools.lru_cache(maxsize=8)
def band_peak(sec):
    hl, ln = struct.unpack(">II", sec[4:12])
    peak = 1
    for j in range(expected_band_count(sec)):
        if hl + 3 * j + 3 > ln:
            break
        mi, hi, lo = sec[hl + 3 * j:hl + 3 * j + 3]
        peak = max(peak, lo * 125 + mi * 82 + hi * 64)
    return peak


def expected_band_column(sec, first, step, rows_max=55):
    """per-row colours cw.c puts in cw_tbl for a 3-band column (PWV7)"""
    sec = bytes(sec)
    hl = struct.unpack(">I", sec[4:8])[0]
    n = expected_band_count(sec)
    k7 = (rows_max << 16) // band_peak(sec)
    mi = hi = lo = 0
    for j in range(first, min(first + step, n)):
        a, b, c = sec[hl + 3 * j:hl + 3 * j + 3]
        mi, hi, lo = max(mi, a), max(hi, b), max(lo, c)
    a = lo * 125
    b = a + mi * 82
    c = b + hi * 64
    ha, hb, hc = (a * k7) >> 16, (b * k7) >> 16, (c * k7) >> 16
    hc = max(1, min(rows_max, hc))
    return [C_CORE if k < ha else C_MIDC if k < hb else C_OUTER for k in range(hc)]


def emu():
    """Full chain: overview cave (with COPY5 call) + detail cave armed,
    LOAD a track (overview painter -> COPY5 snapshots PWV5 into DBUF),
    then wreck the live ANLZ index entry (what browsing does) and drive
    the centre painter (detail_fake): colours must still be this
    track's PWV5."""
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    import detail_fake as DF
    ext = os.environ.get("EXT", os.path.expanduser(
        "~/Downloads/Rekordbox stuff/PIONEER/USBANLZ/P000/0002D83A/"
        "ANLZ0000.EXT"))
    m = Machine.load(os.environ.get("CKPT", "cache/emu_track_loaded.pkl"),
                     core="py")
    mem, c = m.mem, m.cpu
    wmode = os.environ.get("WMODE", "rgb")           # rgb | 3band
    for a_, b_ in RO.plan(RO.MODES[wmode])[1]:
        mem.w8(a_, b_)
    for i, b_ in enumerate(copy_bytes()):
        mem.w8(COPY5 + i, b_)
    assert all(mem.r8(RO.SNAP + i) == 0
               for i in range(0, RO.SLOT5 + RO.SLOT5_MAX - RO.SNAP, 256))
    assert mem.r32(POOL) == PALETTE and mem.r16(INSN) == INSN_OLD
    assert mem.r16(ROWINC[0]) == ROWINC[1]
    install(mem)
    syms = cw_build()[1]
    mem.dev.panel_frame = panl.build_frame(keys=["RotaryPush"])
    c.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    c.run(limit=40_000_000)                     # load -> overview -> COPY5
    pwv5 = RO.section_from_file(ext, b"PWV5")
    hl = mem.r8(RO.SLOT5 + 7)
    snap = bytes(mem.r8(RO.SLOT5 + hl + i) for i in range(len(pwv5)))
    n = len(pwv5) // 2 if snap == pwv5 else 0
    print(f"SLOT5 tag ok: {mem.r32(RO.SLOT5) == TAG_PWV5}, data equal: "
          f"{snap == pwv5} ({len(pwv5) // 2} entries)")
    # simulate browsing: the live index's PWV5 entry now points elsewhere
    ctx = mem.r32(RO.CTX_TAB) & 0x1FFFFFFF
    ix = ctx + RO.IDX_OFF
    for k in range(mem.r32(ix)):
        e = ix + 4 + 16 * k
        if mem.r32(e + 4) == TAG_PWV5:
            mem.w32(e + 12, 0x0BAD0000)
    c.run(limit=30_000_000)                    # let the load settle
    mem.dev.panel_frame = panl.build_frame(keys=["Browse"])  # main screen
    c.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    c.run(limit=30_000_000)
    # The load choreography lands on the browser overlay (the load press
    # opened the browser over the main screen). 2026-10-10 GUI: the centre
    # painter only runs on the MAIN screen -- sit there before measuring.
    mem.dev.panel_frame = panl.build_frame(keys=["Browse"])
    c.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    c.run(limit=6_000_000)
    p7 = None
    if wmode == "3band":
        # the .EXT on this stick has no PWV7 yet (Mod Studio copies it in
        # from .2EX); add the section + an index entry the way the deck's
        # loader lists extra sections
        sec = section_raw(ext[:-3] + "2EX", b"PWV7")
        SEC = 0x0F100000
        for i, b_ in enumerate(sec):
            mem.w8(SEC + i, b_)
        ctx = mem.r32(RO.CTX_TAB) & 0x1FFFFFFF
        ix = ctx + RO.IDX_OFF
        n_ = mem.r32(ix)
        e = ix + 4 + 16 * n_
        for o_, v_ in ((0, 0), (4, TAG_PWV7), (8, len(sec)), (12, SEC)):
            mem.w32(e + o_, v_)
        mem.w32(ix, n_ + 1)
        p7 = sec
    DF.install(m, ext, int(os.environ.get("POS", "3000")))
    seen, bad = {}, 0
    tbl = syms["cw_tbl"]

    def tr(pc, op):
        nonlocal bad
        if pc == 0x08DC3B8A:
            idx = c.gr(8) >> 3
            step = mem.r32((c.gr(15) & 0x1FFFFFFF) + 16)
            rows = c.gr(10)
            got = [mem.r16(tbl + 2 * k) for k in range(rows)]
            if (c.gr(2) & 0x1FFFFFFF) != tbl:
                bad += 1
                return
            if wmode == "rgb" and idx * 2 + 1 < len(pwv5):
                ec, eh = expected_column(pwv5, idx, step)
                bad += (got[:1], rows) != ([ec], eh) or len(set(got)) != 1
                seen[idx] = (got[0], rows)
            elif wmode == "3band" and idx < expected_band_count(p7):
                want = expected_band_column(p7, idx, step)
                bad += got != want
                seen[idx] = (tuple(got), rows)
    c.trace = tr
    c.run(limit=int(os.environ.get("STEPS", "20000000")))
    c.trace = None
    fb_png(m, f"screenshots/centre_{wmode}_emu.png")
    print(f"mode {wmode}: columns: {len(seen)} distinct colours: "
          f"{len(set(v[0] for v in seen.values()))} heights "
          f"{min((v[1] for v in seen.values()), default=0)}.."
          f"{max((v[1] for v in seen.values()), default=0)} mismatches: {bad}")
    ok = snap == pwv5 and len(seen) > 50 and bad == 0
    if wmode == "3band":
        ok &= len(set(len(set(v[0])) for v in seen.values()) - {1}) > 0
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "emu"
    if mode == "plan":
        c = dcave_bytes()
        print(f"dcave {len(c)} B @ {DCAVE:#x}; pool {POOL:#x} {PALETTE:#x}"
              f"->{DCAVE:#x}; insn {INSN:#x} {INSN_OLD:#06x}->{INSN_NEW:#06x}")
        return 0
    return emu()


if __name__ == "__main__":
    sys.exit(main())
