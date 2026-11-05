#!/usr/bin/env python3
"""perform_pads.py -- BEAT JUMP pads + KEY/STEMS control on the stock
PERFORM screen (XDJ-700, RAM only, reboot = stock).

Layout (PERFORM screen, 800x480) -- the XDJ-1000 PERFORM layout:
  * HOT CUE / BEAT JUMP / KEY SHIFT / STEMS mode tabs stacked left of the
    hot-cue row (they replace the stock "HOT CUE --" label; the stack sits
    flush inside the row, 232..295). HOT CUE = stock pads, untouched.
  * BEAT JUMP mode: the hot-cue row becomes three pairs
        [<< 1][>> 1] -- [<< 2][>> 2] -- [<< 4][>> 4]
    (stock hot-cue pad artwork at 76 px, << / >> tab on top, number in the
    pad font). Tapping BEAT JUMP again pages 1-2-4 -> 4-8-16 -> 16-32-64.
  * (the top-left [-] NAME [+] box was removed 2026-10-13: KEY SHIFT and
    STEMS tabs do that job)
  * STEMS mode: the hot-cue row becomes five mute pads
        [VOCALS][DRUMS][BASS][OTHER]
    (stock pad art, 112 px). PMASK u8 bit 0..3 = stem muted; tapping a pad
    toggles its bit (MIX pad removed 2026-10-18). ON = orange strip under a
    white label; MUTED = dark hatched pad, orange MUTE chip, dim label.
  * Waveform strip and REV stay stock. Text is anti-aliased
    (tools/aafont.py: pad digits = the firmware's own glyphs).

How (CLAUDE.md §47/§48):
  touch   pool 0x08D5278C (sole user: parser 0x8D5263A) = hit-tester
          FUN_08DD0128(down, x, y, ..). PTOUCH: on PERFORM (*0x0BA371D4==7)
          a NEW press (zone state *0x0C0E75BC==0x1E) on our tabs / KEY area /
          (BEAT JUMP mode) the whole hot-cue row is latched + consumed;
          everything else tail-jumps to the stock fn.
  jump    size rows of actions 1/2 (0x080B7D20 rev / +0x28 fwd, written via
          the uncached alias like the handler reads them) + key bit Rev1
          (byte 94 0x04) / Fwd1 (byte 93 0x80) held while pressed -> stock
          consumer -> stock handler 0x8D4318E (quantize/guards all stock).
  draw    pool 0x08C92E20 (sole user 0x8C92D7C) = 2DG flush FUN_08E7D278.
          The UI-layer present (quad 800x480) -> draw our pads into the
          layer first; the PERFORM waveform strip quad (504x72) -> squash
          it to y 64..101. Sentinels skip redraws when nothing changed.

  perform_pads.py plan   byte counts / addresses
  perform_pads.py emu    emulator test (every pad, paging, key steps)
"""
import os
import struct
import sys

import numpy as np

sys.path[:0] = [".", "tools"]
from shasm import assemble
import rgb_overview as RO
import vredirect as VR
import aafont

PC0 = RO.CAVE + 0x3600              # 0x081DBF00 (image zero run -> 0x081E03EB)
PDRAW = PC0 + 0x5C8  # 0x540 -> 0x5C0: swipe code (2026-10-12); -> 0x5E0 stale-latch fix (2026-10-19); -> 0x5C8: light-ink guards in fillrect/drawaa (2026-10-26) eat the hook gap
# 2026-10-17: incremental BJ paint (+~180 B) -> all units from KSSTEP on
# shifted +0xB0 (cave zero run reaches PC0+0x44EB, still clear).
KSDRAW = PC0 + 0x11F0                   # KEY SHIFT panel (own unit, calls DRAW)
PDATA = PC0 + 0x1820
PMASK = PC0 + 0x1A10                # u8 stem mute mask (bit V/D/B/O) + drawn
PST = PC0 + 0x1A18                  # +0 latch +1 page +2 mode +4 drawn state
GATELATCH = PST + 3                 # u8 stock hot-cue pad held (gate arm)
PSURF, PSTR, PCLIP, PCOL = PST + 8, PST + 12, PST + 16, PST + 20
PTMP, PFONT, PTMPW = PST + 24, PST + 40, PST + 44
PSW = PST + 48                      # swipe: u16 down-x, +2 pending code,
                                    # +3 swiped, +4 release frames left
PADART = PC0 + 0x1A58               # stock hot-cue pad: 63 rows x 22 u16
FONTP = PC0 + 0x2558                # pad font (14-px caps, digits < > + -)
FONTT = PC0 + 0x2C98                # tab font (condensed 9-px caps, << >>)
FONTK = PC0 + 0x37A8                # key-name font (condensed bold 13-px)
# room for Windows fonts too: there Arial stands in for Helvetica Neue and
# the tab/key tables come out ~310 B bigger each (2802 / 3325 B vs 2433 /
# 2920 on a Mac) -- 2026-10-20 a Windows install hit FONTT's old limit
PEND = PC0 + 0x44E8
PDKW = PST + 56                    # u32: &lt_pdk (light add-on's ink flip,
                                    # r2 -> r2, r4-r7 preserved). Default =
                                    # PDKSTUB (bare rts: no flip); the
                                    # LIGHT installer re-arms it
PDKSTUB = PC0 + 0x11EC             # rts;nop in the 4-byte gap kq..KSDRAW
ZERO_END = 0x081E03EB               # the image zero run holding the cave
assert PEND <= ZERO_END, hex(PEND)
HCPAGE = PMASK + 2                  # u8 hot cue page (tools/hotcue8 writes)
HC_BASE = int(os.environ.get("HC_BASE", "0x09790000"), 0)
HC_NEXTP = HC_BASE + 4              # hotcue8 entry table: +4 = hc_next
# 2026-10-12: hot cue pages have no proven-free home yet -> the HOT CUE
# re-tap reads a word that is always zero in our own cave (= do nothing)
SAFE_ZERO = PMASK + 4                # 0x081DD7FC, deck-verified zero
if os.environ.get("HOTCUE8_DISABLE"):        # opt-out: re-tap does nothing
    HC_NEXTP = SAFE_ZERO
REC_X, REC_W = 0x1CC, 0x4E          # stock REC zone 26 (x 460, w 78)
import mysettings as MSET
MSOFF = MSET.MSOFF                  # 1 = MODS OFF -> PERFORM stays stock
# real-time key shift (tools/keyshift_dsp_apply.py): when the BD8 tempo-copy
# store is NOPed and no version file is loaded, KEY SHIFT steps RTKEY
# -6..+6 and writes the tagged Q20 ratio the DSP code reads (CLAUDE.md §95)
RT_NOPSITE, RT_KEYWORD = 0x08D36B3A, 0x09D24DD8
RTKEY, RTMODE = PST + 60, PST + 61      # i8 semitones, u8 1 = real-time panel
PBUSY = PST + 63                        # PP_GUARD: pblit re-entrancy latch
# 2026-10-21 freeze fix (tester: MT off -> key shift -> MT on froze the
# deck): real-time key shift is MT-only by design (the DSP's MT-off
# resampler path is not wrapped), so the tab is INERT while MT is off and
# any MT change resets the key to 0 (the MT-on transition with a key
# armed is the freeze sequence).
MTW = 0x0B6C4308                        # u8 MASTER TEMPO state (1 = on)
MTLAST = PST + 62                       # u8 MT state as of the last flush
# INCREMENTAL PAINT (deck-freeze fix 2026-10-17, CLAUDE.md §109): the whole
# BJ row (~30 nested draw calls) never enters ONE display flush; 2 pads per
# flush via this resume byte. Values: 0 fresh, 1 rowclear+pads 0..1,
# 2 pads 2..3, 3 pads 4..5, 4 finisher (connectors + hint).
# NOTE: offset 54, NOT 62 (§109's draft): byte loads/stores @r14 accept
# disp 0..15 only, and PST+53..59 are free; 62 would be unelectable.
BJNEXT = PST + 54                       # u8 next BJ paint stage
# 2026-10-18: the incremental (2 pads per flush) paint was built on the
# "draw-call burst" theory; the real freeze was an illegal delay slot
# (bsr pdswhint + PC-relative load). With that fixed, paint the whole BEAT
# JUMP row in ONE flush again (no partial rows on screen when the stock UI
# repaints = the "moving numbers" glitch). PP_INCREMENTAL=1 = old behaviour.
ONEFLUSH = not os.environ.get("PP_INCREMENTAL")
BJ_AFTER_START = "pdbj" if ONEFLUSH else "pdend"
BJ_AFTER_STAGE = "pdbj" if ONEFLUSH else "pdmkb"
KSSTEP = PC0 + 0xF60                    # KEY SHIFT step logic (own unit)
                                        # 2026-10-14: moved 0xE60->0xE90 for the
                                        # swipe-hint glyphs (draw unit 2256 B)
KSSTEP_ENTRY = KSSTEP
HCSW = PC0 + 0x1050                      # HOT CUE tab swipe (own unit, 3 entries)
# 2026-10-18: the loaded track's key for the KEY SHIFT panel (own unit in
# the gap after PDATA). The player keeps the key as a UTF-16 string
# ("F#m", "Bm", or Camelot "11A") at TRK_KEYSTR (emulator: follows every
# load; same static data area on the deck).
TRKKEY = PC0 + 0x1954
TRK_KEYSTR = 0x09D30430
HC_PREVP = HC_BASE + 8 if HC_NEXTP != SAFE_ZERO else SAFE_ZERO
RTKEEP = PC0 + 0x10E0                   # per-flush key re-assert (own unit)
                                        # 2026-10-21: +MT watch -> the units
                                        # below moved +0x20/+0x30
KQTAB = PC0 + 0x11B8                    # 13 tagged Q20 words, -6..+6
                                        # (2026-10-25: +0x20 for rtkeep's
                                        # stale-jump-bit cleanup)
PAYDOWN = 0x0B6C7A45                    # PAY+21 bit0: finger on the screen
KQWORDS = [0x00100000 if s == 0 else 0x40000000 | round(2 ** (s / 12) * (1 << 20))
           for s in range(-6, 7)]
VKEY = VR.VSCR + 14                 # trailer byte 14: detected key (0 = ?)
VSEMI = VR.VSCR + 112               # trailer +112: int8 semitones / version

HOOK_TOUCH, ORIG_TOUCH = 0x08D5278C, 0x08DD0128
HOOK_BLIT, ORIG_BLIT = 0x08C92E20, 0x08E7D278
MODEW, ZSTATE = 0x0BA371D4, 0x0C0E75BC
PLAYW = 0x0B6C4264                # play state word (5 = playing)
PLAYPP = 0x0B6C7A7F               # key bitmap byte holding PlayPause 0x04
GATEBAND = (114, 230, 506, 68)    # stock hot-cue row rect (x, y, w, h)
GATEW, GATEH = 506, 68            # pool-constant widths (imm range)
PULSEN = 4000000                  # PlayPause pulse iterations (~8M steps)
MSGATE = MSET.MS + 0x7F8           # u8 1 = hot-cue gate armed (MY SETTINGS)
KEY93, KEY94 = 0x0B6C7A85, 0x0B6C7A86
ROWREV, ROWFWD = 0xA80B7D20, 0xA80B7D28      # actions 1 / 2, uncached alias

HC_Y, HC_H = 232, 63                # stock hot-cue row (A B C REC CALL)
PADW, PAIRGAP = 76, 2
ITEMS = []                          # 3 pairs [<< n][>> n], connectors between
_x = 116
for _g in range(3):
    ITEMS += [(_x, PADW), (_x + PADW + PAIRGAP, PADW)]
    _x += 2 * PADW + PAIRGAP + 19
CONN = (116 + 2 * PADW + PAIRGAP + 4, 116 + 2 * (2 * PADW + PAIRGAP + 19) - 15)
STEMS5 = [(115 + 126 * i, 112) for i in range(4)]  # VOCALS DRUMS BASS OTHER
# (MIX pad removed 2026-10-18 at user request: tap a muted pad to un-mute)
STLBL = ("voc", "drm", "bas", "oth")
PAGES = 3                           # sizes 1<<(2p) .. 1<<(2p+2)
TABW = 30                           # << / >> tab on top of each pad
NUM_DY = 27
TAB_X, TAB_W, TAB_H = 16, 92, 15    # HOT CUE / BEAT JUMP / KEY SHIFT / STEMS
TAB_Y = (232, 248, 264, 280)        # stack flush inside the row (232..295)
TAB_Y0, TAB_Y1 = TAB_Y[0], TAB_Y[1]
KSL, KSM, KSR = (116, 240, 130, 48), (290, 236, 151, 57), (486, 240, 130, 48)
HATCH = ((250, 255, 36, 18), (445, 255, 37, 18))
KS_EDGE, KS_FILL, KS_CHIP, KS_ARROW, KS_LIGHT, KS_DARK, KS_STRIP, KS_HATCH = \
    0xA534, 0x7BCF, 0xBDF7, 0x4208, 0xDEFB, 0x2945, 0xA514, 0x4A69
LBL = (14, 230, 101, 68)            # stock "HOT CUE --" label area (blanked)
ROWCLR = (114, 230, 506, 68)
MKA = (14, 230)                     # marker pixels (value 1, invisible)
MKB = (287, 240)                    # a gap in both BEAT JUMP and KEY SHIFT
# 2026-10-18: witness INSIDE the tab stack (top-left corner pixel of the
# KEY SHIFT tab). The stock UI repaints its "HOT CUE" label over our tabs
# without touching MKA/MKB -> tabs vanished until something else changed.
MKC = (TAB_X, TAB_Y[2])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emu import fwhome as _FWH                       # noqa: E402
PADART_DATA = np.load(_FWH.path("pad_edges.npy"))    # from the user's firmware
K_Y, K_H = 12, 50
K_BOX = ((100, 36), (140, 76), (220, 36))    # [-] name [+]
WHITE, ORANGE, DIM, KFILL, KEDGE, KPRESS = \
    0xFFFF, 0xFB80, 0x6C33, 0x21A8, 0x3A8C, 0x4B0E
TABC, ICON = 0x632C, 0xFFFF
TABEDGE, TABFILL0, TABTXT0, TABFILL1, TABTXT1 = \
    0x4A69, 0x2945, 0x8C71, 0xBDF7, 0x18E3
MASK = 0x07E0F81F

NUMS = ["1", "2", "4", "8", "16", "32", "64"]
STRINGS = {"key": "STEMS", "mix": "MIX", "mi": "-", "pl": "+",
           "hc": "[ HOT CUE ]", "bj": "[ BEAT JUMP ]", "ks": "KEY SHIFT",
           "st": "STEMS", "voc": "VOCALS", "drm": "DRUMS", "bas": "BASS",
           "oth": "OTHER", "mute": "MUTE",
           "ib": "[", "if": "]", "lt": "<", "gt": ">", "reset": "RESET",
           "mtoff": "MT OFF",
           "swl": "[", "swr": "]"}
NOTE_NAMES = ["C", "C#", "D", "E_", "E", "F", "F#", "G", "A_", "A", "B_", "B"]
STRINGS.update({f"k{i}": n for i, n in enumerate(NOTE_NAMES)})
STRINGS.update({f"n{k}": s for k, s in enumerate(NUMS)})


