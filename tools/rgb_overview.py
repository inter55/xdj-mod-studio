#!/usr/bin/env python3
"""rgb_overview.py -- RGB overview waveform for the XDJ-700 (emu-first MVP).

How the stock deck works (found 2026-10-04, emulator with a real track):
  * On track load the ANLZ module caches the .EXT file and ALSO loads
    every non-standard section into RAM -- incl. rekordbox's colour
    sections PWV5 (detail) and PWV4 (overview, 1200 cols x 6 B). They
    sit in the "extra section" index at ctx+0x13B8 (ctx = [0x09826BE4],
    the USB slot): u32 count, then 16-B entries {off, tag, len, ptr}.
    The ptr points at the raw section (big-endian header, 24 B).
  * Overview painter FUN_08DBC230 draws 500 cols x 34 rows (1 px/col,
    bottom row 33) from the mono PWAV, then calls the layer release
    fn [0x8DBC5C8] = 0x08C2C984(handle). At that call the painter's
    frame holds @(0,r15) = stride (px), @(4,r15) = layer pointer.

The mod (RAM only): point pool word 0x8DBC5C8 at a cave that repaints
the 500 columns from PWV4 (height = d2*17>>6 (bytes are 7-bit), colour = bands d3/d4/d5
as R/G/B, brightness-normalised by doubling until the strongest band
>= 128), then tail-jumps to the original release fn. No PWV4 -> the
stock mono overview stays untouched.

  .venv/bin/python tools/rgb_overview.py emu   (apply in emu, load track,
                                                compare to model, PNGs)
  .venv/bin/python tools/rgb_overview.py plan  (byte list for the deck)
"""
import os
import struct
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tools")
from shasm import assemble

HOOK_POOL = 0x08DBC5C8      # pool word used ONLY by the painter's final jsr
ORIG_RELEASE = 0x08C2C984   # stock value of HOOK_POOL
CTX_TAB = 0x09826BE4        # ANLZ ctx table, slot 0 = USB
IDX_OFF = 0x13B8            # ctx + 0x478 + 0xF40: .EXT extra-section index
TAG_PWV4 = struct.unpack("<I", b"PWV4")[0]
TAG_PWV6 = struct.unpack("<I", b"PWV6")[0]   # 3-band (copied in by stick_add_3band.py)
STEP = 157286               # 1200/500 in 16.16 (2.4 * 65536)
CAVE = int(os.environ.get("CAVE", "0x081D8900"), 0)
# 2026-10-04: the first cave (0x088BA000) sat inside a graphic that the
# display hardware reads directly (cave bytes showed as a coloured dotted
# line by TRACK on deck + emu; CPU read-watch could not see it). 0x09608000
# (525 KB zero run above the image) junk-filled: 0 screen px changed,
# untouched by firmware through boot/browse/load/taglist -- BUT on the
# REAL deck 0x09608000 is NOT zero (live data): the installer aborted.
# 0x081D8900 (31 KB zero run in the image): emu junk-fill 0 px changed,
# deck read-only survey ALL ZERO (2026-10-04).
OLD_CAVES = [0x088BA000]
COLS, ROWS = 500, 34
MODE_ADDR = CAVE + 0x7F0    # u8: 0 = stock blue, 1 = RGB, 2 = 3-band
SCRATCH = CAVE + 0x7F4      # u32: 3-band scale K (computed per track)
RECIP = CAVE + 0x580        # u16[256]: 65535 // m  (in the code blob)
COPY5 = CAVE + 0x2800 + 0x100   # rgb_detail.py snapshot routine
SNAP = int(os.environ.get("SNAP", "0x0972B000"), 0)  # snapshot area
SLOT4, SLOT6, SNAP_HASH, SLOT5 = SNAP, SNAP + 0x2000, SNAP + 0x3000, SNAP + 0x4000
SLOT4_MAX, SLOT6_MAX, SLOT5_MAX = 0x2000, 0x1000, 0x40000
MODES = {"blue": 0, "rgb": 1, "3band": 2, "custom": 3}
STOCK_BLUE = 0x6D1E         # the stock overview bar colour (emu-sampled; one colour)
# 2026-11-04 custom mode (3): the stock painter draws its mono bar, then the
# cave recolours every STOCK_BLUE pixel with CUSTOM_TAB[row] (34 RGB565,
# row 0 = bottom): a 1-3 colour gradient the Studio chose -- exactly the
# preview's maths (custom_table). The frame, beat marks and playhead are
# other colours and stay.
CUSTOM_TAB = CAVE + 0x780   # u16[34], ends 0x7C4 (mode byte at 0x7F0)
C_LOW, C_MID, C_HIGH = 0x1B1A, 0xE4A5, 0xEF18   # 3-band blue/orange/cream
THEME_FLAG = MODE_ADDR + 1  # u8: 1 = run theme pass once (tools/theme_deck.py)
P5FLAG = MODE_ADDR + 2      # u8: this paint draws RGB from PWV5 (centre look)
# 2026-10-11: OFF. Max-of-~57-PWV5-entries per column saturates (every
# column full height, colour of one transient = the "distorted" overview
# the user saw on deck + emu). The XDJ-1000MK2 draws its overview from
# PWV4 too (loader 0x92CBF56), and the PWV4 Beat-Link look was the one
# confirmed on hardware 2026-10-04. Centre waveform keeps PWV5.
OVERVIEW_FROM_PWV5 = False
STEPVAR = CAVE + 0x7F8      # u32: per-paint column step (16.16)
TAG_PWV5_ = 0x35565750      # "PWV5" little-endian word (as stored)
THEME_CODE = CAVE + 0x800

SRC = f"""
cave:
  sts.l pr,@-r15
  mov.l r4,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov.l r12,@-r15
  mov.l r13,@-r15
  mov.l r14,@-r15
  mov.l @({COPY5:#x},pc),r1  ; on a NEW track: snapshot PWV4/5/6 into
  jsr @r1                   ; private slots (rgb_detail.py) BEFORE drawing
  nop
  mov.l @({THEME_FLAG:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0                 ; theme pending? (tools/theme_deck.py)
  bt nothm
  mov #0,r0
  mov.b r0,@r1              ; one-shot: clear first
  mov.l @({THEME_CODE:#x},pc),r1
  jsr @r1                   ; recolour artwork regions (clobbers r0-r14)
  nop
nothm:
  mov.l @(36,r15),r8        ; painter @(0,r15): stride in px
  add r8,r8                 ; -> bytes per row
  mov.l @(40,r15),r9        ; painter @(4,r15): layer pointer
  tst r9,r9
  bt x1
  mov.l @({MODE_ADDR:#x},pc),r0
  mov.b @r0,r0
  extu.b r0,r0
  tst r0,r0                 ; mode 0 = stock blue: leave it
  bt x1
  cmp/eq #3,r0              ; mode 3 = custom colours over the stock bar
  bt x3
  cmp/eq #2,r0
  bt t6
  mov.l @({TAG_PWV4:#x},pc),r3
  bra tagok
  nop
t6:
  mov.l @({TAG_PWV6:#x},pc),r3
tagok:
  mov.l @({SLOT4:#x},pc),r10 ; snapshot slot for this mode's tag
  mov.l @({TAG_PWV6:#x},pc),r0
  cmp/eq r3,r0
  bf ffound
  mov.l @({SLOT6:#x},pc),r10
  bra ffound
  nop
x1:
  bra done
  nop
x3:
  bra custom
  nop
ffound:
  tst r10,r10
  bt x1
  mov.l @r10,r0
  cmp/eq r3,r0              ; section really starts with the tag
  bf x1
  mov.b @(7,r10),r0         ; BE header length (low byte)
  extu.b r0,r0
  add r0,r10                ; column data
  mov.l @({MODE_ADDR:#x},pc),r0
  mov.b @r0,r0
  cmp/eq #2,r0
  bf nopre
  mov r10,r2                ; 3-band pre-pass: track peak of the
  mov.l @(1200,pc),r1       ; cumulative height (Beat Link scales
  mov #1,r3                 ; each track to its own maximum)
pre:
  mov.b @(2,r2),r0
  extu.b r0,r0
  mov #125,r4
  mulu.w r0,r4
  sts macl,r5
  mov.b @(0,r2),r0
  extu.b r0,r0
  mov #82,r4
  mulu.w r0,r4
  sts macl,r4
  add r4,r5
  mov.b @(1,r2),r0
  extu.b r0,r0
  mov #64,r4
  mulu.w r0,r4
  sts macl,r4
  add r4,r5
  shlr8 r5
  cmp/hi r3,r5
  bf pn
  mov r5,r3
pn:
  add #3,r2
  dt r1
  bf pre
  mov #-1,r0
  extu.b r0,r0
  cmp/hi r0,r3
  bf pk
  mov r0,r3
pk:
  mov r3,r0
  add r0,r0
  mov.l @({RECIP:#x},pc),r1
  mov.w @(r0,r1),r4
  extu.w r4,r4
  mov #33,r5
  mulu.w r4,r5
  sts macl,r4               ; K = 33 * 65535/peak
  mov.l @({SCRATCH:#x},pc),r1
  mov.l r4,@r1
nopre:
  mov.l @({STEP:#x},pc),r0  ; default column step (1200-entry PWV4/PWV6)
  mov.l @({STEPVAR:#x},pc),r1
  mov.l r0,@r1
  mov #0,r0
  mov.l @({P5FLAG:#x},pc),r1
  mov.b r0,@r1
  mov.l @({MODE_ADDR:#x},pc),r0
  mov.b @r0,r0
  cmp/eq #{1 if OVERVIEW_FROM_PWV5 else 99},r0   ; 99 = never: PWV4 path
  bf p5no
  mov.l @({SLOT5:#x},pc),r2 ; RGB: draw from the PWV5 snapshot like the
  mov.l @r2,r0              ; centre waveform (1000 MK2 maths)
  mov.l @({TAG_PWV5_:#x},pc),r1
  cmp/eq r1,r0
  bf p5no
  mov #17,r0                ; entry count, BE u32 @+16 (low 24 bits)
  mov.b @(r0,r2),r0
  extu.b r0,r3
  shll16 r3
  mov #18,r0
  mov.b @(r0,r2),r0
  extu.b r0,r0
  shll8 r0
  or r0,r3
  mov #19,r0
  mov.b @(r0,r2),r0
  extu.b r0,r0
  or r0,r3                  ; n5
  tst r3,r3
  bt p5no
  mov.l @({SCRATCH:#x},pc),r1
  mov.l r3,@r1
  mov.l @({131:#x},pc),r1   ; step5 = n5 * 65536 / 500
  mul.l r3,r1
  sts macl,r4
  mov.l @({4719:#x},pc),r1
  mul.l r3,r1
  sts macl,r1
  shlr16 r1
  add r1,r4
  mov.l @({STEPVAR:#x},pc),r1
  mov.l r4,@r1
  mov.b @(7,r2),r0          ; header length
  extu.b r0,r0
  add r0,r2
  mov r2,r10                ; r10 = PWV5 entries
  mov #1,r0
  mov.l @({P5FLAG:#x},pc),r1
  mov.b r0,@r1
p5no:
  mov #33,r0
  mulu.w r0,r8
  sts macl,r0
  add r0,r9                 ; r9 = bottom row (33) base
  mov #0,r11                ; col
  mov #0,r12                ; src pos, 16.16
cloop:
  mov.l @({P5FLAG:#x},pc),r0
  mov.b @r0,r0
  tst r0,r0
  bt clorig
  bra p5col
  nop
clorig:
  mov r12,r0
  shlr16 r0
  mov r0,r1
  add r0,r0
  add r1,r0                 ; 3*idx
  mov r0,r13
  mov.l @({MODE_ADDR:#x},pc),r0
  mov.b @r0,r0
  cmp/eq #2,r0
  bf rgbcol
  bra band3
  nop
rgbcol:
  add r13,r13               ; 6*idx (PWV4)
  add r10,r13               ; -> first entry under this pixel
  mov.l @({STEPVAR:#x},pc),r0
  mov.l @r0,r2
  add r12,r2
  shlr16 r2                 ; idx1 (exclusive)
  mov r12,r0
  shlr16 r0
  sub r0,r2                 ; n = entries under this pixel (2..3)
  mov #1,r0
  cmp/hs r0,r2
  bt avn
  mov #1,r2
avn:
  mov r2,r7                 ; keep n
  mov #0,r4                 ; sums of d3/d4/d5 (AVERAGE, not nearest:
  mov #0,r5                 ; single saturated treble entries made
  mov #0,r6                 ; full-height blue/purple spikes)
avl:
  mov.b @(3,r13),r0
  extu.b r0,r0
  add r0,r4
  mov.b @(4,r13),r0
  extu.b r0,r0
  add r0,r5
  mov.b @(5,r13),r0
  extu.b r0,r0
  add r0,r6
  dt r2
  bf/s avl
  add #6,r13
  mov r7,r0
  cmp/eq #2,r0
  bf av3
  shlr r4
  shlr r5
  bra avd
  shlr r6
av3:
  cmp/eq #3,r0
  bf avd
  mov #-85,r1
  extu.b r1,r1              ; 171: v*171>>9 == v/3 for v <= 381
  mulu.w r4,r1
  sts macl,r4
  shlr8 r4
  shlr r4
  mulu.w r5,r1
  sts macl,r5
  shlr8 r5
  shlr r5
  mulu.w r6,r1
  sts macl,r6
  shlr8 r6
  shlr r6
avd:                        ; RGB (Beat Link / NXS2 geometry):
                            ;  back layer  h=max(d3,d4,d5) col*191/max
                            ;  front layer h=d5            col*255/max
  mov #17,r1
  mulu.w r6,r1
  sts macl,r13
  shlr2 r13
  shlr2 r13
  shlr2 r13                 ; front rows = d5*17>>6
  mov r4,r0
  cmp/hs r5,r0
  bt m1
  mov r5,r0
m1:
  cmp/hs r6,r0
  bt m2
  mov r6,r0
m2:
  tst r0,r0
  bt black
  mov r0,r7
  mulu.w r7,r1
  sts macl,r7
  shlr2 r7
  shlr2 r7
  shlr2 r7                  ; back rows = max*17>>6
  add r0,r0
  mov.l @({RECIP:#x},pc),r1 ; reciprocal table
  mov.w @(r0,r1),r3
  extu.w r3,r3              ; 65535/max
  mulu.w r4,r3
  sts macl,r4
  shlr8 r4
  mulu.w r5,r3
  sts macl,r5
  shlr8 r5
  mulu.w r6,r3
  sts macl,r6
  shlr8 r6                  ; channels scaled to the peak (~0..255)
  mov #-1,r0
  extu.b r0,r0              ; 255
  cmp/hi r0,r4
  bf c1
  mov r0,r4
c1:
  cmp/hi r0,r5
  bf c2
  mov r0,r5
c2:
  cmp/hi r0,r6
  bf c3
  mov r0,r6
c3:
  mov r4,r14
  shlr2 r14
  shlr r14
  shll8 r14
  shll2 r14
  shll r14
  mov r5,r0
  shlr2 r0
  shll2 r0
  shll2 r0
  shll r0
  or r0,r14
  mov r6,r0
  shlr2 r0
  shlr r0
  or r0,r14                 ; front colour
  mov r4,r0
  shlr2 r0
  sub r0,r4
  mov r5,r0
  shlr2 r0
  sub r0,r5
  mov r6,r0
  shlr2 r0
  sub r0,r6                 ; back = 3/4 brightness
  mov r4,r3
  shlr2 r3
  shlr r3
  shll8 r3
  shll2 r3
  shll r3
  mov r5,r0
  shlr2 r0
  shll2 r0
  shll2 r0
  shll r0
  or r0,r3
  mov r6,r0
  shlr2 r0
  shlr r0
  or r0,r3                  ; back colour
  bra draw
  nop
black:
  mov #0,r7
  mov #0,r13
draw:
  mov r11,r0
  add r0,r0
  add r9,r0
  mov r0,r6                 ; bottom pixel of this column
  mov #0,r1
  mov #34,r2
rloop:
  cmp/hs r13,r1
  bt rb
  bra rput
  mov r14,r0
rb:
  cmp/hs r7,r1
  bt rz
  bra rput
  mov r3,r0
rz:
  mov #0,r0
rput:
  mov.w r0,@r6
  sub r8,r6
  add #1,r1
  dt r2
  bf rloop
cnext:
  mov.l @({STEPVAR:#x},pc),r0
  mov.l @r0,r0
  add r0,r12
  add #1,r11
  mov.l @({COLS:#x},pc),r0
  cmp/hs r0,r11
  bf cloop2
done:
  mov.l @r15+,r14
  mov.l @r15+,r13
  mov.l @r15+,r12
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  mov.l @r15+,r4
  lds.l @r15+,pr
  mov.l @({ORIG_RELEASE:#x},pc),r1
  jmp @r1                   ; tail-call stock release(handle)
  nop
cloop2:
  bra cloop
  nop
p5col:                      ; tallest PWV5 entry in this column's slice
  mov r12,r1
  shlr16 r1                 ; i0
  mov.l @({STEPVAR:#x},pc),r0
  mov.l @r0,r0
  add r12,r0
  shlr16 r0
  mov r0,r2                 ; i1
  mov.l @({SCRATCH:#x},pc),r0
  mov.l @r0,r0              ; n5
  cmp/hi r0,r2
  bf p5a
  mov r0,r2
p5a:
  cmp/hs r0,r1              ; i0 past the end -> empty column
  bf p5c
  bra black
  nop
p5c:
  cmp/hs r2,r1
  bf p5b
  mov r1,r2
  add #1,r2                 ; at least one entry
p5b:
  mov #-1,r5
  mov #0,r6
p5l:
  mov r1,r0
  shll r0
  add r10,r0
  mov.b @r0,r3
  extu.b r3,r3
  shll8 r3
  mov.b @(1,r0),r0
  extu.b r0,r0
  or r0,r3                  ; w = rrrgggbbbhhhhh00
  mov r3,r4
  shlr2 r4
  mov #31,r0
  and r0,r4                 ; h
  cmp/gt r5,r4
  bf p5n
  mov r4,r5
  mov r3,r6
p5n:
  add #1,r1
  cmp/hs r2,r1
  bf p5l
  tst r5,r5
  bf p5h
  bra black
  nop
p5h:
  mov #35,r0                ; rows = h * 35 / 32 (0..33)
  mulu.w r5,r0
  sts macl,r13
  shlr2 r13
  shlr2 r13
  shlr r13
  mov.l @({0xE000:#x},pc),r0
  mov r6,r14
  and r0,r14                ; R << 13
  mov.l @({0x1C00:#x},pc),r0
  mov r6,r4
  and r0,r4
  shlr2 r4
  or r4,r14                 ; G << 8
  mov.l @({0x380:#x},pc),r0
  mov r6,r4
  and r0,r4
  shlr2 r4
  shlr2 r4
  shlr r4
  or r4,r14                 ; B << 2
  mov #0,r7                 ; one colour per bar (no back layer)
  bra draw
  nop
band3:                      ; PWV6 (mid, high, low), cumulative like
  add r10,r13               ; Beat Link: core=low*.49 (cream), +mid*.32
  mov #125,r1               ; (orange), +high*.25 (blue), scaled to the
  mov.b @(2,r13),r0         ; track peak via K (pre-pass)
  extu.b r0,r0
  mulu.w r0,r1
  sts macl,r6
  mov #82,r1
  mov.b @(0,r13),r0
  extu.b r0,r0
  mulu.w r0,r1
  sts macl,r5
  mov #64,r1
  mov.b @(1,r13),r0
  extu.b r0,r0
  mulu.w r0,r1
  sts macl,r4
  add r6,r5
  add r5,r4
  shlr8 r6
  shlr8 r5
  shlr8 r4
  mov.l @({SCRATCH:#x},pc),r1
  mov.l @r1,r1              ; K
  mul.l r1,r6
  sts macl,r6
  shlr16 r6
  mul.l r1,r5
  sts macl,r5
  shlr16 r5
  mul.l r1,r4
  sts macl,r4
  shlr16 r4
  mov r11,r0
  add r0,r0
  add r9,r0
  mov r0,r13                ; bottom pixel of this column
  mov #0,r1
  mov #34,r2
bloop:
  cmp/hs r6,r1
  bt b1
  mov.l @({C_HIGH:#x},pc),r0
  bra bput
  nop
b1:
  cmp/hs r5,r1
  bt b2
  mov.l @({C_MID:#x},pc),r0
  bra bput
  nop
b2:
  cmp/hs r4,r1
  bt b3
  mov.l @({C_LOW:#x},pc),r0
  bra bput
  nop
b3:
  mov #0,r0
bput:
  mov.w r0,@r13
  sub r8,r13
  add #1,r1
  dt r2
  bf bloop
  bra cnext
  nop
custom:                     ; recolour the stock bar: 500 cols x 34 rows
  mov #33,r0
  mulu.w r0,r8
  sts macl,r0
  add r0,r9                 ; r9 = bottom row (33) base
  mov.l @({CUSTOM_TAB:#x},pc),r10
  mov.l @({STOCK_BLUE:#x},pc),r13
  mov #0,r11                ; col
ccol:
  mov r11,r0
  add r0,r0
  add r9,r0
  mov r0,r6                 ; bottom pixel of this column
  mov r10,r7                ; table: row 0 first
  mov #34,r2
crow:
  mov.w @r6,r0
  extu.w r0,r0
  cmp/eq r13,r0
  bf cskip
  mov.w @r7,r0
  mov.w r0,@r6
cskip:
  add #2,r7
  sub r8,r6
  dt r2
  bf crow
  add #1,r11
  mov.l @({COLS:#x},pc),r0
  cmp/hs r0,r11
  bf ccol
  bra done
  nop
"""