def _data():
    blob, addr = bytearray(), {}
    for k, s in STRINGS.items():
        addr[k] = PDATA + len(blob)
        blob += s.encode() + b"\0"
    while len(blob) % 4:
        blob += b"\0"
    addr["nums"] = PDATA + len(blob)
    for k in range(len(NUMS)):
        blob += struct.pack("<I", addr[f"n{k}"])
    addr["notes"] = PDATA + len(blob)
    for i in range(12):
        blob += struct.pack("<I", addr[f"k{i}"])
    addr["items"] = PDATA + len(blob)
    for x, w in ITEMS:
        blob += struct.pack("<HH", x, w)
    addr["stems"] = PDATA + len(blob)
    for x, w in STEMS5:
        blob += struct.pack("<HH", x, w)
    addr["stlabs"] = PDATA + len(blob)
    for k in STLBL:
        blob += struct.pack("<I", addr[k])
    addr["kb"] = PDATA + len(blob)
    for x, w in K_BOX:
        blob += struct.pack("<HH", x, w)
    assert PDATA + len(blob) <= PMASK, len(blob)
    return bytes(blob), addr


_DATA, A = _data()


# ---------------------------------------------------------------- hooks ----
HOOK_SRC = f"""
ptouch:                     ; = hit-tester(r4 down, r5 x, r6 y, r7 ..)
  tst r4,r4
  bf ptdown
  bra prel                  ; (bt range)
  nop
ptdown:
  mov.l @({PST:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  tst r0,r0
  bf pchk                   ; our pad held: keep consuming
pnew:
  mov.l @({MODEW:#x},pc),r1
  mov.l @r1,r0
  cmp/eq #7,r0              ; PERFORM screen only
  bf porig_n
  mov.l @({MSOFF:#x},pc),r1 ; MODS OFF in MY SETTINGS -> stock
  mov.b @r1,r0
  tst r0,r0
  bf porig_n
  mov.l @({ZSTATE:#x},pc),r1
  mov.l @r1,r0
  cmp/eq #30,r0             ; stock hit-tester idle = a NEW press
  bf porig_n
  sts.l pr,@-r15
  mov.l r4,@-r15
  mov.l r5,@-r15
  mov.l r6,@-r15
  mov.l r7,@-r15
  bsr phit
  nop
  mov.l @r15+,r7
  mov.l @r15+,r6
  mov.l @r15+,r5
  mov.l @r15+,r4
  lds.l @r15+,pr
  tst r0,r0
  bt porig_n
  mov r0,r2                 ; hit code
  mov.l @({PST:#x},pc),r1
  mov.b @(2,r1),r0          ; BEAT JUMP mode + a jump pad (1..14)?
  cmp/eq #1,r0
  bf phct
  mov r2,r0
  mov #15,r3                ; every jump pad 1..14 (incl. the 64s)
  cmp/hs r3,r0
  bt platch
  mov #51,r0                ; swipe-or-tap pending: decided on move/release
  mov.b r0,@r1
  mov.l @({PSW:#x},pc),r3
  mov r2,r0
  mov.b r0,@(2,r3)          ; pending jump code
  mov.w r5,@r3              ; touch-down x
  mov #0,r0
  mov.b r0,@(3,r3)          ; not swiped yet
  mov.b r0,@(4,r3)
  rts
  nop
phct:                       ; HOT CUE mode + HOT CUE tab: swipe-or-tap
  tst r0,r0                 ; pending (HCSW unit; latch 52)
  bf platch
  mov r2,r0
  cmp/eq #40,r0
  bf platch
  mov.l @({HCSW:#x},pc),r3
  jmp @r3
  nop
platch:
  mov r2,r0
  mov.b r0,@r1              ; latch
  bra pact
  nop
porig_n:                    ; trampoline (bt/bf range)
  bra porig
  nop
pchk:                       ; r0 = latch (r1 = PST), finger down
  mov #15,r2
  cmp/hs r2,r0
  bt pheld                  ; pending / tab / key codes: really held
  mov r0,r2
  mov.l @({PSW:#x},pc),r3
  mov.b @(4,r3),r0          ; jump code 1..14: release countdown left?
  tst r0,r0
  mov r2,r0                 ; (keeps T)
  bf pheld
  mov #0,r0                 ; countdown over = a STALE jump latch (the
  mov.b r0,@r1              ; hit-tester stopped before it cleared):
  bra pnew                  ; drop it and take this as a NEW press
  nop                       ; (else the old jump re-fired, the new one
                            ; was lost: "32/64 sometimes do nothing")
pheld:                      ; r0 = latch (r1 = PST)
  cmp/eq #51,r0             ; pending beat-jump touch: swipe pages sizes
  bf phl52
  mov.l @({PSW:#x},pc),r3
  mov.b @(3,r3),r0
  tst r0,r0
  bf pret                   ; one page step per swipe
  mov.w @r3,r0
  extu.w r0,r0
  sub r5,r0                 ; r0 = down x - x (> 0 = moved left)
  mov #48,r2
  cmp/gt r2,r0
  bt pswl
  neg r0,r0
  cmp/gt r2,r0
  bt pswr
  bra pret
  nop
pswl:                       ; swipe left: bigger sizes (next page)
  mov.b @(1,r1),r0
  cmp/eq #2,r0
  bt pswd
  add #1,r0
  bra pswset
  nop
pswr:                       ; swipe right: smaller sizes
  mov.b @(1,r1),r0
  tst r0,r0
  bt pswd
  add #-1,r0
pswset:
  mov.b r0,@(1,r1)
pswd:
  mov #1,r0
  rts
  mov.b r0,@(3,r3)
phl52:                      ; pending HOT CUE tab touch: swipe pages cues
  cmp/eq #52,r0
  bf pheld_old
  mov.l @({HCSW + 4:#x},pc),r3
  jmp @r3
  nop
pheld_old:                  ; r0 = latch: re-assert the jump key bit
  mov #15,r2
  cmp/hs r2,r0
  bt pret
  add #-1,r0
  tst #1,r0
  bt phr
  mov.l @({KEY93:#x},pc),r1
  mov.b @r1,r0
  or #128,r0
  rts
  mov.b r0,@r1
phr:
  mov.l @({KEY94:#x},pc),r1
  mov.b @r1,r0
  or #4,r0
  rts
  mov.b r0,@r1
pret:
  rts
  nop
prel:                       ; release: drop our latch + key bits
  mov.l @({PST:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  tst r0,r0
  bt pgate                    ; our latch clear: maybe a gated pad
  cmp/eq #51,r0
  bf prl52
  mov.l @({PSW:#x},pc),r3
  mov.b @(3,r3),r0            ; swiped: no jump
  tst r0,r0
  bf prel_clr
  mov.b @(2,r3),r0            ; a tap: jump now
  extu.b r0,r0
  mov.b r0,@r1                ; latch = jump code
  mov r0,r2
  mov #4,r0
  mov.b r0,@(4,r3)            ; keep the key bit up 4 more frames (the
                              ; hit-tester only runs ~4 frames after a
                              ; release; more would block the next tap)
  mov r2,r0
  bra pjump
  nop
prl52:
  cmp/eq #52,r0
  bf prel_cd
  mov.l @({HCSW + 8:#x},pc),r3
  jmp @r3
  nop
prel_cd:                      ; jump latch: hold the key bit a few frames
  mov r0,r2
  mov.l @({PSW:#x},pc),r3
  mov.b @(4,r3),r0
  tst r0,r0
  bt prel_clr
  add #-1,r0
  mov.b r0,@(4,r3)
  mov r2,r0
  bra pheld_old
  nop
prel_clr:
  mov #0,r0
  mov.b r0,@r1
  mov.l @({KEY93:#x},pc),r1
  mov.b @r1,r0
  and #127,r0
  mov.b r0,@r1
  mov.l @({KEY94:#x},pc),r1
  mov.b @r1,r0
  and #251,r0
  mov.b r0,@r1
  bra porig
  nop
pgate:                      ; gated stock hot-cue pad released?
  mov.l @({GATELATCH:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bt porig
  mov #0,r0
  mov.b r0,@r1              ; disarm
  mov.l @({PLAYW:#x},pc),r1
  mov.l @r1,r0
  cmp/eq #5,r0
  bf porig                  ; not playing: nothing to pause
  mov.l @({PLAYPP:#x},pc),r1
  mov.b @r1,r0
  mov #4,r2
  or r2,r0
  mov.b r0,@r1              ; press PlayPause...
  mov.l @({PULSEN:#x},pc),r2
ppulse:
  dt r2
  bf ppulse
  mov.b @r1,r0
  and #251,r0
  mov.b r0,@r1              ; ...hold ~16 ms, release (= pause)
porig:
  tst r4,r4                 ; release frames: nothing to arm
  bt porig_go
  mov.l @({MODEW:#x},pc),r0 ; PERFORM screen only
  mov.l @r0,r0
  cmp/eq #7,r0
  bf porig_go
  mov.l @({MSOFF:#x},pc),r0 ; MODS OFF -> stock
  mov.b @r0,r0
  tst r0,r0
  bf porig_go
  mov.l @({MSGATE:#x},pc),r0 ; gate OFF -> stock
  mov.b @r0,r0
  tst r0,r0
  bt porig_go
  mov r5,r0                 ; x in [114, 620)?
  add #-114,r0
  mov.l @({GATEW:#x},pc),r1
  cmp/hs r1,r0
  bt porig_go
  mov r6,r0                 ; y in [230, 298)?
  add #-100,r0
  add #-100,r0
  add #-30,r0
  mov.l @({GATEH:#x},pc),r1
  cmp/hs r1,r0
  bt porig_go
  mov.l @({GATELATCH:#x},pc),r0
  mov #1,r1
  mov.b r1,@r0              ; arm: stock still triggers below
porig_go:
  mov.l @({ORIG_TOUCH:#x},pc),r0
  jmp @r0
  nop
phit:                       ; r5 x, r6 y -> r0 code (0 none); r0-r4, r7
  mov r5,r2                 ; mode tabs (always ours on PERFORM)
  add #-{TAB_X},r2
  mov #{TAB_W},r3
  cmp/hs r3,r2
  bt phrow
  mov.l @({TAB_Y0:#x},pc),r3
  mov r6,r2
  sub r3,r2
  mov #{4 * (TAB_H + 1)},r3
  cmp/hs r3,r2
  bt phrow
  mov #{TAB_H + 1},r3
  cmp/hs r3,r2
  bt pht1
  nop
  rts
  mov #40,r0
pht1:
  mov #{2 * (TAB_H + 1)},r3
  cmp/hs r3,r2
  bt pht2
  nop
  rts
  mov #41,r0
pht2:
  mov #{3 * (TAB_H + 1)},r3
  cmp/hs r3,r2
  bt pht3
  nop
  rts
  mov #42,r0
pht3:
  rts
  mov #43,r0
phrow:                      ; BEAT JUMP mode: the whole hot-cue row is ours
  mov.l @({PST:#x},pc),r1
  mov.b @(2,r1),r0
  tst r0,r0
  bt phhc
  mov.l @({HC_Y:#x},pc),r3
  mov r6,r2
  sub r3,r2
  mov #{HC_H},r3
  cmp/hs r3,r2
  bt phk
  mov.b @(2,r1),r0
  cmp/eq #2,r0
  bt phks
  cmp/eq #3,r0
  bt phst
  mov.l @({A["items"]:#x},pc),r1
  mov #0,r4
phl:
  mov.w @r1+,r2             ; item x
  extu.w r2,r2
  mov.w @r1+,r3             ; item w
  extu.w r3,r3
  mov r5,r0
  sub r2,r0
  cmp/hs r3,r0
  bf phb
  add #1,r4
  mov #6,r0
  cmp/eq r0,r4
  bf phl
  nop
  rts
  mov #50,r0                ; gap inside the row: swallow (no stock hot cue)
phhc:                       ; HOT CUE mode: block REC on hot cue pages
  mov.l @({HCPAGE:#x},pc),r1 ; 2/3 (it would overwrite A-C on the stick)
  mov.b @r1,r0
  tst r0,r0
  bt phk
  mov.l @({HC_Y:#x},pc),r3
  mov r6,r2
  sub r3,r2
  mov #{HC_H},r3
  cmp/hs r3,r2
  bt phk
  mov.l @({REC_X:#x},pc),r3
  mov r5,r2
  sub r3,r2
  mov #{REC_W},r3
  cmp/hs r3,r2
  bt phk
  nop
  rts
  mov #50,r0
phb:                        ; r4 = pad: k = 2p + i/2, code 1 + 2k + (i&1)
  mov.l @({PST:#x},pc),r1
  mov.b @(1,r1),r0
  extu.b r0,r7
  shll r7
  mov r4,r0
  shlr r0
  add r7,r0
  shll r0
  add #1,r0
  mov r4,r2
  mov #1,r3
  and r3,r2
  rts
  add r2,r0
phks:                       ; KEY SHIFT: [< key-1] [key RESET] [key+1 >]
  mov r5,r2
  add #-{KSL[0]},r2
  mov.l @({KSL[2]:#x},pc),r3
  cmp/hs r3,r2
  bt phks1
  nop
  rts
  mov #60,r0
phks1:
  mov.l @({KSM[0]:#x},pc),r3
  mov r5,r2
  sub r3,r2
  mov.l @({KSM[2]:#x},pc),r3
  cmp/hs r3,r2
  bt phks2
  nop
  rts
  mov #61,r0
phks2:
  mov.l @({KSR[0]:#x},pc),r3
  mov r5,r2
  sub r3,r2
  mov.l @({KSR[2]:#x},pc),r3
  cmp/hs r3,r2
  bt phks3
  nop
  rts
  mov #62,r0
phks3:
  rts
  mov #50,r0
phst:                       ; STEMS mode: 4 pads -> code 30..33, gap 50
  mov.l @({A["stems"]:#x},pc),r1
  mov #0,r4
phstl:
  mov.w @r1+,r2             ; pad x
  extu.w r2,r2
  mov.w @r1+,r3             ; pad w
  extu.w r3,r3
  mov r5,r0
  sub r2,r0
  cmp/hs r3,r0
  bf phsth
  add #1,r4
  mov #{len(STEMS5)},r0
  cmp/eq r0,r4
  bf phstl
  nop
  rts
  mov #50,r0                ; gap inside the row: swallow
phsth:
  mov r4,r0
  rts
  add #30,r0
phk:                        ; top-left [-] NAME [+] box REMOVED (user,
  bra phn                   ; 2026-10-13): touches there stay stock; the
  nop                       ; KEY SHIFT tab does keys, STEMS tab stems
  mov r6,r2
  add #-{K_Y},r2
  mov #{K_H},r3
  cmp/hs r3,r2
  bt phn
  mov r5,r2
  add #-{K_BOX[0][0]},r2
  mov #{K_BOX[0][1]},r3
  cmp/hs r3,r2
  bt phk2
  nop
  rts
  mov #20,r0
phk2:
  mov.l @({K_BOX[2][0]:#x},pc),r3
  mov r5,r2
  sub r3,r2
  mov #{K_BOX[2][1]},r3
  cmp/hs r3,r2
  bt phn
  nop
  rts
  mov #21,r0
phn:
  rts
  mov #0,r0
pact:                       ; r0 = new latch code; returns to the parser
  mov #60,r3
  cmp/hs r3,r0
  bf pa_n60
  bra paks
  nop
pa_n60:
  mov #50,r3
  cmp/hs r3,r0
  bt paret_t
  mov #40,r3
  cmp/hs r3,r0
  bt pamode
  mov #30,r3
  cmp/hs r3,r0
  bt pastm_t
  mov #20,r3
  cmp/hs r3,r0
  bt pakey_t
  nop
  bra pjump
  nop
pakey_t:
  bra pakey
  nop
paret_t:                    ; trampolines (bt range)
  bra paret
  nop
pastm_t:
  bra pastm
  nop
pjump:
  add #-1,r0
  mov r0,r2
  shlr r2                   ; k
  and #1,r0                 ; 1 = forward
  mov #1,r3
  shld r2,r3                ; beats = 1 << k
  tst r0,r0
  bt parev
  mov.l @({ROWFWD:#x},pc),r1
  mov.l r3,@r1
  mov.l @({KEY93:#x},pc),r1
  mov.b @r1,r0
  or #128,r0
  rts
  mov.b r0,@r1
parev:
  mov.l @({ROWREV:#x},pc),r1
  mov.l r3,@r1
  mov.l @({KEY94:#x},pc),r1
  mov.b @r1,r0
  or #4,r0
  rts
  mov.b r0,@r1
pamode:                     ; 40 HOT CUE: tap = mode change ONLY
                            ; (2026-10-13: paging by SWIPE on the tab row)
  mov.l @({PST:#x},pc),r1
  cmp/eq #40,r0
  bf pam1
  mov.b @(2,r1),r0          ; already HOT CUE: nothing (swipes page cues)
  tst r0,r0
  bt pam40x
  mov #0,r0
  rts
  mov.b r0,@(2,r1)          ; from another mode: switch to HOT CUE
pam40x:
  rts
  mov #0,r0
.pool
pam1:
  cmp/eq #42,r0
  bf pam43
  mov #2,r0
  rts
  mov.b r0,@(2,r1)
pam43:                      ; 43 = STEMS tab: mode 3 (41 falls through)
  cmp/eq #43,r0
  bf pam41
  nop
  mov #3,r0
  rts
  mov.b r0,@(2,r1)
pam41:
  mov.b @(2,r1),r0
  cmp/eq #1,r0
  bt pampg
  mov #1,r0
  rts
  mov.b r0,@(2,r1)
pampg:
  mov.b @(1,r1),r0
  add #1,r0
  cmp/eq #{PAGES},r0
  bf pps
  mov #0,r0
pps:
  rts
  mov.b r0,@(1,r1)
paks:                       ; 60 key down, 61 reset, 62 key up -> KSSTEP unit
  mov.l @({KSSTEP_ENTRY:#x},pc),r1
  jmp @r1
  nop
pakey:                      ; 20 = '-', 21 = '+': step VSEL over 0..n
  mov r0,r3
  mov.l @({VR.VTBL + 8:#x},pc),r1
  mov.l @r1,r2
  extu.w r2,r2              ; n exact (high half is the keycode)
  tst r2,r2
  bt paret
  mov.l @({VR.VSEL:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  mov #21,r7
  cmp/eq r7,r3
  bf pminus
  add #1,r0
  cmp/hi r2,r0
  bf pstore
  nop
  bra pstore
  mov #0,r0
pminus:
  tst r0,r0
  bf pdec
  nop
  bra pstore
  mov r2,r0
pdec:
  add #-1,r0
pstore:
  mov.b r0,@r1
paret:
  rts
  nop
pastm:                      ; 30..33 toggle a stem mute bit, 34 MIX resets
  mov.l @({PMASK:#x},pc),r1
  mov r0,r2
  add #-34,r2
  tst r2,r2
  bt pstmreset
  nop
  mov r0,r2
  add #-30,r2
  mov #1,r3
  shld r2,r3                ; 1 << stem
  mov.b @r1,r0
  xor r3,r0
  rts
  mov.b r0,@r1
pstmreset:
  mov #0,r0
  rts
  mov.b r0,@r1
pblit:                      ; = FUN_08E7D278(r4 dev, r5 req, ..)
  mov.l @({PBUSY:#x},pc),r1 ; PP_GUARD: re-entrancy latch. Our painter
  mov.b @r1,r0              ; runs from THIS hook, and our own draws
  extu.b r0,r0              ; re-enter the hooked blit pool; a second
  tst r0,r0                 ; entry while one is in flight used to run
  bf pbrec                  ; RTKEEP + the list filter + pdraw again.
  mov #1,r0                 ; On hardware that interleaving is the
  mov.b r0,@r1              ; freeze suspect (every ELEMENT of the BJ
                            ; row was exonerated 2026-10-17; only
                            ; skipping the whole heavy row is clean).
                            ; Re-entrant calls now fall straight
                            ; through to the stock blit (pbrec).
  sts.l pr,@-r15            ; real-time key: re-assert the DSP key word
  mov.l @({RTKEEP:#x},pc),r0
  jsr @r0
  nop
  lds.l @r15+,pr
  mov.l @r5,r1              ; display list (uncached)
  mov.l @(52,r1),r0
  cmp/eq #76,r0             ; w[13] == 0x4C (WPR src)
  bf pbo
  mov.l @(56,r1),r2         ; src layer
  add #64,r1
  mov.l @r1,r0
  cmp/eq #88,r0             ; w[16] == 0x58 (WPR src stride)
  bf pbo
  mov.l @(4,r1),r3          ; stride px
  mov.l @(44,r1),r0         ; w[27] op
  shlr16 r0
  shlr8 r0
  mov #-126,r7
  extu.b r7,r7
  cmp/eq r7,r0              ; 0x82 textured quad
  bf pbo
  mov.l @(52,r1),r0         ; w[29] size
  mov.l @({0x032001E0:#x},pc),r7
  cmp/eq r7,r0
  bf pbo
  mov.l @({MODEW:#x},pc),r1
  mov.l @r1,r0
  cmp/eq #7,r0
  bf pbo
  mov.l @({MSOFF:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bf pbo
  mov.l @({0x1FFFFFFF:#x},pc),r0
  and r0,r2
  mov.l @({0xA0000000:#x},pc),r0
  or r0,r2
  mov.l @({PSURF:#x},pc),r1
  mov.l r2,@r1
  add r3,r3
  mov.l r3,@(4,r1)          ; PSTR bytes
  sts.l pr,@-r15
  mov.l r4,@-r15
  mov.l r5,@-r15
  mov.l r6,@-r15
  mov.l r7,@-r15
  mov.l @({PDRAW:#x},pc),r0
  jsr @r0
  nop
  mov.l @r15+,r7
  mov.l @r15+,r6
  mov.l @r15+,r5
  mov.l @r15+,r4
  lds.l @r15+,pr
  bra pbo
  nop
pbo:
  mov.l @({PBUSY:#x},pc),r1 ; release the latch (all normal exits run
  mov #0,r0                 ; through here); then the stock blit
  mov.b r0,@r1
  mov.l @({ORIG_BLIT:#x},pc),r0
  jmp @r0
  nop
pbrec:                      ; re-entrant entry (latch busy): stock only
  mov.l @({ORIG_BLIT:#x},pc),r0
  jmp @r0
  nop
"""

# ----------------------------------------------------------------- draw ----
DRAW_SRC = f"""
pdraw:
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov.l r12,@-r15
  mov.l r13,@-r15
  mov.l r14,@-r15
  mov.l @({PST:#x},pc),r14
  mov.b @(2,r14),r0         ; state = latch|vsel<<8|n<<16|page<<24|mode<<28
  extu.b r0,r13
  shll2 r13
  shll2 r13
  mov.b @(1,r14),r0
  extu.b r0,r0
  or r0,r13
  shll16 r13
  shll8 r13
  mov.l @({VKEY:#x},pc),r1
  mov.b @r1,r0
  mov.l @({RTKEY:#x},pc),r1  ; real-time key change -> redraw
  mov.b @r1,r2
  xor r2,r0
  extu.b r0,r0
  shll16 r0
  or r0,r13
  mov.l @({MTLAST:#x},pc),r1 ; MT change -> redraw (MT OFF strip/chips);
  mov.b @r1,r0               ; shares byte 2 with page+xor (a simultaneous
  extu.b r0,r0               ; page+MT flip could mask one repaint: cosmetic)
  shll16 r0
  or r0,r13
  mov.l @({VR.VTBL + 8:#x},pc),r1
  mov.l @r1,r0
  and #15,r0
  and #15,r0
  shll2 r0
  shll2 r0
  mov.l @({VR.VSEL:#x},pc),r1
  mov.b @r1,r2
  extu.b r2,r2
  or r2,r0
  shll8 r0
  or r0,r13
  mov.b @r14,r0
  extu.b r0,r0
  or r0,r13
  mov #{MKA[0]},r4          ; marker A: cleared by any stock repaint
  mov.l @({MKA[1]:#x},pc),r5
  bsr pxaddr
  nop
  mov.w @r0,r1
  extu.w r1,r1
  mov #1,r2
  cmp/eq r2,r1
  bf pdgo
  mov #{MKC[0]},r4          ; tab-stack witness: stock label repaint?
  mov.l @({MKC[1]:#x},pc),r5
  bsr pxaddr
  nop
  mov.w @r0,r1
  extu.w r1,r1
  mov #1,r2
  cmp/eq r2,r1
  bf pdgo
  mov.l @(4,r14),r1
  cmp/eq r13,r1
  bf pdres
  mov.l @({PMASK:#x},pc),r1
  mov.b @(1,r1),r0           ; drawn mask
  extu.b r0,r0
  mov.b @r1,r2               ; stem mask changed -> redraw too
  extu.b r2,r2
  cmp/eq r2,r0
  bf pdres
  mov.b @(2,r14),r0
  tst r0,r0
  bt pdskip
  mov.l @({MKB[0]:#x},pc),r4 ; marker B (BEAT JUMP mode): hot-cue pad repaint
  mov.l @({MKB[1]:#x},pc),r5
  bsr pxaddr
  nop
  mov.w @r0,r1
  extu.w r1,r1
  mov #1,r2
  cmp/eq r2,r1
  bf pdgo
  nop
pdskip:
  mov.b @(2,r14),r0          ; BEAT JUMP mode only: mid-cycle resume
  extu.b r0,r0
  mov #1,r1
  cmp/eq r1,r0
  bf pdskip2
  nop
  mov.l @({BJNEXT:#x},pc),r1  ; next != 0 -> continue the row (cheap units)
  mov.b @r1,r0
  extu.b r0,r0
  tst r0,r0
  bt pdskip2
  nop
  bra pdgo
  nop
pdskip2:
  bra pdend
  nop
pdres:
  mov #0,r0
  mov.l @({BJNEXT:#x},pc),r1
  mov.b r0,@r1               ; fresh redraw: the BJ cycle restarts next flush
  bra pdgo
  nop
pdgo:
  mov.l r13,@(4,r14)
  mov.l @({PMASK:#x},pc),r1
  mov.b @r1,r0
  mov.b r0,@(1,r1)           ; remember the drawn mask
  mov.l @({BJNEXT:#x},pc),r1  ; mid-cycle flush: row units only (label/tabs
  mov.b @r1,r0               ; are proven costs; 2 pads never in one flush;
  tst r0,r0                  ; stage 0..4 positive -> no extu needed)
  bt pdjfresh                ; 2026-10-18: was a nop -> a fresh BEAT JUMP
                             ; redraw never painted the tabs ("left tabs
                             ; disappear" on the deck, R2-R7)
  mov.b @(2,r14),r0          ; BEAT JUMP mode still? A stale resume in a
  extu.b r0,r0               ; foreign mode would paint the row off-mode
  mov #1,r2
  cmp/eq r2,r0
  bf pdjre
  nop
  bra pdbj
  nop
pdjre:
  mov #0,r0
  mov.b r0,@r1               ; stale mid-cycle in a foreign mode: restart
  bra pdjfresh
  nop
pdjfresh:
  mov.l @({PCOL:#x},pc),r1  ; blank the stock "HOT CUE --" label
  mov #0,r0
  mov.l r0,@r1
  mov #{LBL[0]},r4
  mov.l @({LBL[1]:#x},pc),r5
  mov #{LBL[2]},r6
  bsr fillrect
  mov #{LBL[3]},r7
  mov #{MKA[0]},r4
  mov.l @({MKA[1]:#x},pc),r5
  bsr pxaddr
  nop
  mov #1,r1
  mov.w r1,@r0
  mov.l @({PFONT:#x},pc),r1
  mov.l @({FONTT:#x},pc),r0
  mov.l r0,@r1
  mov.b @(2,r14),r0         ; tabs: HOT CUE / BEAT JUMP / KEY SHIFT / STEMS
  extu.b r0,r0
  mov r0,r12
  mov #{TAB_X},r4
  mov.l @({TAB_Y[0]:#x},pc),r5
  mov.l @({A["hc"]:#x},pc),r6
  mov r12,r0
  cmp/eq #0,r0
  bsr drawtab
  movt r7
  mov #{TAB_X},r4
  mov.l @({TAB_Y[1]:#x},pc),r5
  mov.l @({A["bj"]:#x},pc),r6
  mov r12,r0
  cmp/eq #1,r0
  bsr drawtab
  movt r7
  mov #{TAB_X},r4
  mov.l @({TAB_Y[2]:#x},pc),r5
  mov.l @({A["ks"]:#x},pc),r6
  mov r12,r0
  cmp/eq #2,r0
  bsr drawtab
  movt r7
  mov #{TAB_X},r4
  mov.l @({TAB_Y[3]:#x},pc),r5
  mov.l @({A["st"]:#x},pc),r6
  mov r12,r0
  cmp/eq #3,r0
  bsr drawtab
  movt r7
  mov #{MKC[0]},r4          ; tab-stack witness (see MKC)
  mov.l @({MKC[1]:#x},pc),r5
  bsr pxaddr
  nop
  mov #1,r1
  mov.w r1,@r0
  tst r12,r12
  bt pdhc                   ; HOT CUE mode: only KEY area + markers
  nop
pdnot0:
  mov r12,r0
  cmp/eq #2,r0
  bt pdkeys2
  cmp/eq #3,r0
  bt pdstems
  nop
  bra pdbj                  ; BEAT JUMP mode
  nop
pdhc:                       ; HOT CUE mode (swipe arrows live in the tabs;
  bra pdkeys                ; the old label hint overlapped the tab stack)
  nop
.pool
pdkeys2:                    ; KEY SHIFT mode
  bsr rowclear
  nop
  mov.l @({KSDRAW:#x},pc),r0
  jsr @r0
  nop
  bra pdmkb
  nop
pdstems:                    ; STEMS mode: VOCALS DRUMS BASS OTHER MIX pads
  bsr rowclear
  nop
  mov.l @({PMASK:#x},pc),r1
  mov.b @r1,r10
  extu.b r10,r10
  mov #0,r12                ; pad i
pstpad:
  mov.l @({A["stems"]:#x},pc),r1
  mov r12,r0
  shll2 r0
  add r0,r1
  mov.w @r1,r8              ; x
  mov.w @(2,r1),r0
  mov r0,r9                 ; w
  mov.b @r14,r2             ; pressed rim: latch == 30+i?
  extu.b r2,r2
  mov r12,r0
  add #30,r0
  cmp/eq r2,r0
  bf pstnp
  mov.l @({PCOL:#x},pc),r1
  mov.l @({ORANGE:#x},pc),r0
  mov.l r0,@r1
  mov r8,r4
  add #-1,r4
  mov.l @({HC_Y:#x},pc),r5
  add #-1,r5
  mov r9,r6
  add #2,r6
  bsr fillrect
  mov #65,r7
pstnp:
  mov r8,r4
  mov.l @({HC_Y:#x},pc),r5
  bsr padart
  mov r9,r6
  mov r12,r0                ; MIX (i=4)?
  cmp/eq #4,r0
  bf pstmbit
  tst r10,r10               ; MIX: lit iff mask==0
  bt pstmon
  nop
  bra pstmmute
  nop
pstmbit:
  mov r10,r2
  mov r12,r3
  neg r3,r3
  shld r3,r2                ; r2 = mask >> i
  shlr r2                   ; T = bit i of the mask (1 = muted)
  bf pstmon
pstmmute:                   ; dark hatched pad + MUTE chip + dim label
  mov.l @({PCOL:#x},pc),r1
  mov.l @({KS_DARK:#x},pc),r0
  mov.l r0,@r1
  mov r8,r4
  add #1,r4
  mov.l @({HC_Y + 1:#x},pc),r5
  mov r9,r6
  add #-2,r6
  bsr fillrect
  mov #61,r7
  mov.l @({PCOL:#x},pc),r1
  mov.l @({KS_HATCH:#x},pc),r0
  mov.l r0,@r1
  mov r8,r11
  add #3,r11
psth:
  mov r11,r4
  mov.l @({HC_Y + 3:#x},pc),r5
  mov #2,r6
  mov #57,r7
  bsr fillrect
  nop
  add #4,r11
  mov r8,r0
  add r9,r0
  add #-3,r0
  cmp/ge r0,r11
  bf psth
  nop
  mov.l @({PCOL:#x},pc),r1  ; MUTE chip
  mov.l @({ORANGE:#x},pc),r0
  mov.l r0,@r1
  mov r9,r4
  shlr r4
  add r8,r4
  add #-19,r4
  mov.l @({HC_Y + 3:#x},pc),r5
  mov #38,r6
  bsr fillrect
  mov #12,r7
  mov.l @({PFONT:#x},pc),r1
  mov.l @({FONTT:#x},pc),r0
  mov.l r0,@r1
  mov.l @({A["mute"]:#x},pc),r6
  bsr aawidth
  nop
  mov r0,r4
  shlr r4
  mov r9,r0
  shlr r0
  add r8,r0
  sub r4,r0
  mov r0,r4
  mov.l @({HC_Y + 5:#x},pc),r5
  mov.l @({A["mute"]:#x},pc),r6
  mov.l @({TABTXT1:#x},pc),r7
  bsr drawaa
  nop
  mov.l @({DIM:#x},pc),r13  ; NOT in the bra slot: a PC-relative load in a
  bra pstlab                ; delay slot differs on the real SH-4 (deck crash
  nop                       ; on the STEMS tab, 2026-10-12)
pstmon:                     ; orange strip + white label
  mov.l @({PCOL:#x},pc),r1
  mov.l @({ORANGE:#x},pc),r0
  mov.l r0,@r1
  mov r8,r4
  add #6,r4
  mov.l @({HC_Y + 51:#x},pc),r5
  mov r9,r6
  add #-12,r6
  bsr fillrect
  mov #5,r7
  mov.l @({WHITE:#x},pc),r13  ; NOT in the bra slot: a PC-relative load in a
  bra pstlab                ; delay slot differs on the real SH-4 (deck crash
  nop                       ; on the STEMS tab, 2026-10-12)
pstlab:                     ; label centred on the pad, colour r13
  mov.l @({A["stlabs"]:#x},pc),r1
  mov r12,r0
  shll2 r0
  add r0,r1
  mov.l @r1,r11
  mov.l @({PFONT:#x},pc),r1
  mov.l @({FONTT:#x},pc),r0
  mov.l r0,@r1
  mov r11,r6
  bsr aawidth
  nop
  mov r9,r4
  sub r0,r4
  shlr r4
  add r8,r4
  mov.l @({HC_Y + NUM_DY:#x},pc),r5
  mov r11,r6
  bsr drawaa
  mov r13,r7
  add #1,r12
  mov #{len(STEMS5)},r0
  cmp/eq r0,r12
  bt pstdone
  nop
  bra pstpad
  nop
pstdone:
  bra pdmkb
  nop
pdbj:
  mov.l @({BJNEXT:#x},pc),r5 ; stage in BJNEXT: 0 fresh (label/tabs only),
  mov.b @r5,r0              ; 1 rowclear + pads 0..1, 2 pads 2..3,
                             ; 3 pads 4..5, 4 finisher. 2 pads per flush ->
  mov #4,r1                 ; the whole row fills in ~5 frames (~80 ms).
  cmp/eq r1,r0
  bt pdtfar                 ; far-jump trampoline (bt is +-128 B only)
  nop
  tst r0,r0
  bt pdbstart
  nop
  mov r0,r1                 ; stages 1-3: start pad = 2*(stage-1)
  shll r0
  add #-2,r0
  mov r0,r12
  mov #1,r2
  cmp/eq r2,r1
  bt pdbrowc
  nop
  bra pdpad
  nop
pdbstart:
  mov.l @({MKB[0]:#x},pc),r4 ; paint the marker-B pixel NOW: the gate only
  mov.l @({MKB[1]:#x},pc),r5 ; admits mid-cycle flushes when it reads 1, so
  bsr pxaddr
  nop
  mov #1,r1
  mov.w r1,@r0               ; leaving it to the finisher would deadlock the
                             ; cycle (every flush looked fresh-marker-fail)
  mov #1,r0
  mov.l @({BJNEXT:#x},pc),r5
  mov.b r0,@r5              ; row work starts on the NEXT flush (r5 = BJNEXT)
  bra {BJ_AFTER_START}       ; ONEFLUSH: straight on (whole row this flush)
  nop
pdtfar:
  bra pdbtail
  nop
pdbrowc:
  bsr rowclear              ; BEAT JUMP mode: blank the hot-cue row (once)
  nop
  mov #0,r12                ; pad i (fresh full cycle: pads 0..5)
pdpad:
  mov.l @({A["items"]:#x},pc),r1
  mov r12,r0
  shll2 r0
  add r0,r1
  mov.w @r1,r8              ; x
  mov.w @(2,r1),r0
  mov r0,r9                 ; w
  mov r8,r4
  mov.l @({HC_Y:#x},pc),r5
  bsr padart
  mov r9,r6
  mov r12,r0                ; tab: back -> right end, forward -> left end
  tst #1,r0
  bf pdtf
  mov r8,r10
  add r9,r10
  add #-{TABW + 3},r10
  mov.l @({A["ib"]:#x},pc),r11
  bra pdtd
  nop
pdtf:
  mov r8,r10
  add #3,r10
  mov.l @({A["if"]:#x},pc),r11
pdtd:
  mov.l @({PCOL:#x},pc),r1
  mov.l @({TABC:#x},pc),r0
  mov.l r0,@r1
  mov r10,r4
  mov.l @({HC_Y + 4:#x},pc),r5
  mov #{TABW},r6
  bsr fillrect
  mov #11,r7
  mov.l @({PFONT:#x},pc),r1
  mov.l @({FONTT:#x},pc),r0
  mov.l r0,@r1
  bsr aawidth
  mov r11,r6
  mov #{TABW},r4
  sub r0,r4
  shlr r4
  add r10,r4
  mov.l @({HC_Y + 5:#x},pc),r5
  mov r11,r6
  mov.l @({ICON:#x},pc),r7
  bsr drawaa
  nop
  mov.l @({PST:#x},pc),r1   ; number: k = 2*page + i/2, code 1 + 2k + (i&1)
  mov.b @(1,r1),r0
  extu.b r0,r10
  shll r10
  mov r12,r0
  shlr r0
  add r0,r10                ; k
  mov r10,r0
  shll r0
  add #1,r0
  mov r12,r2
  mov #1,r3
  and r3,r2
  add r2,r0                 ; code
  mov.b @r14,r2
  extu.b r2,r2
  mov.l @({WHITE:#x},pc),r7
  cmp/eq r2,r0
  bf pdn1
  mov.l @({ORANGE:#x},pc),r7
pdn1:
  mov r7,r13
  mov.l @({A["nums"]:#x},pc),r1
  mov r10,r0
  shll2 r0
  mov.l @(r0,r1),r11
  mov.l @({PFONT:#x},pc),r1
  mov.l @({FONTP:#x},pc),r0
  mov.l r0,@r1
  bsr aawidth
  mov r11,r6
  mov r9,r4
  sub r0,r4
  shlr r4
  add r8,r4
  mov.l @({HC_Y + NUM_DY:#x},pc),r5
  mov r11,r6
  bsr drawaa
  mov r13,r7
  add #1,r12
  mov #6,r0
  cmp/eq r0,r12
  bt pdbstageend
  nop
  mov r12,r0
  and #1,r0
  tst r0,r0
  bt pdbstageend            ; even pad index = both pads of the flush done
  nop
  bra pdpad
  nop
pdbstageend:
  mov.l @({BJNEXT:#x},pc),r5
  mov.b @r5,r0
  add #1,r0                 ; stage+1 (stage 3 -> 4 = finisher next)
  mov.b r0,@r5
  bra {BJ_AFTER_STAGE}      ; ONEFLUSH: next stage now; else marker refresh
  nop
pdbtail:
  mov #0,r0
  mov.l @({BJNEXT:#x},pc),r5
  mov.b r0,@r5               ; cycle complete: idle flushes skip again
  mov.l @({PCOL:#x},pc),r1  ; connectors between the pairs
  mov.l @({DIM:#x},pc),r0
  mov.l r0,@r1
  mov.l @({CONN[0]:#x},pc),r4
  mov.l @({HC_Y + 31:#x},pc),r5
  mov #10,r6
  bsr fillrect
  mov #1,r7
  mov.l @({CONN[1]:#x},pc),r4
  mov.l @({HC_Y + 31:#x},pc),r5
  mov #10,r6
  bsr fillrect
  mov #1,r7
                            ; (no label hint any more: the tabs carry the
                            ;  swipe arrows and the hint overlapped them)
                            ; 2026-10-18: this slot used to hold pdmkb's
                            ; PC-relative mov.l = slot-illegal on SH-4A =
                            ; THE BEAT JUMP TAB FREEZE (every frozen deck
                            ; round had it live, every clean round nopped it)
pdmkb:
  mov.l @({MKB[0]:#x},pc),r4
  mov.l @({MKB[1]:#x},pc),r5
  bsr pxaddr
  nop
  mov #1,r1
  mov.w r1,@r0
pdkeys:
  bra pdend                 ; top-left box removed (see phk)
  nop
  mov #0,r12                ; KEY area: box i
pdbox:
  mov.l @({A["kb"]:#x},pc),r1
  mov r12,r0
  shll2 r0
  add r0,r1
  mov.w @r1,r8              ; x
  mov.w @(2,r1),r0
  mov r0,r9                 ; w
  mov.b @r14,r0             ; pressed?
  extu.b r0,r0
  mov.l @({KFILL:#x},pc),r10
  mov.l @({KEDGE:#x},pc),r11
  add #-20,r0
  mov r0,r2
  mov r12,r0
  cmp/eq #1,r0              ; name box never highlights
  bt pdbx
  shlr r0                   ; box 0 -> 0 ('-'), box 2 -> 1 ('+')
  cmp/eq r0,r2
  bf pdbx
  mov.l @({KPRESS:#x},pc),r10
  mov.l @({ORANGE:#x},pc),r11
pdbx:
  mov.l @({PCOL:#x},pc),r1  ; edge
  mov.l r11,@r1
  mov r8,r4
  mov #{K_Y},r5
  mov r9,r6
  bsr fillrect
  mov #{K_H},r7
  mov.l @({PCOL:#x},pc),r1  ; inside
  mov.l r10,@r1
  mov r8,r4
  add #1,r4
  mov #{K_Y + 1},r5
  mov r9,r6
  add #-2,r6
  bsr fillrect
  mov #{K_H - 2},r7
  mov.l @({FONTP:#x},pc),r13 ; font for this box -> r13, y -> r11
  mov #{K_Y + (K_H - 14) // 2},r11
  mov r12,r0                ; label -> r6, colour -> r7
  cmp/eq #1,r0
  bt pdname
  mov.l @({A["mi"]:#x},pc),r6
  cmp/eq #0,r0
  bt pdlab
  mov.l @({A["pl"]:#x},pc),r6
pdlab:
  mov.l @({WHITE:#x},pc),r7
  bra pdtxt
  nop
pdname:
  mov.l @({FONTT:#x},pc),r13
  mov #{K_Y + (K_H - 9) // 2},r11
  mov.l @({VR.VTBL + 8:#x},pc),r1
  mov r1,r2                 ; table word as a value: steppers keep VSEL in
                            ; 0..n <= 8 now, so this loose bound can never
                            ; walk past the 8 name slots
  mov.l @({A["key"]:#x},pc),r6
  mov.l @({DIM:#x},pc),r7
  tst r2,r2
  bt pdtxt
  mov.l @({WHITE:#x},pc),r7
  mov.l @({A["mix"]:#x},pc),r6
  mov.l @({VR.VSEL:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  tst r0,r0
  bt pdtxt
  cmp/hi r2,r0
  bt pdtxt
  add #-1,r0                ; name k-1 (8 B, maybe unterminated) -> PTMP
  shll2 r0
  add r0,r0
  mov.l @({VR.VNAMES:#x},pc),r1
  add r0,r1
  mov.l @({PTMP:#x},pc),r6
  mov.l @r1+,r0
  mov.l r0,@r6
  mov.l @r1,r0
  mov.l r0,@(4,r6)
  mov #0,r0
  mov.b r0,@(8,r6)
pdtxt:                      ; centred in the box, clipped to it
  mov.l @({PFONT:#x},pc),r1
  mov.l r13,@r1
  mov r7,r10
  mov r6,r13
  mov.l @({PCLIP:#x},pc),r1
  mov r8,r0
  add r9,r0
  add #-2,r0
  mov.l r0,@r1
  bsr aawidth
  nop
  mov r9,r1
  cmp/hi r1,r0
  bf pdtw
  mov r9,r0
pdtw:
  mov r9,r4
  sub r0,r4
  shlr r4
  add r8,r4
  mov r11,r5
  mov r13,r6
  bsr drawaa
  mov r10,r7
  mov.l @({PCLIP:#x},pc),r1
  mov.l @({800:#x},pc),r0
  mov.l r0,@r1
  add #1,r12
  mov #3,r0
  cmp/eq r0,r12
  bt pdend
  nop
  bra pdbox
  nop
pdend:
  mov.l @r15+,r14
  mov.l @r15+,r13
  mov.l @r15+,r12
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
.pool
ilcopy:                     ; r1 src -> r5 cursor (no terminator); r0 = 0
  mov.b @r1+,r0
  tst r0,r0
  bt ilcd
  mov.b r0,@r5
  bra ilcopy
  add #1,r5
ilcd:
  rts
  nop
aawidth:                    ; r6 string -> r0 width px in [PFONT]
  mov.l @({PFONT:#x},pc),r1
  mov.l @r1,r1
  mov #0,r3
awc:
  mov.b @r6+,r0
  extu.b r0,r0
  tst r0,r0
  bt awd
  mov #97,r2
  cmp/hs r2,r0
  bf awu
  add #-32,r0
awu:
  add #-32,r0
  mov #64,r2
  cmp/hs r2,r0
  bt aws
  add #4,r0
  mov.b @(r0,r1),r0
  extu.b r0,r0
  mov #-1,r2
  extu.b r2,r2
  cmp/eq r2,r0
  bt aws
  shll2 r0
  add #69,r0                ; +68 table, +1 advance
  mov.b @(r0,r1),r0
  extu.b r0,r0
  bra awc
  add r0,r3
aws:
  mov.b @(1,r1),r0
  extu.b r0,r0
  bra awc
  add r0,r3
awd:
  rts
  mov r3,r0
drawaa:                     ; r4 x, r5 y, r6 string, r7 colour, font [PFONT]
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov.l r12,@-r15
  mov.l r13,@-r15
  mov.l r14,@-r15
  mov.l @({PDKW:#x},pc),r0  ; light theme: neutral ink flips through the
  mov.l @r0,r0              ; armed word ([PDKW] -> lt_pdk, r2 in/out, or a
  jsr @r0                   ; bare rts stub when the light add-on is absent;
  mov r7,r2                 ; delay slot hands it the ink (r0-r3 dead here)
  mov r2,r7
  mov r4,r8                 ; pen x
  mov r6,r9                 ; string
  mov.l @({MASK:#x},pc),r11
  extu.w r7,r10             ; fg spread
  mov r10,r0
  shll16 r0
  or r0,r10
  and r11,r10
  mov.l @({PFONT:#x},pc),r12
  mov.l @r12,r12
  mov #0,r4
  bsr pxaddr
  nop
  mov r0,r13                ; row y base
  mov.l @({PSTR:#x},pc),r14
  mov.l @r14,r14
dch:
  mov.b @r9+,r0
  extu.b r0,r0
  tst r0,r0
  bt ddone
  mov #97,r1
  cmp/hs r1,r0
  bf dup
  add #-32,r0
dup:
  add #-32,r0
  mov #64,r1
  cmp/hs r1,r0
  bt dsp
  add #4,r0
  mov.b @(r0,r12),r0
  extu.b r0,r0
  mov #-1,r1
  extu.b r1,r1
  cmp/eq r1,r0
  bt dsp
  shll2 r0
  add #68,r0
  mov r12,r1
  add r0,r1                 ; glyph entry (w, adv, off)
  mov.b @r1,r2
  extu.b r2,r2              ; w
  mov.b @(1,r1),r0
  extu.b r0,r3              ; adv
  mov.l @({PCLIP:#x},pc),r0
  mov.l @r0,r0
  mov r8,r4
  add r3,r4
  cmp/hi r0,r4
  bt ddone
  mov.w @(2,r1),r0
  extu.w r0,r0
  mov r12,r1
  add r0,r1                 ; alpha pointer
  mov.l r3,@-r15            ; adv
  mov.l r2,@-r15            ; w
  mov r8,r2
  add r2,r2
  add r13,r2                ; row pointer
  mov.b @r12,r3
  extu.b r3,r3              ; rows
drow:
  mov r2,r5
  mov.l @r15,r4
dpx:
  mov.b @r1+,r6
  extu.b r6,r6
  tst r6,r6
  bt dskip
  mov.w @r5,r7              ; out = (bg*(32-a) + fg*a) >> 5, spread form
  extu.w r7,r7
  mov r7,r0
  shll16 r0
  or r0,r7
  and r11,r7
  mov #32,r0
  sub r6,r0
  mul.l r0,r7
  sts macl,r7
  mul.l r6,r10
  sts macl,r0
  add r0,r7
  shlr2 r7
  shlr2 r7
  shlr r7
  and r11,r7
  mov r7,r0
  shlr16 r0
  or r0,r7
  mov.w r7,@r5
dskip:
  dt r4
  bf/s dpx
  add #2,r5
  dt r3
  bf/s drow
  add r14,r2
  mov.l @r15+,r2
  mov.l @r15+,r3
  bra dch
  add r3,r8
dsp:
  mov.b @(1,r12),r0
  extu.b r0,r0
  bra dch
  add r0,r8
ddone:
  mov.l @r15+,r14
  mov.l @r15+,r13
  mov.l @r15+,r12
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
.pool
drawtab:                    ; r4 x, r5 y, r6 label, r7 selected (1/0)
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov r4,r8
  mov r5,r9
  mov r6,r10
  mov.l @({TABEDGE:#x},pc),r0
  mov.l @({TABTXT0:#x},pc),r11
  mov.l @({TABFILL0:#x},pc),r2
  tst r7,r7
  bt dtb
  mov.l @({TABTXT1:#x},pc),r11
  mov.l @({TABFILL1:#x},pc),r2
dtb:
  mov.l r2,@-r15
  mov.l @({PCOL:#x},pc),r1
  mov.l r0,@r1
  mov r8,r4
  mov r9,r5
  mov #{TAB_W},r6
  bsr fillrect
  mov #{TAB_H},r7
  mov.l @r15+,r2
  mov.l @({PCOL:#x},pc),r1
  mov.l r2,@r1
  mov r8,r4
  add #1,r4
  mov r9,r5
  add #1,r5
  mov #{TAB_W - 2},r6
  bsr fillrect
  mov #{TAB_H - 2},r7
  bsr aawidth
  mov r10,r6
  mov #{TAB_W},r4
  sub r0,r4
  shlr r4
  add r8,r4
  mov r9,r5
  add #{(TAB_H - 9) // 2},r5
  mov r10,r6
  bsr drawaa
  mov r11,r7
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
rowclear:                   ; blank the stock hot-cue row
  sts.l pr,@-r15
  mov.l @({PCOL:#x},pc),r1
  mov #0,r0
  mov.l r0,@r1
  mov.l @({ROWCLR[0]:#x},pc),r4
  mov.l @({ROWCLR[1]:#x},pc),r5
  mov.l @({ROWCLR[2]:#x},pc),r6
  bsr fillrect
  mov #{ROWCLR[3]},r7
  lds.l @r15+,pr
  rts
  nop
padart:                     ; r4 x, r5 y, r6 w: stock hot-cue pad at width w
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov r6,r11
  bsr pxaddr
  nop
  mov r0,r9
  mov.l @({PADART:#x},pc),r8
  mov.l @({PSTR:#x},pc),r1
  mov.l @r1,r1
  mov #63,r10
parow:
  mov r9,r3
  mov r8,r2
  mov #10,r6
pal:
  mov.w @r2+,r0
  mov.w r0,@r3
  dt r6
  bf/s pal
  add #2,r3
  mov.w @r2+,r0             ; two dither fill colours
  mov.w @r2+,r7
  mov r11,r6
  add #-20,r6
  shlr r6
paf:
  mov.w r0,@r3
  add #2,r3
  mov.w r7,@r3
  dt r6
  bf/s paf
  add #2,r3
  mov #10,r6
par:
  mov.w @r2+,r0
  mov.w r0,@r3
  dt r6
  bf/s par
  add #2,r3
  add #44,r8
  add r1,r9
  dt r10
  bf parow
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
pxaddr:                     ; r4 x, r5 y -> r0 pixel pointer (r0, r1)
  mov.l @({PSTR:#x},pc),r0
  mov.l @r0,r0
  mulu.w r5,r0
  sts macl,r0
  mov.l @({PSURF:#x},pc),r1
  mov.l @r1,r1
  add r1,r0
  add r4,r0
  rts
  add r4,r0
fillrect:                   ; r4 x r5 y r6 w r7 h, colour [PCOL]
  sts.l pr,@-r15
  mov.l r6,@-r15
  mov.l @({PCOL:#x},pc),r2   ; ink first: the light flip ([PDKW] -> lt_pdk,
  mov.l @r2,r2               ; r2 in/out, keeps r4-r7) runs while only the
  mov.l @({PDKW:#x},pc),r0   ; stack and r0-r3 are live; pxaddr touches
  mov.l @r0,r0               ; only r0/r1, so the colour stays in r2
  jsr @r0
  mov.l @r15+,r6             ; delay slot: pop w around the flip
  bsr pxaddr
  nop
  mov.l @({PSTR:#x},pc),r1
  mov.l @r1,r1
frr:
  mov r0,r3
  mov r6,r5
frc:
  mov.w r2,@r3
  dt r5
  bf/s frc
  add #2,r3
  dt r7
  bf/s frr
  add r1,r0
  lds.l @r15+,pr
  rts
  nop
"""