def hex565(h):
    """'#rrggbb' -> RGB565 (the Studio's colour strings)"""
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return _pack(r, g, b)


def grad565(cols, t):
    """Colour at t (0..1) on a straight RGB blend through cols (RGB565) --
    the Studio preview's maths (studio/app.py imports this one)."""
    if len(cols) == 1:
        return cols[0]
    t = min(1.0, max(0.0, t)) * (len(cols) - 1)
    i = min(int(t), len(cols) - 2)
    f = t - i
    a, b = cols[i], cols[i + 1]

    def ch(sh, mask):
        x, y = (a >> sh) & mask, (b >> sh) & mask
        return int(round(x + (y - x) * f)) << sh
    return (ch(11, 31) | ch(5, 63) | ch(0, 31)) or 0x0821   # 0 = empty pixel


def custom_table(cols):
    """CUSTOM_TAB contents for 1-3 RGB565 colours: row k (0 = bottom) ->
    grad565(cols, k / 33); one colour = a solid bar."""
    cols = list(cols)[:3] or [STOCK_BLUE]
    return [grad565(cols, k / (ROWS - 1)) for k in range(ROWS)]


def custom_writes(cols):
    tab = struct.pack(f"<{ROWS}H", *custom_table(cols))
    return [(CUSTOM_TAB + i, b) for i, b in enumerate(tab)]