def _draw_labels():
    from shasm import Asm
    a = Asm()
    a.assemble(DRAW_SRC)
    return {k: PDRAW + v for k, v in a.labels.items()}


D = _draw_labels()


def CALL(name):
    return f"  mov.l @({D[name]:#x},pc),r0\n  jsr @r0\n  nop\n"


def SETCOL(c):
    return (f"  mov.l @({PCOL:#x},pc),r1\n  mov.l @({c:#x},pc),r0\n"
            f"  mov.l r0,@r1\n")


def SETFONT(f):
    return (f"  mov.l @({PFONT:#x},pc),r1\n  mov.l @({f:#x},pc),r0\n"
            f"  mov.l r0,@r1\n")


def RECT(x, y, w, h):
    return (f"  mov.l @({x:#x},pc),r4\n  mov.l @({y:#x},pc),r5\n"
            f"  mov.l @({w:#x},pc),r6\n  mov.l @({h:#x},pc),r7\n" + CALL("fillrect"))


def CTEXT(x, y, w, s_addr, col_reg=None, col=None):
    """centred text in [x, x+w): string at s_addr (abs) or PTMP."""
    t = f"  mov.l @({PTMPW:#x},pc),r1\n  mov.l @({w:#x},pc),r0\n  mov.l r0,@r1\n"
    t += f"  mov.l @({x:#x},pc),r4\n  mov.l @({y:#x},pc),r5\n"
    t += f"  mov.l @({s_addr:#x},pc),r6\n"
    t += f"  mov {col_reg},r7\n" if col_reg else f"  mov.l @({col:#x},pc),r7\n"
    return t + "  bsr ctext\n  nop\n"