def cave_bytes():
    """code, zero padding, then the reciprocal table at RECIP -- one
    contiguous blob the installers write (zeros are skipped on deck)."""
    code = assemble(SRC)
    assert len(code) % 2 == 0 and CAVE + len(code) <= RECIP, len(code)
    table = struct.pack("<256H", 0, *[65535 // m for m in range(1, 256)])
    return code + bytes(RECIP - CAVE - len(code)) + table


def _pack(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def model_column(d):
    """Python twin of the cave RGB math -> 34 px, bottom (k=0) first."""
    r, g, b = d[3], d[4], d[5]
    hf = (b * 17) >> 6
    m = max(r, g, b)
    if m == 0:
        return [0] * ROWS
    hb = (m * 17) >> 6
    rec = 65535 // m
    fr, fg, fb = [min(255, (c * rec) >> 8) for c in (r, g, b)]
    front = _pack(fr, fg, fb)
    back = _pack(fr - (fr >> 2), fg - (fg >> 2), fb - (fb >> 2))
    return [front if k < hf else back if k < hb else 0 for k in range(ROWS)]


def band3_k(data):
    peak = 1
    for i in range(0, len(data) - 2, 3):
        c = (data[i + 2] * 125 + data[i] * 82 + data[i + 1] * 64) >> 8
        peak = max(peak, c)
    peak = min(peak, 255)
    return 33 * (65535 // peak)


def model_band3(d, k):
    """PWV6 (mid, high, low), cumulative like Beat Link, scaled to the
    track peak: cream core = low, orange = +mid, blue outer = +high."""
    lo, mi, hi_ = d[2] * 125, d[0] * 82, d[1] * 64
    hh = (((lo) >> 8) * k & 0xFFFFFFFF) >> 16
    hm = (((lo + mi) >> 8) * k & 0xFFFFFFFF) >> 16
    hl = (((lo + mi + hi_) >> 8) * k & 0xFFFFFFFF) >> 16
    return [C_HIGH if k_ < hh else C_MID if k_ < hm else C_LOW if k_ < hl else 0
            for k_ in range(ROWS)]


def model_pixels(pwv4_data, mode):
    """[col][k] RGB565, k=0 bottom row."""
    cols = []
    k3 = band3_k(pwv4_data[:3600]) if mode == 2 else 0
    for col in range(COLS):
        idx = ((col * STEP) & 0xFFFFFFFF) >> 16
        if mode == 2:
            cols.append(model_band3(pwv4_data[idx * 3: idx * 3 + 3], k3))
            continue
        # RGB: AVERAGE the 2-3 PWV4 entries under this pixel (1200 -> 500).
        # Nearest-entry picking let single saturated treble entries
        # (d5 = 127) draw full-height blue/purple spikes (2026-10-11).
        n = max(1, ((((col * STEP) & 0xFFFFFFFF) + STEP) >> 16) - idx)
        sm = [sum(pwv4_data[(idx + j) * 6 + b] for j in range(n)) for b in range(6)]
        if n == 2:
            av = [v >> 1 for v in sm]
        elif n == 3:
            av = [(v * 171) >> 9 for v in sm]        # == v // 3 for v <= 381
        else:
            av = sm
        cols.append(model_column(bytes(av)))
    return cols


def model_p5(pwv5):
    """Python twin of the PWV5 (centre-look) RGB overview -> [col][k]."""
    n5 = len(pwv5) // 2
    step = n5 * 131 + ((n5 * 4719) >> 16)
    cols, pos = [], 0
    for col in range(COLS):
        i0, i1 = pos >> 16, min((pos + step) >> 16, n5)
        pos += step
        if i0 >= n5:
            cols.append([0] * ROWS)
            continue
        if i0 >= i1:
            i1 = i0 + 1
        best, bw = -1, 0
        for j in range(i0, i1):
            w = (pwv5[2 * j] << 8) | pwv5[2 * j + 1]
            if ((w >> 2) & 31) > best:
                best, bw = (w >> 2) & 31, w
        if best <= 0:
            cols.append([0] * ROWS)
            continue
        rows = (best * 35) >> 5
        c = (bw & 0xE000) | ((bw & 0x1C00) >> 2) | ((bw & 0x380) >> 5)
        cols.append([c if k < rows else 0 for k in range(ROWS)])
    return cols


def model_p5_mean(pwv5):
    """RGB overview from PWV5, AVERAGED per column (2026-10-06): mean height
    scaled so the loudest column fills the 34 rows, colour = height-weighted
    mean of the 3-bit channels. Replaces model_p5's per-column MAX, which
    saturates (~57 detail entries per column: almost every column hits 31 ->
    solid bars, colour from one transient -> flicker). Deck cave: TODO port
    (the max version was never installed on hardware)."""
    n = len(pwv5) // 2
    if n == 0:
        return [[0] * ROWS for _ in range(COLS)]
    sums = []
    for col in range(COLS):
        i0 = col * n // COLS
        i1 = max(i0 + 1, (col + 1) * n // COLS)
        sh = sr = sg = sb = 0
        for j in range(i0, min(i1, n)):
            w = (pwv5[2 * j] << 8) | pwv5[2 * j + 1]
            h = (w >> 2) & 31
            sh += h
            sr += ((w >> 13) & 7) * h
            sg += ((w >> 10) & 7) * h
            sb += ((w >> 7) & 7) * h
        sums.append((sh, i1 - i0, sr, sg, sb))
    peak = max(sh * 256 // cnt for sh, cnt, _, _, _ in sums) or 1
    cols = []
    for sh, cnt, sr, sg, sb in sums:
        rows = (sh * 256 // cnt * ROWS + peak // 2) // peak
        if sh == 0 or rows == 0:
            cols.append([0] * ROWS)
            continue
        r = min(31, (sr * 142 + sh * 16) // (sh * 32))      # x 4.43 (7 -> 31)
        g = min(63, (sg * 9 + sh // 2) // sh)               # x 9    (7 -> 63)
        b = min(31, (sb * 142 + sh * 16) // (sh * 32))
        c = (r << 11) | (g << 5) | b
        cols.append([c if k < rows else 0 for k in range(ROWS)])
    return cols


def model_layer(pwv4_data):
    cols = []
    for col in range(COLS):
        idx = ((col * STEP) & 0xFFFFFFFF) >> 16
        # RGB: AVERAGE the 2-3 PWV4 entries under this pixel (1200 -> 500).
        # Nearest-entry picking let single saturated treble entries
        # (d5 = 127) draw full-height blue/purple spikes (2026-10-11).
        n = max(1, ((((col * STEP) & 0xFFFFFFFF) + STEP) >> 16) - idx)
        sm = [sum(pwv4_data[(idx + j) * 6 + b] for j in range(n)) for b in range(6)]
        if n == 2:
            av = [v >> 1 for v in sm]
        elif n == 3:
            av = [(v * 171) >> 9 for v in sm]        # == v // 3 for v <= 381
        else:
            av = sm
        cols.append(model_column(bytes(av)))
    return cols


def extra_writes():
    """The snapshot routine the cave calls at entry lives in the detail
    area (rgb_detail.COPY5) -- every installer/test must write it too."""
    import rgb_detail
    return [(COPY5 + i, b) for i, b in enumerate(rgb_detail.copy_bytes())]


def plan(mode=1, custom=None):
    """custom: 1-3 RGB565 colours for mode 3 (table written before the mode
    byte); without it mode 3 would recolour with whatever the table holds"""
    code = cave_bytes()
    assert len(code) <= MODE_ADDR - CAVE, f"cave {len(code)} B overlaps mode byte"
    assert len(code) <= CUSTOM_TAB - CAVE, f"cave {len(code)} B overlaps the custom table"
    out = [(CAVE + i, b) for i, b in enumerate(code)]
    if custom:
        out += custom_writes(custom)
    elif mode == MODES["custom"]:
        raise ValueError("custom mode needs its colours")
    out.append((MODE_ADDR, mode))
    out += extra_writes()
    out += [(HOOK_POOL + i, b) for i, b in
            enumerate(struct.pack("<I", CAVE))]
    return code, out


def section_from_file(path, tag):
    b = open(path, "rb").read()
    p = struct.unpack(">I", b[4:8])[0]
    while p + 12 <= len(b):
        h, t = struct.unpack(">II", b[p + 4:p + 12])
        if b[p:p + 4] == tag:
            return b[p + h:p + t]
        if t == 0:
            break
        p += t
    return None


def pwv4_from_file(path):
    b = open(path, "rb").read()
    hl = struct.unpack(">I", b[4:8])[0]
    p = hl
    while p + 12 <= len(b):
        tag = b[p:p + 4]
        h, t = struct.unpack(">II", b[p + 4:p + 12])
        if tag == b"PWV4":
            return b[p + h:p + t]
        if t == 0:
            break
        p += t
    return None


def emu():
    """Apply on the browse-at-track snapshot, push to load, verify."""
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    ckpt = os.environ.get("CKPT", "cache/emu_nav_track.pkl")
    ext = os.environ.get("EXT", os.path.expanduser(
        "~/Downloads/Rekordbox stuff/PIONEER/USBANLZ/P000/0002D83A/"
        "ANLZ0000.EXT"))
    code, _ = plan()
    m = Machine.load(ckpt, core="py")
    c, mem = m.cpu, m.mem
    for i in range(len(code)):
        assert mem.r8(CAVE + i) == 0, f"cave not empty at {CAVE + i:#x}"
    assert mem.r32(HOOK_POOL) == ORIG_RELEASE, "hook pool not stock"
    mode = MODES[os.environ.get("MODE", "rgb")]
    for i, b in enumerate(code):
        mem.w8(CAVE + i, b)
    for a, b in extra_writes():
        mem.w8(a, b)
    mem.w8(MODE_ADDR, mode)
    mem.w32(HOOK_POOL, CAVE)
    seen = []

    def tr(pc, op):
        if pc == CAVE and not seen:
            sp = c.gr(15)
            seen.append((mem.r32(sp), mem.r32(sp + 4), c.gr(4)))
    c.trace = tr
    mem.dev.panel_frame = panl.build_frame(keys=["RotaryPush"])
    c.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    c.run(limit=int(os.environ.get("STEPS", "40000000")))
    c.trace = None
    fb_png(m, "screenshots/rgb_overview_emu_browse.png")
    if not seen:
        print("RESULT: FAIL - cave never entered")
        return 1
    stride, layer, handle = seen[0]
    print(f"cave entered: stride={stride}px layer={layer:#x} handle={handle:#x}")
    if mode == 1 and OVERVIEW_FROM_PWV5:
        want = model_p5(section_from_file(ext, b"PWV5"))
    else:
        want = model_pixels(section_from_file(ext, b"PWV6" if mode == 2 else b"PWV4"), mode)
    bad = 0
    stock = {0x6D1E}
    for col in range(COLS):
        for k in range(ROWS):
            a = layer + ((33 - k) * stride + col) * 2
            got = mem.r16(a & 0x1FFFFFFF)
            if mode == 0:             # must be untouched stock artwork
                exp = got if got in stock or got == 0 or \
                    got not in (C_LOW, C_MID, C_HIGH) else -1
            else:
                exp = want[col][k]
            if got != exp:
                bad += 1
                if bad <= 5:
                    print(f"  col {col} k {k}: got {got:#06x} want {exp:#06x}")
    print(f"layer check: {COLS * ROWS - bad}/{COLS * ROWS} px match model")
    mem.dev.panel_frame = panl.build_frame(keys=["Browse"])
    c.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    c.run(limit=6_000_000)
    fb_png(m, f"screenshots/wave_{os.environ.get('MODE', 'rgb')}_emu.png")
    if os.environ.get("SAVE"):
        m.save(os.environ["SAVE"])
    print("RESULT:", "PASS" if bad == 0 else "FAIL")
    return 0 if bad == 0 else 1


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "emu"
    if mode == "emu":
        return emu()
    if mode == "plan":
        code, out = plan()
        print(f"cave {len(code)} B @ {CAVE:#x}; hook pool {HOOK_POOL:#x} "
              f"{ORIG_RELEASE:#x} -> {CAVE:#x}; {len(out)} bytes total")
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