def _side(box, chip_left, arrow, delta, code):
    x, y, w, h = box
    cx = x + 1 if chip_left else x + w - 41
    nx = x + 41 if chip_left else x + 1
    return (
        f"  mov.b @r14,r0\n  mov.l @({KS_EDGE:#x},pc),r2\n  mov #{code},r1\n"
        f"  cmp/eq r1,r0\n  bf ksn{code}\n  mov.l @({ORANGE:#x},pc),r2\nksn{code}:\n"
        f"  mov.l @({PCOL:#x},pc),r1\n  mov.l r2,@r1\n"
        + RECT(x, y, w, h) + SETCOL(KS_FILL) + RECT(x + 1, y + 1, w - 2, h - 2)
        + SETCOL(KS_CHIP) + RECT(cx, y + 1, 40, h - 2)
        + SETFONT(FONTP) + CTEXT(cx, y + (h - 14) // 2, 40, A[arrow], col=KS_ARROW)
        + f"  mov r8,r4\n  add #{delta},r4\n  bsr avail\n  nop\n"
        f"  mov.l @({WHITE:#x},pc),r9\n  tst r0,r0\n  bf ksa{code}\n"
        f"  mov.l @({0x9CF3:#x},pc),r9\nksa{code}:\n"
        f"  mov r8,r4\n  add #{delta},r4\n  mov.l @({PTMP:#x},pc),r5\n"
        "  bsr keyname\n  nop\n"
        + SETFONT(FONTK) + CTEXT(nx, y + (h - 15) // 2, w - 42, PTMP, col_reg="r9"))


KS_SRC = f"""
ksdraw:
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov.l r14,@-r15
  mov.l @({PST:#x},pc),r14
  mov.l @({RTMODE:#x},pc),r9    ; real-time key shift live + no version file?
  mov #0,r0
  mov.b r0,@r9
  mov.l @({RT_NOPSITE:#x},pc),r1
  mov.w @r1,r0
  extu.w r0,r0
  cmp/eq #9,r0
  bf ksnrt
  mov.l @({VR.VTBL + 8:#x},pc),r1
  mov.l @r1,r0              ; 2026-10-18: the COUNT (was the address, never
  tst r0,r0                 ; 0 -> real-time mode never engaged: stale key
  bf ksnrt                  ; names like "Ab" from an old version file)
  mov #1,r0
  mov.b r0,@r9
  mov.l @({RTKEY:#x},pc),r1
  bra ks1
  mov.b @r1,r8                  ; shift = RTKEY
ksnrt:
  mov.l @({VR.VSEL:#x},pc),r1   ; current shift s -> r8
  mov.b @r1,r0
  extu.b r0,r0
  mov #0,r8
  tst r0,r0
  bt ks1
  add #-1,r0
  mov.l @({VSEMI:#x},pc),r1
  mov.b @(r0,r1),r0
  cmp/eq #127,r0
  bt ks1
  mov r0,r8
ks1:
""" + "".join(
    f"  mov.l @({hx:#x},pc),r4\n  mov.l @({hy:#x},pc),r5\n  mov #{hw},r6\n"
    f"  bsr hatch\n  mov #{hh},r7\n" for hx, hy, hw, hh in HATCH) \
    + _side(KSL, True, "lt", -1, 60) + _side(KSR, False, "gt", 1, 62) \
    + SETCOL(KS_EDGE) + RECT(*KSM) \
    + SETCOL(KS_LIGHT) + RECT(KSM[0] + 1, KSM[1] + 1, KSM[2] - 2, 40) \
    + SETCOL(KS_DARK) + RECT(KSM[0] + 1, KSM[1] + 1, 62, 40) \
    + f"  mov r8,r4\n  mov.l @({PTMP:#x},pc),r5\n  bsr keyname\n  nop\n" \
    + SETFONT(FONTK) + CTEXT(KSM[0] + 1, KSM[1] + 13, 62, PTMP, col=WHITE) \
    + f"  mov r8,r4\n  mov.l @({PTMP:#x},pc),r5\n  bsr signed\n  nop\n" \
    + CTEXT(KSM[0] + 63, KSM[1] + 13, KSM[2] - 64, PTMP, col=KS_DARK) \
    + SETCOL(KS_STRIP) + RECT(KSM[0] + 1, KSM[1] + 41, KSM[2] - 2, 15) \
    + f"""
  mov.l @({RTMODE:#x},pc),r1   ; real-time mode + MT off -> the strip says
  mov.b @r1,r0                 ; MT OFF (the tab is inert, see ksstep)
  tst r0,r0
  bf ksres0
  mov.l @({MTW:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bf ksres0
  mov.l @({A["mtoff"]:#x},pc),r6
  bra ksstrip
  nop
ksres0:
  mov.l @({A["reset"]:#x},pc),r6
ksstrip:
""" + SETFONT(FONTT) \
    + f"  mov.l @({PTMPW:#x},pc),r1\n  mov.l @({KSM[2] - 2:#x},pc),r0\n  mov.l r0,@r1\n" \
    + f"  mov.l @({KSM[0] + 1:#x},pc),r4\n  mov.l @({KSM[1] + 44:#x},pc),r5\n" \
    + f"  mov.l @({KS_DARK:#x},pc),r7\n  bsr ctext\n  nop\n" \
    + f"""
  mov.l @r15+,r14
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
.pool
ctext:                      ; r4 x, r5 y, r6 string, r7 colour, width [PTMPW]
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov r4,r8
  mov r5,r9
  mov r6,r10
  mov r7,r11
{CALL("aawidth")}  mov.l @({PTMPW:#x},pc),r1
  mov.l @r1,r4
  cmp/hi r4,r0
  bf ctw
  mov r4,r0
ctw:
  sub r0,r4
  shlr r4
  add r8,r4
  mov r9,r5
  mov r10,r6
  mov r11,r7
{CALL("drawaa")}  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
hatch:                      ; r4 x, r5 y, r6 w, r7 h: 2-px bars every 4 px
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov r4,r8
  mov r5,r9
  mov r6,r10
  mov r7,r11
{SETCOL(KS_HATCH)}hl:
  mov r8,r4
  mov r9,r5
  mov #2,r6
  mov r11,r7
{CALL("fillrect")}  add #4,r8
  add #-4,r10
  cmp/pl r10
  bt hl
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
avail:                      ; r4 shift -> r0 1 if a version has it (0 = mix)
  mov.l @({RTMODE:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bt avnrt
  mov.l @({MTW:#x},pc),r1   ; MT off: nothing reachable (chips draw dim;
  mov.b @r1,r0              ; the tab is inert, see ksstep)
  tst r0,r0
  bt av0
  mov #6,r0                 ; real-time: -6..+6
  cmp/gt r0,r4
  bt av0
  mov #-6,r0
  cmp/ge r0,r4
  bt av1
  bra av0
  nop
avnrt:
  tst r4,r4
  bt av1
  mov.l @({VR.VTBL + 8:#x},pc),r1
  mov r1,r2
  extu.w r2,r2              ; scan bound = exact n (was: dereferenced)
  mov.l @({VSEMI:#x},pc),r1
  mov #0,r3
avl:
  mov r3,r0
  mov.b @(r0,r1),r0
  cmp/eq r4,r0
  bt av1
  add #1,r3
  cmp/eq r2,r3
  bf avl
  nop
av0:
  rts
  mov #0,r0
av1:
  rts
  mov #1,r0
keyname:                    ; r4 shift, r5 dst: "C#^" (^ = m) or signed
  mov.l @({RTMODE:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bf kntrk                  ; real-time: the loaded track's own key
  mov.l @({VKEY:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  tst r0,r0
  bf kngot
kntrk:
  sts.l pr,@-r15
  mov.l r4,@-r15
  mov.l r5,@-r15
  mov.l @({TRKKEY:#x},pc),r0
  jsr @r0
  nop
  mov.l @r15+,r5
  mov.l @r15+,r4
  lds.l @r15+,pr
  tst r0,r0
  bt signed                 ; key unknown -> "+2"
kngot:
  add #-1,r0
  mov r0,r2
  shlr r2                   ; tonic
  and #1,r0
  mov r0,r3                 ; minor
  add r4,r2
  add #24,r2
km:
  mov #12,r0
  cmp/hs r0,r2
  bf km2
  nop
  bra km
  add #-12,r2
km2:
  mov.l @({A["notes"]:#x},pc),r1
  shll2 r2
  add r2,r1
  mov.l @r1,r1
kcp:
  mov.b @r1+,r0
  tst r0,r0
  bt kd
  mov.b r0,@r5
  bra kcp
  add #1,r5
kd:
  tst r3,r3
  bt ke
  mov #94,r0
  mov.b r0,@r5
  add #1,r5
ke:
  mov #0,r0
  rts
  mov.b r0,@r5
signed:                     ; r4 shift -> "\\0" (+-0) / "+1" / "-2" at r5
  tst r4,r4
  bf sg1
  mov #92,r0
  mov.b r0,@r5
  add #1,r5
  mov #48,r0
  mov.b r0,@r5
  bra sgend
  add #1,r5
sg1:
  cmp/pz r4
  bf sgneg
  nop
  bra sgw
  mov #43,r0
sgneg:
  neg r4,r4
  mov #45,r0
sgw:
  mov.b r0,@r5
  add #1,r5
  mov #10,r0
  cmp/hs r0,r4
  bf sgd
  mov #49,r0
  mov.b r0,@r5
  add #1,r5
  add #-10,r4
sgd:
  mov r4,r0
  add #48,r0
  mov.b r0,@r5
  add #1,r5
sgend:
  mov #0,r0
  rts
  mov.b r0,@r5
"""


KSSTEP_SRC = f"""
ksstep:                     ; r0 = 60 key down / 61 reset / 62 key up
  mov r0,r3
  mov.l @({RT_NOPSITE:#x},pc),r1   ; real-time key shift installed?
  mov.w @r1,r0
  extu.w r0,r0
  cmp/eq #9,r0
  bf ksver
  mov.l @({VR.VTBL + 8:#x},pc),r1  ; a version file wins (stems/keys)
  mov.l @r1,r2
  extu.w r2,r2              ; n exact (high half is the keycode, not a count)
  tst r2,r2
  bf ksver
  mov.l @({MTW:#x},pc),r1   ; MASTER TEMPO off -> inert: without MT the DSP
  mov.b @r1,r0              ; plays through its resampler path, which the
  tst r0,r0                 ; key-shift cave does not wrap (freeze fix
  bt ksret                  ; 2026-10-21); presses change nothing
  mov.l @({RTKEY:#x},pc),r6
  mov.b @r6,r0              ; signed semitones
  mov #61,r2
  cmp/eq r2,r3
  bf ksr1
  bra ksrw
  mov #0,r0
ksr1:
  mov #60,r2
  cmp/eq r2,r3
  bf ksr2
  add #-2,r0
ksr2:
  add #1,r0
  mov #6,r2
  cmp/gt r2,r0
  bf ksr3
  mov #6,r0
ksr3:
  mov #-6,r2
  cmp/ge r2,r0
  bt ksrw
  mov #-6,r0
ksrw:
  mov.b r0,@r6
  add #6,r0
  shll2 r0
  mov.l @({KQTAB:#x},pc),r1
  mov.l @(r0,r1),r1
  mov.l @({RT_KEYWORD:#x},pc),r2
  rts
  mov.l r1,@r2
ksver:
  mov r3,r0
  mov r0,r3
  mov.l @({VR.VSEL:#x},pc),r6
  mov #61,r2
  cmp/eq r2,r3
  bf pks1
  mov #0,r0
  rts
  mov.b r0,@r6
pks1:
  mov.b @r6,r0              ; current shift s
  extu.b r0,r0
  tst r0,r0
  bt pks2
  add #-1,r0
  mov.l @({VSEMI:#x},pc),r1
  mov.b @(r0,r1),r0
  cmp/eq #127,r0            ; a stems version: count as 0
  bf pks2
  mov #0,r0
pks2:
  mov #60,r2
  cmp/eq r2,r3
  bf pks3
  add #-2,r0
pks3:
  add #1,r0                 ; target shift
  tst r0,r0
  bf pks4
  nop
  rts
  mov.b r0,@r6
pks4:
  mov r0,r7                 ; target shift (signed)
  mov.l @({VR.VTBL + 8:#x},pc),r1
  mov.l @r1,r2
  extu.w r2,r2              ; n exact (high half is the keycode: without the
                            ; mask a miss scans far past the table)
  mov #8,r0
  cmp/hi r0,r2              ; corrupt count?
  bt ksret
  tst r2,r2                 ; (slot) empty table?
  bt ksret
  mov.l @({VSEMI:#x},pc),r1 ; (slot) version semitones
  mov #0,r4
pks5:
  mov r4,r0
  mov.b @(r0,r1),r0         ; VSEMI[r4], signed; r4 < n <= 8 always
  cmp/eq r7,r0
  bt pks6
  add #1,r4
  cmp/eq r2,r4
  bf pks5
  nop
  bra ksret                 ; no version carries that shift: keep current
  nop
pks6:
  add #1,r4
  mov r4,r0
  rts
  mov.b r0,@r6              ; VSEL = index + 1
ksret:
  rts
  nop
.pool
"""


HCSW_SRC = f"""
  bra hcdown                ; +0 touch-down on the HOT CUE tab (HOT CUE mode)
  nop
  bra hcheld                ; +4 held
  nop
  bra hcrel                 ; +8 released
  nop
hcdown:                     ; r1 = PST, r5 = x
  mov #52,r0
  mov.b r0,@r1
  mov.l @({PSW:#x},pc),r3
  mov.w r5,@r3              ; touch-down x
  mov #0,r0
  rts
  mov.b r0,@(3,r3)          ; not swiped yet
hcheld:                     ; r1 = PST, r5 = x: swipe left = next page,
  mov.l @({PSW:#x},pc),r3   ; right = previous (one step per swipe)
  mov.b @(3,r3),r0
  tst r0,r0
  bf hcret
  mov.w @r3,r0
  extu.w r0,r0
  sub r5,r0                 ; > 0: moved left
  mov #40,r2
  cmp/gt r2,r0
  bt hcl
  neg r0,r0
  cmp/gt r2,r0
  bf hcret
  mov.l @({HC_PREVP:#x},pc),r2
  bra hcgo
  nop
hcl:
  mov.l @({HC_NEXTP:#x},pc),r2
hcgo:
  mov #1,r0
  mov.b r0,@(3,r3)
hccall:
  mov.l @r2,r2              ; hotcue8 entry table (0 = not installed)
  tst r2,r2
  bt hcret
  sts.l pr,@-r15
  jsr @r2
  nop
  lds.l @r15+,pr
hcret:
  rts
  mov #0,r0
hcrel:                      ; r1 = PST: release on the tab. A swipe already
  mov.l @({PSW:#x},pc),r3   ; paged (hcheld); since 2026-10-21 a plain TAP
  mov.b @(3,r3),r0          ; pages forward too (three testers naturally
  tst r0,r0                 ; tapped; swipe and tap both work now)
  bt hctap
  bra hcrd
  nop
hctap:
  mov #0,r0
  mov.b r0,@r1              ; drop the latch first (hc_next may repaint)
  mov.l @({HC_NEXTP:#x},pc),r2
  bra hcgo                  ; null-safe via hccall (SAFE_ZERO = no hotcue8)
  nop
hcrd:
  mov #0,r0                 ; drop the latch
  mov.b r0,@r1
  rts
  mov #0,r0
.pool
"""


RTKEEP_SRC = f"""
rtkeep:                     ; every flush: (0) a beat-jump latch left behind
  mov.l @({PAYDOWN:#x},pc),r1 ; after a tap (the stock hit-tester stops a few
  mov.b @r1,r0              ; frames after a release, sometimes before its
  mov.l @({PSW:#x},pc),r3   ; countdown cleared our latch): the jump key bit
  tst #1,r0                 ; stayed HELD until the next touch (2026-10-25,
  bt rkup                   ; emulator; tester: SLIP + beat jump felt stuck).
  mov #0,r0                 ; finger down: the hit-tester owns the latch
  bra rkjd
  mov.b r0,@(5,r3)          ; age = 0
rkup:
  mov.l @({PST:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  tst r0,r0
  bt rkjd
  mov #15,r2
  cmp/hs r2,r0              ; >= 15: tab / pending / key codes, not a jump
  bt rkjd
  mov.b @(5,r3),r0          ; finger up + jump latch: age it, drop it after
  add #1,r0                 ; 8 flushes (the hit-tester's own 4-frame
  mov.b r0,@(5,r3)          ; release hold always runs first)
  cmp/eq #8,r0
  bf rkjd
  mov #0,r0
  mov.b r0,@r1              ; latch
  mov.b r0,@(5,r3)
  mov.b r0,@(4,r3)          ; countdown
  mov.l @({KEY93:#x},pc),r1
  mov.b @r1,r0
  and #127,r0
  mov.b r0,@r1
  mov.l @({KEY94:#x},pc),r1
  mov.b @r1,r0
  and #251,r0
  mov.b r0,@r1
rkjd:                       ; (1) a MASTER TEMPO change resets
  mov.l @({MTW:#x},pc),r1   ; the real-time key to 0 -- arming a key while
  mov.b @r1,r0              ; MT is off and switching MT on froze a deck
  mov.l @({MTLAST:#x},pc),r2  ; (2026-10-21); MTLAST tracks MT per flush
  mov.b @r2,r3
  cmp/eq r0,r3
  bt rkmt
  mov.b r0,@r2
  mov.l @({RTKEY:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bt rkmt
  mov #0,r0
  mov.b r0,@r1
  add #6,r0
  shll2 r0
  mov.l @({KQTAB:#x},pc),r1
  mov.l @(r0,r1),r1
  mov.l @({RT_KEYWORD:#x},pc),r2
  mov.l r1,@r2
rkmt:
  mov.l @({RT_NOPSITE:#x},pc),r1   ; (2) the deck clears 0x09D24DD8 when a
  mov.w @r1,r0              ; track (re)loads -> put our key back
  extu.w r0,r0
  cmp/eq #9,r0
  bf rkx
  mov.l @({VR.VTBL + 8:#x},pc),r1
  mov.l @r1,r0
  tst r0,r0
  bf rkx                    ; version file: versions own the key
  mov.l @({RTKEY:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bt rkx
  add #6,r0
  shll2 r0
  mov.l @({KQTAB:#x},pc),r1
  mov.l @(r0,r1),r2
  mov.l @({RT_KEYWORD:#x},pc),r1
  mov.l @r1,r3
  cmp/eq r2,r3
  bt rkx
  mov.l r2,@r1
rkx:
  rts
  nop
.pool
"""


TRKKEY_SRC = f"""
trkkey:                     ; -> r0 = 1 + 2*tonic + minor (C=0..B=11), 0 = ?
  mov.l @({TRK_KEYSTR:#x},pc),r1
  mov.w @r1+,r0
  extu.w r0,r2              ; c0
  mov r2,r0
  add #-48,r0               ; c0 - '0'
  mov #9,r3
  cmp/hi r3,r0
  bt tkclassic              ; not a digit
  mov r0,r4                 ; Camelot number
  mov.w @r1+,r0
  extu.w r0,r2
  mov r2,r0
  add #-48,r0
  cmp/hi r3,r0
  bt tkcam
  mov r4,r5                 ; second digit: n = n*10 + d
  shll2 r4
  add r5,r4
  shll r4
  add r0,r4
  mov.w @r1+,r0
  extu.w r0,r2
tkcam:                      ; r4 = n (1..12), r2 = 'A' minor / 'B' major
  mov r4,r0
  add #-1,r0
  mov #11,r3
  cmp/hi r3,r0
  bt tkbad
  mov r0,r5                 ; tonic = base + 7*(n-1) mod 12
  shll2 r0
  shll r0
  sub r5,r0
  mov r2,r5
  mov #65,r3
  cmp/eq r3,r5
  bt tkA
  mov #66,r3
  cmp/eq r3,r5
  bf tkbad
  add #11,r0                ; nB: 1B = B
  bra tkmod
  mov #0,r6
tkA:
  add #8,r0                 ; nA: 1A = Abm
  mov #1,r6
tkmod:
  mov #12,r3
tkm1:
  cmp/hs r3,r0
  bf tkout
  bra tkm1
  sub r3,r0
tkout:                      ; r0 tonic, r6 minor
  shll r0
  add r6,r0
  rts
  add #1,r0
tkclassic:                  ; A..G (+ '#' / 'b') (+ 'm')
  mov r2,r0
  add #-65,r0
  mov #6,r3
  cmp/hi r3,r0
  bt tkbad
  add #5,r0                 ; C=0 D=1 E=2 F=3 G=4 A=5 B=6
  mov #7,r3
  cmp/hs r3,r0
  bf tkc0
  add #-7,r0
tkc0:
  mov r0,r4
  shll r4                   ; 2*idx, minus 1 from F on
  mov #3,r3
  cmp/hs r3,r0
  bf tkc1
  add #-1,r4
tkc1:
  mov.w @r1+,r0
  extu.w r0,r0
  cmp/eq #35,r0             ; '#'
  bf tkc2
  add #1,r4
  bra tkc3
  nop
tkc2:
  cmp/eq #98,r0             ; 'b'
  bf tkc4
  add #11,r4
tkc3:
  mov.w @r1+,r0
  extu.w r0,r0
tkc4:
  mov #0,r6
  cmp/eq #109,r0            ; 'm'
  bf tkc5
  mov #1,r6
tkc5:
  bra tkmod
  mov r4,r0
tkbad:
  rts
  mov #0,r0
"""

def blobs():
    h, d, k = assemble(HOOK_SRC), assemble(DRAW_SRC), assemble(KS_SRC)
    st = assemble(KSSTEP_SRC)
    rk = assemble(RTKEEP_SRC)
    hs = assemble(HCSW_SRC)
    tk = assemble(TRKKEY_SRC)
    if os.environ.get("PP_NO_HINT") and False:   # hint removed 2026-10-18
        # Diagnostic split: silence the swipe-hint painter (BJ/HC rows'
        # only unproven component) while keeping touch/paging identical.
        # Entry becomes rts;nop (callers' stacks stay balanced: no pushes
        # run, so no pops are owed). Refused unless the entry is exactly
        # the expected sts.l pr,@-r15.
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)            # same bytes, exposes labels
        off = lab.labels["pdswhint"]
        assert bytes(d[off:off + 2]) == struct.pack("<H", 0x4F22), \
            d[off:off + 2].hex()
        d = bytes(bytearray(d[:off]) + struct.pack("<HH", 0x000B, 0x0009)
                  + bytearray(d[off + 4:]))
    if os.environ.get("PP_NO_DRAW"):
        # Bigger split: silence the whole row painter (touch, paging and
        # state keep working; the stock row just shows through). Same
        # rts;nop entry patch, same fail-stop check.
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        off = lab.labels["pdraw"]
        assert bytes(d[off:off + 2]) == struct.pack("<H", 0x4F22), \
            d[off:off + 2].hex()
        d = bytes(bytearray(d[:off]) + struct.pack("<HH", 0x000B, 0x0009)
                  + bytearray(d[off + 4:]))
    if os.environ.get("PP_NO_BJROW"):
        # Whole beat-jump row silenced (touch/paging/mode switch untouched;
        # tabs, markers and the blanked label still paint). pdbj is reached
        # by BRA (not bsr: no return address), so the stub is bra pdmkb
        # (continue past the unit), NEVER rts (that would skip pdraw's
        # register restores and corrupt the display worker -- exactly the
        # freeze this is hunting; a 2026-10-16 round tested an rts stub by
        # mistake and proved nothing). Fail-stop on the entry opcode.
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        off = lab.labels["pdbj"]
        first = struct.unpack("<H", bytes(d[off:off + 2]))[0]
        assert (first & 0xF000) == 0xB000, hex(first)
        tgt = PDRAW + lab.labels["pdmkb"]
        assert 0 <= (tgt - (PDRAW + off + 4)) // 2 < 0x1000
        d = (bytes(bytearray(d[:off])
             + struct.pack("<H", 0xA000 | ((tgt - (PDRAW + off + 4)) // 2))
             + bytearray(d[off + 2:])))
    if os.environ.get("PP_NO_BJCALLS"):
        # FINAL BJ-split (2026-10-17): every element is exonerated and
        # the same primitives paint fine from the KEY SHIFT row, so the
        # remaining split is {pure inline code + state reads} vs {the
        # pile-up of nested draw calls (rowclear 1 + 6x(padart+fillrect
        # + aawidth + 2x drawaa) + pdswhint ~= 24 per paint)}. This nops
        # EVERY bsr inside pdbj..pdmkb: the loop still iterates and all
        # the state math still runs; zero nested calls execute.
        # CLEAN => the call multiplicity/sequence is the crasher;
        # FROZE => the inline code itself (all our mapped memory --
        # would point at an IRQ/timing interaction nothing here shows).
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        blob = bytearray(d)
        lo, hi = lab.labels["pdbj"], lab.labels["pdmkb"]
        n = 0
        for off in range(lo, hi, 2):
            w = struct.unpack("<H", bytes(blob[off:off + 2]))[0]
            if (w & 0xF000) == 0xB000:
                blob[off:off + 2] = struct.pack("<H", 0x0009)
                n += 1
        # static count: rowclear 1 + padart 1 (loop body, runs 6x) +
        # fillrect 3 + aawidth 2 + drawaa 2 + pdswhint 1 = 10 sites
        assert n == 10, n
        d = bytes(blob)
    if os.environ.get("PP_NO_BJAA"):
        # Fourth BJ-split round (2026-10-17): BJNUM (drawaa nopped) and
        # BJFR (fillrect nopped) both still froze; BJROW (unit skipped)
        # was clean. aawidth ran in EVERY frozen round (it executes
        # BEFORE the nopped draw sites) -- and a bad table read in
        # aawidth is exactly the class of bug that kills real hardware
        # while the emulator (all-RAM) sails on. Nop BOTH bsr aawidth
        # sites in the unit AND the bsr drawaa sites: with the width in
        # r0 undefined the following drawaa would draw at a wild x, so
        # the draws must go together (drawaa is exonerated anyway).
        # CLEAN here => aawidth (font measuring) is the crasher;
        # FROZE => only the inline state math / @r14 reads remain.
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        blob = bytearray(d)
        lo, hi = lab.labels["pdbj"], lab.labels["pdmkb"]
        aaw = PDRAW + lab.labels["aawidth"]
        daa = PDRAW + lab.labels["drawaa"]
        na = nd = 0
        for off in range(lo, hi, 2):
            w = struct.unpack("<H", bytes(blob[off:off + 2]))[0]
            if (w & 0xF000) != 0xB000:
                continue
            disp = (w & 0xFFF) - (0x1000 if w & 0x800 else 0)
            tgt = PDRAW + off + 4 + disp * 2
            if tgt == aaw:
                blob[off:off + 2] = struct.pack("<H", 0x0009)
                na += 1
            elif tgt == daa:
                blob[off:off + 2] = struct.pack("<H", 0x0009)
                nd += 1
        assert na == 2, na
        assert nd == 2, nd
        d = bytes(blob)
    if os.environ.get("PP_NO_BJFR"):
        # Third BJ-split round (2026-10-17): PP_NO_BJROW (whole unit
        # skipped) taps CLEAN on the deck while PP_NO_BJNUM (numbers +
        # tab icons nopped) still froze -- so the crasher is elsewhere
        # in pdbj..pdmkb. Numbers/icons (drawaa), pad art and the swipe
        # hint are exonerated; the remaining suspects are the three
        # bsr fillrect calls (tab square + the two 10x1 connector
        # strips) and aawidth. This nops those fillrect calls (same
        # stack-safe nop-the-bsr pattern as PP_NO_BJNUM; the delay-slot
        # colour mov still runs, harmlessly).
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        blob = bytearray(d)
        lo, hi = lab.labels["pdbj"], lab.labels["pdmkb"]
        fillrect = PDRAW + lab.labels["fillrect"]
        n = 0
        for off in range(lo, hi, 2):
            w = struct.unpack("<H", bytes(blob[off:off + 2]))[0]
            if (w & 0xF000) != 0xB000:
                continue                      # not bsr
            disp = (w & 0xFFF) - (0x1000 if w & 0x800 else 0)
            if PDRAW + off + 4 + disp * 2 != fillrect:
                continue
            blob[off:off + 2] = struct.pack("<H", 0x0009)
            n += 1
        assert n == 3, n
        d = bytes(blob)
    if os.environ.get("PP_NO_BJNUM"):
        # Beat-jump numbers + tab icons don't paint (their boxes and art
        # still do; touch/paging untouched). Nops the two `bsr drawaa`
        # sites inside pdbj..pdmkb (aawidth still runs: it only reads).
        # A nop-ped call leaves no stack imbalance (nothing pushed).
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        blob = bytearray(d)
        lo, hi = lab.labels["pdbj"], lab.labels["pdmkb"]
        drawaa = PDRAW + lab.labels["drawaa"]
        n = 0
        for off in range(lo, hi, 2):
            w = struct.unpack("<H", bytes(blob[off:off + 2]))[0]
            if (w & 0xF000) != 0xB000:
                continue                      # not bsr
            disp = (w & 0xFFF) - (0x1000 if w & 0x800 else 0)
            if PDRAW + off + 4 + disp * 2 != drawaa:
                continue                      # bsr to somewhere else
            blob[off:off + 2] = struct.pack("<H", 0x0009)
            n += 1
        assert n == 2, n
        d = bytes(blob)
    _kill_list = [n for n in os.environ.get("PP_NOCALLS", "").split(",")
                  if n in ("padart", "fillrect", "aawidth", "drawaa",
                           "rowclear")]
    if _kill_list:
        # Generic per-call bisection (2026-10-17 night): nop inside
        # pdbj..pdmkb every bsr whose TARGET is in PP_NOCALLS. The
        # bisection history: PP_INCPROBE (ALL six call types nopped,
        # machinery live) = NO FREEZE; the full paint (all calls) =
        # FREEZE. So per-call rounds narrow which of the six types is
        # guilty under the incremental structure. Example:
        #   PP_NOCALLS=aawidth,drawaa,pdswhint  (cheap pixels stay on)
        # pxaddr (markers) is never a candidate and stays live.
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        blob = bytearray(d)
        kills = {PDRAW + lab.labels[n] for n in _kill_list}
        lo, hi = lab.labels["pdbj"], lab.labels["pdmkb"]
        n = 0
        for off in range(lo, hi, 2):
            w = struct.unpack("<H", bytes(blob[off:off + 2]))[0]
            if (w & 0xF000) != 0xB000:
                continue                      # not bsr
            disp = (w & 0xFFF) - (0x1000 if w & 0x800 else 0)
            if PDRAW + off + 4 + disp * 2 not in kills:
                continue                      # not a listed target
            blob[off:off + 2] = struct.pack("<H", 0x0009)
            n += 1
        expect = 0
        for nm, cnt in (("rowclear", 1), ("padart", 1), ("fillrect", 3),
                        ("aawidth", 2), ("drawaa", 2), ("pdswhint", 1)):
            if nm in _kill_list:
                expect += cnt
        assert n == expect, (n, expect)
        d = bytes(blob)
    if os.environ.get("PP_INCPROBE"):
        # 2026-10-17 EVENING ROUND 1 (first incremental deck run) FROZE
        # mid-cycle: user saw stock hot-cue pads show through, our pads
        # begin, tabs vanish, then the wedge. Same freeze class as all
        # live-BJ-paint rounds, so either (a) the new scheduling
        # machinery (gate/stages/markers/resume byte) kills hardware,
        # or (b) the draw calls still do under the new interleaving.
        # This probe answers that in ONE round: nop every draw call
        # (padart/fillrect/aawidth/drawaa/pdswhint/rowclear) inside
        # pdbj..pdmkb by TARGET (like PP_NO_BJAA), BUT KEEP pxaddr (the
        # two marker writes need a valid fb address or the mov.w store
        # fires wild = a freeze by construction). Row + tabs never
        # paint; the stage machine, markers, resume byte and the gate
        # all run. Equivalence of old PP_NO_BJCALLS (HW-CLEAN but in
        # the OLD single-flush structure) in the NEW structure.
        #   FROZE here => the new machinery is the crasher.
        #   CLEAN here => the calls under interleaving are; next
        #   probe re-enables exactly one call type (start: padart).
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        blob = bytearray(d)
        kills = {PDRAW + lab.labels[n] for n in
                 ("padart", "fillrect", "aawidth", "drawaa",
                  "pdswhint", "rowclear")}
        lo, hi = lab.labels["pdbj"], lab.labels["pdmkb"]
        n = 0
        for off in range(lo, hi, 2):
            w = struct.unpack("<H", bytes(blob[off:off + 2]))[0]
            if (w & 0xF000) != 0xB000:
                continue                      # not bsr
            disp = (w & 0xFFF) - (0x1000 if w & 0x800 else 0)
            if PDRAW + off + 4 + disp * 2 not in kills:
                continue                      # pxaddr / unknown: keep
            blob[off:off + 2] = struct.pack("<H", 0x0009)
            n += 1
        assert n == 10, n                     # rowclear 1 + padart 1 +
        # fillrect 3 + aawidth 2 + drawaa 2 + pdswhint 1
        d = bytes(blob)
    if os.environ.get("PP_NO_PADART"):
        # Silence only the stock-art blitter (bsr-called: rts stub is
        # stack-safe). Same fail-stop entry check as the other stubs.
        from shasm import Asm
        lab = Asm()
        lab.assemble(DRAW_SRC)
        off = lab.labels["padart"]
        assert bytes(d[off:off + 2]) == struct.pack("<H", 0x4F22), \
            d[off:off + 2].hex()
        d = bytes(bytearray(d[:off]) + struct.pack("<HH", 0x000B, 0x0009)
                  + bytearray(d[off + 4:]))
    kq = struct.pack("<13I", *KQWORDS)
    fp, ft, fk = aafont.blob("pad"), aafont.blob("tab"), aafont.blob("key")
    art = PADART_DATA.astype("<u2").tobytes()
    assert PADART_DATA.shape == (63, 22)
    assert PC0 + len(h) <= PDRAW, len(h)
    assert PDRAW + len(d) <= KSSTEP, len(d)
    assert KSSTEP + len(st) <= HCSW, len(st)
    assert HCSW + len(hs) <= RTKEEP, len(hs)
    assert RTKEEP + len(rk) <= KQTAB, len(rk)
    assert KQTAB + len(kq) <= KSDRAW
    assert KSDRAW + len(k) <= PDATA, len(k)
    assert PADART + len(art) <= FONTP
    assert FONTP + len(fp) <= FONTT, len(fp)
    assert FONTT + len(ft) <= FONTK, len(ft)
    assert FONTK + len(fk) <= PEND, len(fk)
    init = struct.pack("<II", 0, 0) + struct.pack("<IIII", 0, 0, 800, 0)
    init += b"\0" * (64 - len(init))    # through PST+63: PBUSY starts clear
    init = init[:56] + struct.pack("<I", PDKSTUB) + init[60:]
    assert PDATA + len(_DATA) <= TRKKEY and TRKKEY + len(tk) <= PMASK, len(tk)
    return [(PC0, h), (PDRAW, d), (KSSTEP, st), (HCSW, hs), (RTKEEP, rk), (KQTAB, kq), (KSDRAW, k),
            (TRKKEY, tk),
            (PDATA, _DATA), (PST, init), (RTKEY, b"\0\0"),
            (PMASK, b"\0\0"), (PADART, art),
            (FONTP, fp), (FONTT, ft), (FONTK, fk), (PDKSTUB, b"\x0b\x00\x09\x00")]


def patches():
    """[(addr, stock byte, new byte)] static data edits -- none any more
    (the XDJ-1000 layout leaves REV and the waveform strip stock)."""
    return []


def entries():
    from shasm import Asm
    a = Asm()
    a.assemble(HOOK_SRC)
    return PC0 + a.labels["ptouch"], PC0 + a.labels["pblit"]


def hooks():
    t, b = entries()
    return [(HOOK_TOUCH, ORIG_TOUCH, t), (HOOK_BLIT, ORIG_BLIT, b)]


def install(mem):
    for a, b in blobs():
        for i, x in enumerate(b):
            mem.w8(a + i, x)
    for a, old, new in patches():
        assert mem.r8(a) == old, hex(a)
        mem.w8(a, new)
    for pool, old, new in hooks():
        assert mem.r32(pool) == old, hex(pool)
        mem.w32(pool, new)


def plan():
    n = 0
    for a, b in blobs():
        print(f"{a:#010x} {len(b):5d} B")
        n += len(b)
    for p, o, v in hooks():
        print(f"hook {p:#010x}: {o:#010x} -> {v:#010x}")
    print(f"total {n} B, range {PC0:#x}..{PEND:#x}")


# ----------------------------------------------------------------- emu -----
def emu():
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    ck = sys.argv[2] if len(sys.argv) > 2 else "cache/emu_perform_track.pkl"
    m = Machine.load(ck, core="py")
    mem, c = m.mem, m.cpu
    install(mem)
    ok = True

    def chk(label, good):
        """Label every ok &= so one run names the exact flipping step."""
        good = bool(good)
        if not good:
            print(f"CHECK FAIL: {label}")
        return good

    def run(n, frame=None):
        mem.dev.panel_frame = frame or panl.build_frame()
        r = c.run(limit=n)
        if "limit" not in str(r):
            print("RUN STOPPED:", r)

    calls = []

    def tr(pc, op):
        if pc == 0x08D4318E:            # beat-jump handler entry
            calls.append(c.gr(4))
    run(6_000_000)
    fb_png(m, "screenshots/perform_pads_hotcue.png")

    def tap(xy, tag, hold=3_000_000):
        if isinstance(xy, int):
            x, w = ITEMS[xy]
            xy = (x + w // 2, HC_Y + HC_H // 2)
        calls.clear()
        c.trace = tr
        run(hold, panl.build_frame(touch=xy))
        latch = mem.r8(PST)
        fb_png(m, f"screenshots/perform_pads_{tag}.png")
        run(3_000_000)
        c.trace = None
        rv, fw = mem.r32(ROWREV & 0x1FFFFFFF), mem.r32(ROWFWD & 0x1FFFFFFF)
        print(f"[{tag}] {xy} latch={latch} mode={mem.r8(PST + 2)} "
              f"page={mem.r8(PST + 1)} handler r4={calls[:2]} rows rev={rv} "
              f"fwd={fw} after={mem.r8(PST)} cd={mem.r8(PSW + 4)} zone={mem.r32(ZSTATE):#x}")
        return latch, calls[:], rv, fw

    def jump_ok(i, page, tag):
        k, h = 2 * page + i // 2, i & 1
        lt, cl, rv, fw = tap(i, tag)
        # since the swipe update the jump fires on RELEASE (latch 51 while
        # held): check the handler call + the size row, not the latch
        good = cl[:1] == [1 + h] and (fw if h else rv) == 1 << k
        if not good:
            print("   ^^ FAIL (want code", 1 + 2 * k + h, "size", 1 << k, ")")
        return good

    BJTAB = (TAB_X + TAB_W // 2, TAB_Y1 + TAB_H // 2)
    HCTAB = (TAB_X + TAB_W // 2, TAB_Y0 + TAB_H // 2)
    tap(BJTAB, "tab_bj")
    ok &= chk("tab_bj mode/page", mem.r8(PST + 2) == 1 and mem.r8(PST + 1) == 0)
    run(6_000_000)
    fb_png(m, "screenshots/perform_pads_beatjump.png")
    for i in range(6):
        ok &= chk(f"p0_{i} jump", jump_ok(i, 0, f"p0_{i}"))
    tap((193 + 0, HC_Y + 30), "gap")          # pair gap: swallowed
    ok &= chk("gap swallow ZSTATE", mem.r32(ZSTATE) == 0x1E)
    tap(BJTAB, "page1")
    ok &= chk("page1", mem.r8(PST + 1) == 1)
    ok &= chk("p1 jumps", jump_ok(0, 1, "p1_back4") and jump_ok(5, 1, "p1_fwd16"))
    tap(BJTAB, "page2")
    ok &= chk("page2", mem.r8(PST + 1) == 2)
    run(6_000_000)
    fb_png(m, "screenshots/perform_pads_page2.png")
    ok &= chk("p2 jumps", jump_ok(4, 2, "p2_back64") and jump_ok(1, 2, "p2_fwd16"))
    tap(BJTAB, "page_wrap")
    ok &= chk("page_wrap", mem.r8(PST + 1) == 0)
    # SWIPE (user request 2026-10-12): press a jump pad, drag 80 px, release
    # -> one page step, NO jump; a tap still jumps (on release)
    def swipe(dx_sign):
        x0, w0 = ITEMS[2]
        xs, y = x0 + w0 // 2, HC_Y + HC_H // 2
        calls.clear()
        c.trace = tr
        run(2_000_000, panl.build_frame(touch=(xs, y)))
        for d in (20, 40, 60, 80):
            run(1_500_000, panl.build_frame(touch=(xs + dx_sign * d, y)))
        run(3_000_000)
        c.trace = None
        return mem.r8(PST + 1), calls[:]
    pg, cl = swipe(-1)
    print(f"[swipe left] page {pg} jumps {cl}")
    ok &= chk("swipe left -> page 1, no jump", pg == 1 and not cl)
    pg, cl = swipe(+1)
    print(f"[swipe right] page {pg} jumps {cl}")
    ok &= chk("swipe right -> page 0, no jump", pg == 0 and not cl)
    # KEY SHIFT: fake trailer -- A minor, versions +1 +2 -2 -1
    KSTAB = (TAB_X + TAB_W // 2, TAB_Y[2] + TAB_H // 2)
    mem.w32(VR.VTBL + 8, 4)
    mem.w8(VKEY, 1 + 2 * 9 + 1)
    for k, v in enumerate([1, 2, -2, -1, 127, 127, 127, 127]):
        mem.w8(VSEMI + k, v & 0xFF)
    tap(KSTAB, "tab_ks")
    ok &= chk("tab_ks mode", mem.r8(PST + 2) == 2)
    run(6_000_000)
    fb_png(m, "screenshots/perform_keyshift.png")
    L, M, R = ((b[0] + b[2] // 2, b[1] + b[3] // 2) for b in (KSL, KSM, KSR))
    ks = []
    for tag, xy in (("up", R), ("up", R), ("up_none", R), ("reset", M),
                    ("down", L), ("down", L), ("down_none", L)):
        tap(xy, f"ks_{tag}{len(ks)}")
        ks.append(mem.r8(VR.VSEL))
    print("KEY SHIFT VSEL sequence", ks)
    ok &= chk("ks VSEL sequence", ks == [1, 2, 2, 0, 4, 3, 3])
    run(6_000_000)
    fb_png(m, "screenshots/perform_keyshift_minus2.png")
    mem.w8(VR.VSEL, 0)
    # real-time key shift (no version file, BD8 store NOPed): -6..+6
    ncount = mem.r32(VR.VTBL + 8)
    site = mem.r16(RT_NOPSITE)
    mem.w32(VR.VTBL + 8, 0)
    mem.w16(RT_NOPSITE, 0x0009)
    mem.w8(MTW, 1)            # real-time keys are MT-only since the freeze
    run(6_000_000)            # fix; let a flush sync MTLAST first
    rt, kw = [], []
    for tag, xy in (("up", R), ("up", R), ("reset", M), ("down", L), ("down", L),
                    ("down", L)):
        tap(xy, f"rt_{tag}{len(rt)}")
        v = mem.r8(RTKEY)
        rt.append(v - 256 if v > 127 else v)
        kw.append(mem.r32(RT_KEYWORD))
    print("REAL-TIME KEY sequence", rt, [hex(w) for w in kw])
    ok &= chk("rt key sequence", rt == [1, 2, 0, -1, -2, -3])
    ok &= chk("rt key words", kw == [KQWORDS[6 + v] for v in rt])
    run(6_000_000)
    fb_png(m, "screenshots/perform_keyshift_realtime.png")
    for _ in range(3):
        tap(M, "rt_reset_end") if _ == 0 else None
    ok &= chk("rt reset", mem.r8(RTKEY) == 0 and mem.r32(RT_KEYWORD) == KQWORDS[6])
    # MT gating (freeze fix 2026-10-21): inert while MT is off, any MT
    # change resets the key to 0
    mem.w8(MTW, 0)
    tap(R, "rt_mtoff_up")
    ok &= chk("mt off inert", mem.r8(RTKEY) == 0 and mem.r32(RT_KEYWORD) == KQWORDS[6])
    mem.w8(MTW, 1)
    tap(R, "rt_mton_up")
    tap(R, "rt_mton_up2")
    ok &= chk("mt on steps", mem.r8(RTKEY) == 2)
    mem.w8(MTW, 0)          # MT off -> the flush watch resets the key
    run(6_000_000)
    ok &= chk("mt change resets key", mem.r8(RTKEY) == 0 and mem.r32(RT_KEYWORD) == KQWORDS[6])
    mem.w8(MTW, 1)
    run(6_000_000)
    ok &= chk("mt on no key", mem.r8(RTKEY) == 0)
    mem.w16(RT_NOPSITE, site)
    mem.w32(VR.VTBL + 8, ncount)
    mem.w8(VKEY, 0)
    # STEMS mode: VOCALS/DRUMS/BASS/OTHER mute pads
    STTAB = (TAB_X + TAB_W // 2, TAB_Y[3] + TAB_H // 2)
    tap(STTAB, "tab_stems")
    ok &= chk("tab_stems mode", mem.r8(PST + 2) == 3)
    run(6_000_000)
    fb_png(m, "screenshots/perform_stems_allon.png")
    STM = [(x + w // 2, HC_Y + HC_H // 2) for x, w in STEMS5]
    seq = []
    for tag, xy in (("voc", STM[0]), ("drm", STM[1]), ("bas", STM[2]),
                    ("oth", STM[3])):
        tap(xy, f"st_{tag}")
        seq.append(mem.r8(PMASK))
    ok &= chk("stem mute mask walk", seq == [1, 3, 7, 15])
    run(6_000_000)
    fb_png(m, "screenshots/perform_stems_allmuted.png")
    for k in range(4):                     # no MIX pad: un-mute one by one
        tap(STM[k], f"st_unmute{k}")
    ok &= chk("all stems back on", mem.r8(PMASK) == 0)
    tap(STM[0], "st_voc2")
    tap(STM[2], "st_bas2")
    run(6_000_000)
    fb_png(m, "screenshots/perform_stems_voc_bas.png")
    tap(STM[0], "st_voc3")
    ok &= chk("stem toggle back on", mem.r8(PMASK) == 4)
    tap((STEMS5[0][0] + STEMS5[0][1] + 8, HC_Y + 30), "st_gap")
    ok &= chk("stem gap swallow", mem.r32(ZSTATE) == 0x1E)
    tap(BJTAB, "tab_bj_again")
    # key area with a fake 4-version table (circular key order)
    mem.w32(VR.VTBL + 8, 4)
    for k, nm in enumerate([b"KEY+1", b"KEY+2", b"KEY-2", b"KEY-1"]):
        for j, b in enumerate(nm.ljust(8, b"\0")):
            mem.w8(VR.VNAMES + 8 * k + j, b)
    seq = []                # top-left box removed: taps there change nothing
    for tag, xy in (("plus", (238, 37)), ("minus", (118, 37))):
        tap(xy, f"key_{tag}{len(seq)}")
        seq.append(mem.r8(VR.VSEL))
    print("VSEL sequence (box removed)", seq)
    ok &= chk("top-left box gone", seq == [0, 0])
    run(6_000_000)
    fb_png(m, "screenshots/perform_pads_key.png")
    mem.w32(VR.VTBL + 8, 0)
    mem.w8(VR.VSEL, 0)
    tap(HCTAB, "tab_hc")
    ok &= chk("tab_hc mode", mem.r8(PST + 2) == 0)
    # MODS OFF (MY SETTINGS): tabs dead, nothing drawn
    mem.w8(MSOFF, 1)
    tap(BJTAB, "mods_off_tab")
    ok &= chk("mods_off tab dead", mem.r8(PST + 2) == 0)
    mem.w8(MSOFF, 0)
    run(12_000_000)                      # settle: let the tab redraw land
    fb_png(m, "screenshots/perform_pads_back_to_hotcue.png")
    # HOT CUE mode: stock hot cue A reaches the untouched hit-tester again
    # (zone 0x17 while held). Runs last: stock loop presses can stall the
    # emu's panel link (no DSP model).
    # KNOWN FLAKE (2026-10-10): 0x1e (the PERFORM idle latch) can linger in
    # ZSTATE during the hold depending on how much menu work preceded this
    # check (the .bak choreography passes 100%, this one sometimes shows
    # {0x17, 0x1e}). The assertion that matters: 0x17 APPEARS while held
    # (stock hot-cue hit-test untouched) and the state is 0x1e after
    # release. Every pad/key/tab assertion above is exact.
    mem.dev.panel_frame = panl.build_frame(touch=(165, 264))
    zs = set()
    for _ in range(12):
        c.run(limit=300_000)
        zs.add(mem.r32(ZSTATE))
    run(3_000_000)
    print(f"[hotcueA] stock zones while held {sorted(map(hex, zs))}, "
          f"after release {mem.r32(ZSTATE):#x}")
    ok &= chk("hotcueA stock zones (known flake)", 0x17 in zs and mem.r32(ZSTATE) == 0x1E)
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "plan":
        plan()
    elif cmd == "emu":
        sys.exit(0 if emu() else 1)
