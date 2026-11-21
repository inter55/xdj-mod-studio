#!/usr/bin/env python3
"""linkrgb.py -- colour waveforms on a deck that loads its track over
Pro DJ Link (the deck WITHOUT the USB stick). RAM only; reboot = stock.

Why the linked deck stays blue (fw 1.15, static analysis 2026-11-17):
  * the ANLZ context the RGB modules read is built only for the deck's OWN
    USB: 0x08BAE380 maps device type 3 -> ctx slot 0 and every other device
    to -1, so a track that arrives over Link never gets PWV4/PWV5 in RAM;
  * the Link DB client (dbcl_*) can ask the other player for the stock
    waveform (GetWaveData / GetParWaveData), beat grid and cues, but has no
    request for the colour sections.

What the deck CAN do: open files on the other player's stick over NFS.
  * generic fopen 0x08DDF53C(wpath "L:/...", mode, opts) looks the drive
    letter up in 0x0C8D84B0 and dispatches by device type; type 5 calls
    the NFS open 0x08D712E8 through the pool word 0x08DDF7F4 -- the ONLY
    way into an NFS open (every other entry into the NFS module is
    read/seek/size/close on an open handle);
  * the media read server FUN_08D5FBC8 reads an NFS-backed track through
    the pool word 0x08D5FD1C (= NFS fread 0x08D72204, loaded once at
    0x08D5FC7C); a USB track goes through 0x08D5FFA8 instead;
  * an analysis file's folder is a pure function of the AUDIO path that
    rekordbox wrote into PPTH:  N = hash(path) % 200003 (fw 0x08D08878,
    h = (h*0x5BC9 + c)*0x93B5 + c per UTF-16 char), folder P%03X = bits
    0,2,6,7,9,13,16 of N, file PIONEER/USBANLZ/P%03X/%08X/ANLZ0000.EXT.
    Verified on a real export: 1396 of 1396 tracks.

The module (all in the deck-surveyed free tail of the loader's 64 KB at
0x09890000, which ends at 0x098993A8; LBASE must still read zero):
  lopen  pool 0x08DDF7F4 -> lopen: an NFS open for READ of a file outside
         the PIONEER tree = an audio file. A load is confirmed only when
         the deck RE-OPENS the same file (CONFIRM_MS / PRE_MS below); then
         copy its path, bump GEN, set PEND. No file I/O here.
  lread  the 5 NFS fread pool words (READ_HOOKS) -> lrs0..4 -> lread: on
         the first NFS read after that (a tas.b claim keeps it to one
         task), call lfetch, then the stock NFS read. Those tasks do
         blocking NFS I/O by design.
  lfetch hash the path with the firmware's own function, build
         "L:/PIONEER/USBANLZ/P.../.../ANLZ0000.EXT", open it with the
         generic fopen (same drive letter -> same NFS mount), walk the
         sections and read PWV4 / PWV5 / PWV6 straight into the RGB
         snapshot slots (tag word cleared first, written last). If another
         Link open happened meanwhile, nothing is published.
  lsnap  replaces the overview cave's snapshot routine (rgb_detail
         COPY_SRC, reached through ONE pool word inside the overview cave)
         with the same code plus one call: on a new track, lnew decides
         whether it is the Link track (an NFS audio open less than
         WINDOW_MS ago not yet bound to a track). Link track -> do not
         copy from the (unrelated) USB ANLZ context; keep the fetched
         slots, or blank them until the fetch lands. Otherwise lfix.
  lfix   copy the USB context's PWV4/PWV5/PWV6 only if it is the LOADED
         track (overview whiteness equal on all 400 columns, heights
         +-H_TOL, PWV5 length +-LEN_TOL vs the player's length word);
         else blank (stock blue), never another track's colours.
  lcol   pool 0x08DC3D94 (centre painter -> DCAVE): while unverified, rerun
         lfix at most every FIX_MS, then DCAVE as before.
  See docs/LINKRGB.md.

ENABLE (V_EN): 0 = pass-through, 1 = log NFS opens only (no fetch),
3 = log + fetch. The installer writes it last.

  linkrgb.py plan        (sizes, addresses, hook words)
"""
import os
import struct
import sys

sys.path[:0] = [".", "tools"]
from shasm import assemble
import rgb_overview as RO
import rgb_detail as RD

# ---- firmware 1.15 -------------------------------------------------------
POOL_OPEN, ORIG_OPEN = 0x08DDF7F4, 0x08D712E8   # generic fopen -> NFS open
POOL_READ, ORIG_READ = 0x08D5FD1C, 0x08D72204   # media server -> NFS fread
# Every caller of NFS fread(r4 buf, r5 bytes, r6 nfs handle), each through a
# pool word loaded once. Deck 2026-11-18: with only the media server hooked,
# 14 Link loads and 0 reads -- the track is read by one of the others. All
# five are hooked; the first read after a Link open does the fetch, and a
# per-site counter shows which path the deck uses.
READ_HOOKS = ((0x08D5FD1C, "media server", True),   # stock jsr @r7: r7 = fn
              (0x08D2C474, "reader 0x08D2C212", False),
              (0x08D2CFDC, "reader 0x08D2CE4C", False),
              (0x08D2D300, "reader 0x08D2D12A", False),
              (0x08DDFFE4, "generic fread", False))
FW_FOPEN = 0x08DDF53C       # (wpath, mode, opts|0) -> handle | 0
FW_FREAD = 0x08DDFC96       # (buf, size, count, h, [sp]=req) -> items
FW_FSEEK = 0x08DE00D6       # (h, off, whence, req) -> 0 ok
FW_FCLOSE = 0x08DDF992      # (h)
FW_HASH = 0x08D08878        # (wstr) -> hash % 200003 (ANLZ folder number)
FW_REQ = 0xA80B84A0         # the firmware's own default request block
SYS_MS = 0x0B704A80         # 1 ms system clock (TMU ISR 0x08D632C0)
WINDOW_MS = 8000            # NFS audio open -> new-track pairing window
CONFIRM_MS = 15000          # the deck RE-OPENS the track it really loaded,
                            # 1.5-1.7 s after the first open (every load on
                            # the deck, 2026-11-18) -- 5.8 s when both decks
                            # load the same file at once; it never re-opens the
                            # neighbouring track it pre-opens ~100-250 ms
                            # later, nor tracks skipped past. Only a file
                            # opened twice within this window is fetched.
CHUNK = 0x8000              # section bodies are read in 32 KB pieces; a newer
                            # load between two pieces stops the fetch
TAB_N = 4                   # first opens remembered: {N, ms}
PRE_MS = 700                # an open this soon after ANOTHER file's open is
                            # the deck pre-opening a list neighbour (+100-250
                            # ms on the deck). Skipping forward, that neighbour
                            # is the track just left -- already opened, so its
                            # pre-open looks like a re-open: never a confirm.

# ---- "the colours belong to the loaded track" (deck 2026-11-18: SEARCH
# forward then back left track 2's colours on track 1 -- the stock snapshot
# copied the shared ANLZ buffer before the deck refilled it). The loaded
# track's length in 1/150 s equals its PWV5 entry count (deck: 69304 =
# 462.0 s = the buffer's PWV5, while the screen showed a 413.7 s track).
TRACK_LEN = 0x0C0E6D1C      # loaded track length, 1/150 s (phase meter notes)
# Length alone is NOT an identity: on a 1396-track library ~110 pairs of
# different songs share an exact PWV5 length; the BPM word next to it
# (0x0C0E6D10, x10) follows the tempo fader. What is exact: the loaded
# track's overview the deck keeps for the painter (PREVIEW, 400 columns as
# {height, whiteness} byte pairs) vs the buffer's own PWAV, kept in the
# context at +0xD9C (header, little-endian) / +0xDB0 (400 B, h | w<<5) --
# found on the deck 2026-11-18 (identity_probe: 16/16 B at ctx+0xDB0).
# Both must hold before a copy: overview equal AND the buffer's PWV5 length
# equal to the PLAYER's length word (the player keeps that for the loaded
# track; PREVIEW changed between two deck reads 2026-11-18, so it alone is
# not trusted to stay the loaded track's while browsing).
LEN_TOL = 8                 # PWV5 entries vs the length word (~50 ms)
CTX_PWAV = 0xD9C            # context: PWAV header {tag, 20, 420, count}
PWAV_N = 400
H_TOL = 3                   # the deck rescales some tracks' overview heights
                            # (deck 2026-11-18, a 579 s track: PREVIEW = buffer
                            # +2 on 333 columns, +1 on 21, equal on 46; the
                            # whiteness equal on all 400). Whiteness must be
                            # equal, heights within +-H_TOL, on every column.
PREVIEW = RD.PREVIEW        # 0x0BA3BCCC
FIX_MS = 100                # re-check at most this often while mismatched
CENTRE_POOL = RD.POOL       # 0x08DC3D94: centre painter -> DCAVE (per column)

TAG_PMAI = struct.unpack("<I", b"PMAI")[0]
TAG_PWAV = struct.unpack("<I", b"PWAV")[0]
TAG_PWV4 = struct.unpack("<I", b"PWV4")[0]
TAG_PWV5 = struct.unpack("<I", b"PWV5")[0]
TAG_PWV6 = struct.unpack("<I", b"PWV6")[0]

# ---- our memory ------------------------------------------------------------
LBASE = int(os.environ.get("LINKRGB_BASE", "0x0989E000"), 0)
CODE_MAX = 0xC00
LV = LBASE + CODE_MAX
V = dict(GEN=0x00, BOUND=0x04, TOPEN=0x08, PEND=0x0C, DONE=0x10, T0=0x14,
         EN=0x18, NOPEN=0x1C, NAUD=0x20, NOK=0x24, NFAIL=0x28, WHY=0x2C,
         N=0x30, MS=0x34, NLINK=0x38, NLOCAL=0x3C, CNT=0x40, SECT=0x44,
         HDR=0x48, LOGI=0x58, MODE_R=0x5C, PION=0x60, TLAST=0x70, NDUP=0x74,
         CLAIM=0x78, WHO=0x7C, SITE=0x80, NFIRST=0x98, CURN=0x9C, NCONF=0xA0,
         TAB=0xA4, TABI=0xC4, CDST=0xC8, CREM=0xCC, LASTN=0xD0, NPRE=0xD4,
         OKLEN=0xD8, FIXT=0xDC, LLEN=0xE0, NFIX=0xE4, NBLANK=0xE8)
V = {k: LV + o for k, o in V.items()}
V_PATH = LV + 0x100          # UTF-16, PATH_MAX chars + NUL
PATH_MAX = 255
V_EXT = LV + 0x300           # UTF-16 analysis path (template below)
V_LOG = LV + 0x400           # 4 x 128 B: u32 (audio<<31 | ms & 0xFFFFFF), path
LOG_N, LOG_SZ, LOG_CHARS = 4, 128, 60
DATA_END = V_LOG + LOG_N * LOG_SZ
WHY = {0: "-", 1: "analysis file not found (fopen)", 2: "no colour sections",
       3: "superseded by a newer Link load", 4: "not an ANLZ file"}

EXT_TEMPLATE = "?:/PIONEER/USBANLZ/P000/00000000/ANLZ0000.EXT"
P_AT = EXT_TEMPLATE.index("/P0") + 2          # 20: three hex digits
N_AT = EXT_TEMPLATE.index("/00000000") + 1    # 24: eight hex digits
SCRAMBLE = ((0, 0), (2, 1), (6, 2), (7, 3), (9, 4), (13, 5), (16, 6))


def p_folder(n):
    return sum(((n >> s) & 1) << d for s, d in SCRAMBLE)


def anlz_hash(path):
    h = 0
    for ch in path:
        c = ord(ch)
        h = (h * 0x5BC9 + c) & 0xFFFFFFFF
        h = (h * 0x93B5 + c) & 0xFFFFFFFF
    return h % 200003


def ext_path(letter, audio_path):
    n = anlz_hash(audio_path)
    return f"{letter}:/PIONEER/USBANLZ/P{p_folder(n):03X}/{n:08X}/ANLZ0000.EXT"


def _scramble_src():
    out = ["  mov #0,r10"]
    for s, d in SCRAMBLE:
        out.append("  mov r12,r0")
        if s:
            out += [f"  mov #{-s},r1", "  shld r1,r0"]
        out.append("  and #1,r0")
        if d:
            out += [f"  mov #{d},r1", "  shld r1,r0"]
        out.append("  or r0,r10")
    return "\n".join(out)


def _inc(name):
    return (f"  mov.l @({V[name]:#x},pc),r1\n  mov.l @r1,r0\n"
            f"  add #1,r0\n  mov.l r0,@r1")


def _site_stubs():
    """One entry per read hook: count the call, then the common lread.
    r0-r3 are free at a call (caller-saved, not arguments)."""
    out = []
    for i, (_, what, r7fn) in enumerate(READ_HOOKS):
        out += [f"lrs{i}:                       ; {what}",
                f"  mov.l @({V['SITE'] + 4 * i:#x},pc),r1",
                "  mov.l @r1,r0", "  add #1,r0", "  mov.l r0,@r1"]
        if r7fn:      # the stock call site leaves the callee's address in r7
            out.append(f"  mov.l @({ORIG_READ:#x},pc),r7")
        out += ["  bra lread", f"  mov #{i + 1},r2", "  .pool"]
    return "\n".join(out)


def _snap_src():
    """rgb_detail's snapshot routine: same new-track detection, then the Link
    decision, then a VERIFIED copy (lfix) instead of three blind copies."""
    src = RD.COPY_SRC
    anchor = "  mov.l r5,@r3\n"
    assert src.count(anchor) == 1, "COPY_SRC changed: re-check the insertion"
    i = src.index(anchor) + len(anchor)
    j = src.index("sdone:")
    body = src[i:j]
    assert body.count("bsr cp1") == 3, "COPY_SRC changed: re-check the copies"
    new = ("  bsr lnew\n  nop\n  bt sdone\n"
           f"  mov.l @({V['OKLEN']:#x},pc),r1\n  mov #-1,r0\n"
           "  mov.l r0,@r1              ; re-verify for the new track\n"
           "  bsr lfix\n  nop\n")
    return src[:i] + new + src[j:]


def source():
    S = RO
    return f"""
{_snap_src()}
  .pool

lnew:                       ; new track (snapshot hash changed): Link track?
  mov.l @({V['GEN']:#x},pc),r1
  mov.l @r1,r2
  tst r2,r2
  bt ln_loc                 ; never saw a Link audio open
  mov.l @({V['BOUND']:#x},pc),r1
  mov.l @r1,r0
  cmp/eq r2,r0
  bt ln_loc                 ; that open already belongs to an earlier track
  mov.l @({SYS_MS:#x},pc),r1
  mov.l @r1,r0
  mov.l @({V['TOPEN']:#x},pc),r1
  mov.l @r1,r1
  sub r1,r0                 ; ms since the open
  mov.l @({WINDOW_MS:#x},pc),r1
  cmp/hs r1,r0
  bt ln_loc                 ; too long ago: a local track
  mov.l @({V['BOUND']:#x},pc),r1
  mov.l r2,@r1
{_inc('NLINK')}
  mov.l @({V['DONE']:#x},pc),r1
  mov.l @r1,r0
  cmp/eq r2,r0
  bt ln_keep                ; fetched already: the slots are this track's
  mov #0,r0                 ; else blank them (stock blue) until it lands
  mov.l @({S.SLOT4:#x},pc),r1
  mov.l r0,@r1
  mov.l @({S.SLOT5:#x},pc),r1
  mov.l r0,@r1
  mov.l @({S.SLOT6:#x},pc),r1
  mov.l r0,@r1
ln_keep:
  mov #1,r0
  rts
  cmp/eq #1,r0              ; T = 1: skip the USB-context copies
ln_loc:
{_inc('NLOCAL')}
  mov #-1,r0                ; the next Link load of any file is new again
  mov.l @({V['CURN']:#x},pc),r1
  mov.l r0,@r1
  mov.l @({V['LLEN']:#x},pc),r1
  mov.l r0,@r1              ; and the slots are no longer the Link fetch's
  mov #0,r0
  rts
  cmp/eq #1,r0              ; T = 0: stock snapshot
  .pool

lfix:                       ; make the slots the LOADED track's, or blank
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l @({TRACK_LEN:#x},pc),r1
  mov.l @r1,r8              ; r8 = loaded track length (1/150 s)
  tst r8,r8
  bt lx_out                 ; no track
  mov.l @({RO.SLOT5:#x},pc),r4
  bsr secn
  nop
  mov r0,r9                 ; r9 = what the screen shows (entries, or -1)
  mov.l @({V['DONE']:#x},pc),r1
  mov.l @r1,r0
  tst r0,r0
  bt lx_ctx
  mov.l @({V['GEN']:#x},pc),r1
  mov.l @r1,r1
  cmp/eq r1,r0
  bf lx_ctx
  mov.l @({V['LLEN']:#x},pc),r1
  mov.l @r1,r0
  cmp/eq r9,r0
  bt lx_ok                  ; the current Link track's own fetch: keep
lx_ctx:
  bsr ctxpw
  nop
  bf lx_blank               ; no context, or another track's overview
  mov.l @({TAG_PWV5:#x},pc),r4
  bsr ctxsec
  nop
  tst r0,r0
  bt lx_blank
  mov r0,r4
  bsr secn
  nop
  mov r8,r1                 ; |buffer length - player's length| <= LEN_TOL
  sub r1,r0
  mov #{LEN_TOL},r1
  add r1,r0
  mov #{2 * LEN_TOL},r1
  cmp/hs r0,r1
  bf lx_blank               ; and the PLAYER's track length too
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
{_inc('NFIX')}
lx_ok:
  mov.l @({V['OKLEN']:#x},pc),r1
  mov.l r8,@r1
  bra lx_out
  nop
lx_blank:                   ; nothing of the loaded track's: stock blue, retry
  mov.l @({RO.SLOT5:#x},pc),r1
  mov.l @r1,r0
  tst r0,r0
  bt lx_out                 ; already blank
  mov #0,r0
  mov.l r0,@r1
  mov.l @({RO.SLOT4:#x},pc),r1
  mov.l r0,@r1
  mov.l @({RO.SLOT6:#x},pc),r1
  mov.l r0,@r1
{_inc('NBLANK')}
lx_out:
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  nop
  .pool

secn:                       ; r0 = PWV5 entry count of the section at r4, or -1
  sts.l pr,@-r15
  mov.l @r4,r0
  mov.l @({TAG_PWV5:#x},pc),r1
  cmp/eq r1,r0
  bf sn_no
  add #4,r4
  bsr be32
  nop
  mov r0,r2                 ; header length
  bsr be32
  nop
  sub r2,r0
  shlr r0                   ; 2 bytes per entry
  lds.l @r15+,pr
  rts
  nop
sn_no:
  mov #-1,r0
  lds.l @r15+,pr
  rts
  nop

ctxpw:                      ; T = the USB context's overview == the loaded
  sts.l pr,@-r15            ; track's (PREVIEW), all 400 columns
  mov.l @({RO.CTX_TAB:#x},pc),r3
  bsr c1ok
  mov.l @r3,r3
  bf pw_no
  mov.l @({CTX_PWAV:#x},pc),r2
  add r3,r2                 ; ctx + 0xD9C: the context's PWAV
  mov.l @r2,r0
  mov.l @({TAG_PWAV:#x},pc),r1
  cmp/eq r1,r0
  bf pw_no
  mov.l @(8,r2),r0
  mov.l @({20 + PWAV_N:#x},pc),r1
  cmp/eq r1,r0
  bf pw_no                  ; not a 400-column overview: cannot verify
  add #20,r2
  mov.l @({PREVIEW:#x},pc),r3
  mov.l @({PWAV_N:#x},pc),r7
pw_l:
  mov.b @r3+,r0             ; PREVIEW: height
  and #31,r0
  mov r0,r6
  mov.b @r3+,r0             ; ... whiteness
  and #7,r0
  mov r0,r1
  mov.b @r2+,r0             ; buffer: h | w<<5
  extu.b r0,r0
  mov r0,r5
  shlr2 r0
  shlr2 r0
  shlr r0
  and #7,r0
  cmp/eq r1,r0
  bf pw_no                  ; whiteness: exactly equal
  mov r5,r0
  and #31,r0
  sub r0,r6
  add #{H_TOL},r6
  mov #{2 * H_TOL},r0
  cmp/hs r6,r0
  bf pw_no                  ; height: within +-H_TOL
  dt r7
  bf pw_l
  mov #1,r0
  lds.l @r15+,pr
  rts
  cmp/eq #1,r0
pw_no:
  mov #0,r0
  lds.l @r15+,pr
  rts
  cmp/eq #1,r0

ctxsec:                     ; r0 = the USB context's section tagged r4, or 0
  sts.l pr,@-r15
  mov.l @({RO.CTX_TAB:#x},pc),r3
  bsr c1ok
  mov.l @r3,r3
  bf cs_no
  mov.l @({RO.IDX_OFF:#x},pc),r0
  add r0,r3
  mov.l @r3+,r7
  mov #32,r0
  cmp/hi r0,r7
  bt cs_no
cs_l:
  tst r7,r7
  bt cs_no
  mov.l @(4,r3),r0
  cmp/eq r4,r0
  bt cs_f
  add #16,r3
  bra cs_l
  add #-1,r7
cs_f:
  bsr c1ok
  mov.l @(12,r3),r3
  bf cs_no
  mov.l @r3,r0
  cmp/eq r4,r0
  bf cs_no
  mov r3,r0
  lds.l @r15+,pr
  rts
  nop
cs_no:
  mov #0,r0
  lds.l @r15+,pr
  rts
  nop
  .pool

lcol:                       ; pool 0x08DC3D94: the centre painter, per column
  mov.l @({TRACK_LEN:#x},pc),r3
  mov.l @r3,r3
  mov.l @({V['OKLEN']:#x},pc),r0
  mov.l @r0,r0
  cmp/eq r3,r0
  bt lc_go                  ; verified for this track: straight on
  mov.l @({SYS_MS:#x},pc),r0
  mov.l @r0,r0
  mov.l @({V['FIXT']:#x},pc),r6
  mov.l @r6,r7
  mov r0,r4
  sub r7,r4
  mov #{FIX_MS},r7
  cmp/hs r7,r4
  bf lc_go                  ; checked < FIX_MS ago
  mov.l r0,@r6
  sts.l pr,@-r15
  sts.l macl,@-r15
  sts.l mach,@-r15
  mov.l r1,@-r15
  mov.l r2,@-r15
  mov.l r5,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov.l r12,@-r15
  mov.l r13,@-r15
  mov.l r14,@-r15
  bsr lfix
  nop
  mov.l @r15+,r14
  mov.l @r15+,r13
  mov.l @r15+,r12
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  mov.l @r15+,r5
  mov.l @r15+,r2
  mov.l @r15+,r1
  lds.l @r15+,mach
  lds.l @r15+,macl
  lds.l @r15+,pr
lc_go:
  mov.l @({RD.DCAVE:#x},pc),r0
  jmp @r0                   ; DCAVE as if the painter had called it
  nop
  .pool

lopen:                      ; pool 0x08DDF7F4: NFS open(r4 wpath,r5,r6 mode,r7)
  mov.l r4,@-r15
  mov.l r5,@-r15
  mov.l r6,@-r15
  mov.l r7,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  sts.l pr,@-r15
  mov r4,r10
{_inc('NOPEN')}
  mov.l @({V['EN']:#x},pc),r1
  mov.l @r1,r8
  tst r8,r8
  bt lo_xo
  mov.b @r6,r0              ; read-only opens: "r", "rb" (not "r+")
  cmp/eq #114,r0
  bf lo_xo
  mov.b @(1,r6),r0
  cmp/eq #43,r0
  bt lo_xo
  mov #0,r9                 ; r9 = 1: audio (outside the PIONEER tree)
  mov r10,r1
  add #4,r1                 ; past "L:"
  mov.w @r1,r0
  extu.w r0,r0
  cmp/eq #47,r0             ; '/'
  bf lo_cls
  add #2,r1
  mov.w @r1,r0
  extu.w r0,r0
  cmp/eq #46,r0             ; '.PIONEER'
  bf lo_np
  add #2,r1
lo_np:
  mov.l @({V['PION']:#x},pc),r2
  mov #7,r3
lo_pl:
  mov.w @r1+,r0
  extu.w r0,r0
  mov r0,r7
  add #-97,r7
  mov #26,r6
  cmp/hs r6,r7              ; not 'a'..'z'
  bt lo_up
  add #-32,r0
lo_up:
  mov.w @r2+,r5
  extu.w r5,r5
  cmp/eq r5,r0
  bf lo_aud
  dt r3
  bf lo_pl
  mov.w @r1,r0              ; "PIONEER" then '/' or end: the PIONEER tree
  extu.w r0,r0              ; (analysis / database / artwork), not audio
  tst r0,r0
  bt lo_cls
  cmp/eq #47,r0
  bt lo_cls
lo_aud:
  mov #1,r9
lo_cls:
  mov r8,r0
  tst #1,r0
  bt lo_nolog
  mov.l @({V['LOGI']:#x},pc),r1
  mov.l @r1,r0
  add #1,r0
  mov.l r0,@r1
  add #-1,r0
  and #3,r0
  shll2 r0
  shll2 r0
  shll2 r0
  shll r0                   ; * 128
  mov.l @({V_LOG:#x},pc),r5
  add r0,r5
  mov.l @({SYS_MS:#x},pc),r1
  mov.l @r1,r0
  shll8 r0
  shlr8 r0
  mov r9,r1
  rotr r1
  or r1,r0
  mov.l r0,@r5
  add #4,r5
  mov r10,r4
  mov #{LOG_CHARS},r6
  bsr cpy
  nop
lo_nolog:
  tst r9,r9
  bt lo_xo
{_inc('NAUD')}
  mov r10,r4
  add #4,r4                 ; "/Contents/..." (past "L:")
  mov.l @({FW_HASH:#x},pc),r0
  jsr @r0                   ; N: the file's identity (pure function)
  nop
  mov r0,r9
  mov.l @({V['CURN']:#x},pc),r1
  mov.l @r1,r0
  cmp/eq r9,r0
  bf lo_tab
{_inc('NDUP')}
  bra lo_out                ; the current Link track again (re-opens while
  nop                       ; it plays): nothing to do
lo_xo:                      ; (in reach of the early exits above)
  bra lo_out
  nop
lo_tab:
  mov.l @({SYS_MS:#x},pc),r1
  mov.l @r1,r7              ; now
  mov #0,r5                 ; r5 = 1: right after another file's open
  mov.l @({V['TLAST']:#x},pc),r1
  mov.l @r1,r0
  mov.l r7,@r1
  mov r7,r4
  sub r0,r4                 ; ms since the previous audio open
  mov.l @({V['LASTN']:#x},pc),r1
  mov.l @r1,r0
  mov.l r9,@r1
  cmp/eq r9,r0
  bt lo_t0                  ; the previous open was this same file
  mov.l @({PRE_MS:#x},pc),r0
  cmp/hs r0,r4
  bt lo_t0
  mov #1,r5
lo_t0:
  mov.l @({V['TAB']:#x},pc),r2
  mov #{TAB_N},r3
lo_tl:
  mov.l @r2,r0
  cmp/eq r9,r0
  bf lo_tn
  mov.l @(4,r2),r0
  mov r7,r1
  sub r0,r1                 ; ms since this file's first open
  mov.l @({CONFIRM_MS:#x},pc),r6
  cmp/hs r6,r1
  bt lo_tn                  ; first open too long ago
  tst r5,r5
  bt lo_conf                ; opened again soon: the deck really loaded it
{_inc('NPRE')}
  bra lo_out                ; ... but right after another file: a pre-open
  nop
lo_tn:
  add #8,r2
  dt r3
  bf lo_tl
  mov.l @({V['TABI']:#x},pc),r1
  mov.l @r1,r0              ; a first open: remember it, do nothing
  add #1,r0
  mov.l r0,@r1
  add #-1,r0
  and #{TAB_N - 1},r0
  shll2 r0
  shll r0
  mov.l @({V['TAB']:#x},pc),r2
  add r0,r2
  mov.l r9,@r2
  mov.l r7,@(4,r2)
{_inc('NFIRST')}
  bra lo_out
  nop
lo_conf:
  mov #-1,r0
  mov.l r0,@r2              ; entry used
  mov r10,r4                ; full path (also in log-only mode: --status
  mov.l @({V_PATH:#x},pc),r5   ; shows the analysis path it would open)
  mov.l @({PATH_MAX:#x},pc),r6
  bsr cpy
  nop
  bf lo_out                 ; path too long: no fetch
  mov.l @({V['CURN']:#x},pc),r1
  mov.l r9,@r1
{_inc('NCONF')}
  mov r8,r0
  tst #2,r0
  bt lo_out
  mov.l @({SYS_MS:#x},pc),r1
  mov.l @r1,r2
  mov.l @({V['TOPEN']:#x},pc),r1
  mov.l r2,@r1
  mov.l @({V['GEN']:#x},pc),r1
  mov.l @r1,r0
  add #1,r0
  mov.l r0,@r1
  mov.l @({V['PEND']:#x},pc),r1
  mov.l r0,@r1              ; publish the request last
lo_out:
  lds.l @r15+,pr
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  mov.l @r15+,r7
  mov.l @r15+,r6
  mov.l @r15+,r5
  mov.l @r15+,r4
  mov.l @({ORIG_OPEN:#x},pc),r0
  jmp @r0
  nop
  .pool

cpy:                        ; UTF-16 copy r4 -> r5, at most r6 chars + NUL
  mov.w @r4+,r0             ; T = 1 when the whole string fitted
  mov.w r0,@r5
  add #2,r5
  extu.w r0,r0
  tst r0,r0
  bt cp_ok
  dt r6
  bf cpy
  mov #0,r0
  mov.w r0,@r5
  rts
  cmp/eq #1,r0
cp_ok:
  mov #1,r0
  rts
  cmp/eq #1,r0

wcmp:                       ; T = UTF-16 strings r4, r5 equal (<= 256 chars)
  mov.l @({PATH_MAX + 1:#x},pc),r6
wc_l:
  mov.w @r4+,r0
  mov.w @r5+,r1
  cmp/eq r0,r1
  bf wc_ne
  extu.w r0,r0
  tst r0,r0
  bt wc_eq
  dt r6
  bf wc_l
wc_ne:
  mov #0,r0
  rts
  cmp/eq #1,r0
wc_eq:
  mov #1,r0
  rts
  cmp/eq #1,r0
  .pool

{_site_stubs()}
lread:                      ; every NFS fread call site -> here
  mov.l @({V['PEND']:#x},pc),r0
  mov.l @r0,r0
  tst r0,r0
  bt lr_go
  mov.l @({V['CLAIM']:#x},pc),r1
  .long {0x0009411B:#x}      ; tas.b @r1 ; nop  (atomic: one task fetches)
  bf lr_go                  ; another task is already fetching
  mov.l @({V['WHO']:#x},pc),r1
  mov.l r2,@r1              ; which call site triggered this fetch (1..5)
  sts.l pr,@-r15
  sts.l macl,@-r15
  sts.l mach,@-r15
  mov.l r1,@-r15
  mov.l r2,@-r15
  mov.l r3,@-r15
  mov.l r4,@-r15
  mov.l r5,@-r15
  mov.l r6,@-r15
  mov.l r7,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov.l r12,@-r15
  mov.l r13,@-r15
  mov.l r14,@-r15
  bsr lfetch
  nop
  mov.l @r15+,r14
  mov.l @r15+,r13
  mov.l @r15+,r12
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  mov.l @r15+,r7
  mov.l @r15+,r6
  mov.l @r15+,r5
  mov.l @r15+,r4
  mov.l @r15+,r3
  mov.l @r15+,r2
  mov.l @r15+,r1
  lds.l @r15+,mach
  lds.l @r15+,macl
  lds.l @r15+,pr
  mov.l @({V['CLAIM']:#x},pc),r1
  mov #0,r0
  mov.b r0,@r1              ; release
lr_go:
  mov.l @({ORIG_READ:#x},pc),r0
  jmp @r0                   ; r4-r7 as the caller left them
  nop
  .pool

lfetch:                     ; media-server task: read the peer's .EXT
  sts.l pr,@-r15
  mov.l @({V['PEND']:#x},pc),r1
  mov.l @r1,r13             ; r13 = the Link load (generation) we serve
  mov #0,r0
  mov.l r0,@r1
  mov.l @({SYS_MS:#x},pc),r1
  mov.l @r1,r0
  mov.l @({V['T0']:#x},pc),r1
  mov.l r0,@r1
  mov.l @({V_PATH + 4:#x},pc),r4   ; "/Contents/..." (past "L:")
  mov.l @({FW_HASH:#x},pc),r0
  jsr @r0
  nop
  mov r0,r12                ; N
  mov.l @({V['N']:#x},pc),r1
  mov.l r0,@r1
  mov.l @({V_PATH:#x},pc),r1
  mov.w @r1,r0              ; the same drive letter (NFS mount)
  mov.l @({V_EXT:#x},pc),r11
  mov.w r0,@r11
{_scramble_src()}
  mov r10,r4
  mov #3,r5
  mov r11,r6
  add #{2 * P_AT},r6
  bsr hexw
  nop
  mov r12,r4
  mov #8,r5
  mov r11,r6
  add #{2 * N_AT},r6
  bsr hexw
  nop
  mov r11,r4
  mov.l @({V['MODE_R']:#x},pc),r5
  mov #0,r6
  mov.l @({FW_FOPEN:#x},pc),r0
  jsr @r0
  nop
  tst r0,r0
  bf lf_open
  bra lf_fail
  mov #1,r0
lf_badx:                    ; (in reach of the header checks below)
  bra lf_bad
  nop
lf_open:
  mov r0,r14                ; handle
  mov #0,r8                 ; sections read OK (bit 1 PWV4, 2 PWV5, 4 PWV6)
  mov.l @({V['HDR']:#x},pc),r4
  mov #12,r6
  bsr rd
  nop
  cmp/eq #12,r0
  bf lf_badx
  mov.l @({V['HDR']:#x},pc),r4
  mov.l @r4,r0
  mov.l @({TAG_PMAI:#x},pc),r1
  cmp/eq r1,r0
  bf lf_badx
  add #4,r4
  bsr be32
  nop
  mov r0,r9                 ; offset of the first section
  mov #32,r10               ; at most 32 sections
lf_sec:
  mov.l @({V['GEN']:#x},pc),r1
  mov.l @r1,r0
  cmp/eq r13,r0
  bf lf_close               ; a newer load: stop before the next section
  mov r9,r5
  bsr sk
  nop
  tst r0,r0
  bf lf_close
  mov.l @({V['HDR']:#x},pc),r4
  mov #12,r6
  bsr rd
  nop
  cmp/eq #12,r0
  bf lf_close
  mov.l @({V['HDR'] + 8:#x},pc),r4
  bsr be32
  nop
  mov r0,r11                ; section length (header included)
  mov #12,r1
  cmp/hs r1,r0
  bf lf_close
  mov.l @({V['HDR']:#x},pc),r1
  mov.l @r1,r2              ; tag
  mov.l @({TAG_PWV4:#x},pc),r0
  cmp/eq r0,r2
  bf lf_n4
  mov.l @({S.SLOT4:#x},pc),r4
  mov.l @({S.SLOT4_MAX:#x},pc),r5
  bra lf_take
  mov #1,r6
lf_n4:
  mov.l @({TAG_PWV5:#x},pc),r0
  cmp/eq r0,r2
  bf lf_n5
  mov.l @({S.SLOT5:#x},pc),r4
  mov.l @({S.SLOT5_MAX:#x},pc),r5
  bra lf_take
  mov #2,r6
lf_n5:
  mov.l @({TAG_PWV6:#x},pc),r0
  cmp/eq r0,r2
  bf lf_next
  mov.l @({S.SLOT6:#x},pc),r4
  mov.l @({S.SLOT6_MAX:#x},pc),r5
  mov #4,r6
lf_take:                    ; r4 slot, r5 slot size, r6 bit, r11 length
  cmp/hi r5,r11
  bt lf_next                ; too big for the slot: leave it
  mov r6,r12
  mov #0,r0
  mov.l r0,@r4              ; invalidate (tag word) before the body
  mov.l @({V['HDR']:#x},pc),r1
  mov.l @(4,r1),r0
  mov.l r0,@(4,r4)
  mov.l @(8,r1),r0
  mov.l r0,@(8,r4)
  add #12,r4
  mov.l @({V['CDST']:#x},pc),r1
  mov.l r4,@r1
  mov r11,r6
  add #-12,r6
  mov.l @({V['CREM']:#x},pc),r1
  mov.l r6,@r1
lf_ch:                      ; body in CHUNK pieces
  mov.l @({V['CREM']:#x},pc),r1
  mov.l @r1,r6
  tst r6,r6
  bt lf_got
  mov.l @({CHUNK:#x},pc),r0
  cmp/hi r0,r6
  bf lf_c1
  mov r0,r6
lf_c1:
  mov.l @({V['CNT']:#x},pc),r1
  mov.l r6,@r1
  mov.l @({V['CDST']:#x},pc),r1
  mov.l @r1,r4
  bsr rd
  nop
  mov.l @({V['CNT']:#x},pc),r1
  mov.l @r1,r1
  cmp/eq r1,r0
  bf lf_next                ; short read: this section stays invalid
  mov.l @({V['CDST']:#x},pc),r2
  mov.l @r2,r3
  add r0,r3
  mov.l r3,@r2
  mov.l @({V['CREM']:#x},pc),r2
  mov.l @r2,r3
  sub r0,r3
  mov.l r3,@r2
  mov.l @({V['GEN']:#x},pc),r1
  mov.l @r1,r0
  cmp/eq r13,r0
  bf lf_close               ; a newer load: stop now (publishes nothing)
  bra lf_ch
  nop
lf_got:
  or r12,r8
lf_next:
  add r11,r9
  dt r10
  bf lf_sec
lf_close:
  mov r14,r4
  mov.l @({FW_FCLOSE:#x},pc),r0
  jsr @r0
  nop
  mov.l @({V['GEN']:#x},pc),r1
  mov.l @r1,r0
  cmp/eq r13,r0
  bf lf_stale               ; another Link load started: publish nothing
  tst r8,r8
  bt lf_nosec
  mov r8,r0
  tst #1,r0
  bt lf_p5
  mov.l @({S.SLOT4:#x},pc),r1
  mov.l @({TAG_PWV4:#x},pc),r2
  mov.l r2,@r1
lf_p5:
  mov r8,r0
  tst #2,r0
  bt lf_p6
  mov.l @({S.SLOT5:#x},pc),r1
  mov.l @({TAG_PWV5:#x},pc),r2
  mov.l r2,@r1
lf_p6:
  mov r8,r0
  tst #4,r0
  bt lf_pd
  mov.l @({S.SLOT6:#x},pc),r1
  mov.l @({TAG_PWV6:#x},pc),r2
  mov.l r2,@r1
lf_pd:
  mov.l @({V['SECT']:#x},pc),r1
  mov.l r8,@r1
  mov.l @({S.SLOT5:#x},pc),r4
  bsr secn
  nop
  mov.l @({V['LLEN']:#x},pc),r1
  mov.l r0,@r1              ; the slots now hold this Link fetch
  mov.l @({V['DONE']:#x},pc),r1
  mov.l r13,@r1
{_inc('NOK')}
  mov.l @({SYS_MS:#x},pc),r1
  mov.l @r1,r0
  mov.l @({V['T0']:#x},pc),r1
  mov.l @r1,r1
  sub r1,r0
  mov.l @({V['MS']:#x},pc),r1
  mov.l r0,@r1
  lds.l @r15+,pr
  rts
  nop
lf_bad:
  mov r14,r4
  mov.l @({FW_FCLOSE:#x},pc),r0
  jsr @r0
  nop
  bra lf_fail
  mov #4,r0
lf_stale:
  bra lf_fail
  mov #3,r0
lf_nosec:
  mov #2,r0
lf_fail:
  mov.l @({V['WHY']:#x},pc),r1
  mov.l r0,@r1
{_inc('NFAIL')}
  lds.l @r15+,pr
  rts
  nop
  .pool

rd:                         ; fread(r4 buf, 1, r6 count, r14 handle, req)
  sts.l pr,@-r15
  mov.l @({FW_REQ:#x},pc),r0
  mov.l r0,@-r15
  mov #1,r5
  mov r14,r7
  mov.l @({FW_FREAD:#x},pc),r0
  jsr @r0
  nop
  add #4,r15
  lds.l @r15+,pr
  rts
  nop
sk:                         ; fseek(r14 handle, r5 offset, SEEK_SET, req)
  sts.l pr,@-r15
  mov r14,r4
  mov #0,r6
  mov.l @({FW_REQ:#x},pc),r7
  mov.l @({FW_FSEEK:#x},pc),r0
  jsr @r0
  nop
  lds.l @r15+,pr
  rts
  nop
be32:                       ; r0 = big-endian u32 at r4
  mov.b @r4+,r0
  extu.b r0,r1
  mov.b @r4+,r0
  extu.b r0,r0
  shll8 r1
  or r0,r1
  mov.b @r4+,r0
  extu.b r0,r0
  shll8 r1
  or r0,r1
  mov.b @r4+,r0
  extu.b r0,r0
  shll8 r1
  or r0,r1
  rts
  mov r1,r0
hexw:                       ; r5 upper-case hex digits of r4 -> UTF-16 at r6
  mov r5,r0
  add r0,r0
  add r0,r6
hx1:
  mov r4,r0
  and #15,r0
  mov #10,r1
  cmp/hs r1,r0
  bf hx2
  add #7,r0
hx2:
  add #48,r0
  add #-2,r6
  mov.w r0,@r6
  shlr2 r4
  shlr2 r4
  dt r5
  bf hx1
  rts
  nop
  .pool
"""


def _labels():
    """Offsets of the entry points (assemble once, read the label table)."""
    from shasm import Asm
    a = Asm()
    code = a.assemble(source())
    return code, a.labels


def build():
    code, lab = _labels()
    assert len(code) <= CODE_MAX, f"code {len(code)} B > {CODE_MAX}"
    ent = {k: LBASE + lab[k] for k in ("snap", "lopen", "lread", "lcol")}
    ent.update({f"lrs{i}": LBASE + lab[f"lrs{i}"] for i in range(len(READ_HOOKS))})
    for k, a in ent.items():
        assert a % 2 == 0, k
    return code, ent


def data_init():
    """(addr, bytes) the installer writes BEFORE any hook is armed (EN = 0)."""
    w = []
    w.append((V["MODE_R"], b"r\x00\x00\x00"))
    w.append((V["PION"], "PIONEER".encode("utf-16-le")))
    w.append((V_EXT, (EXT_TEMPLATE + "\0").encode("utf-16-le")))
    w.append((V["CURN"], b"\xff" * 4))           # no current Link file
    w.append((V["TAB"], (b"\xff" * 4 + bytes(4)) * TAB_N))   # empty table
    return w


def overview_pool_word(mode="rgb"):
    """Address of the overview cave's pool word holding COPY5 (-> lsnap)."""
    code, _ = RO.plan(RO.MODES[mode])
    hits = [i for i in range(0, len(code) - 3, 4)
            if struct.unpack_from("<I", code, i)[0] == RO.COPY5]
    assert len(hits) == 1, f"COPY5 pool word: {len(hits)} hits"
    return RO.CAVE + hits[0]


def main():
    code, ent = build()
    print(f"linkrgb: {len(code)} B code at {LBASE:#x}, data {LV:#x}..{DATA_END:#x}")
    for k, a in ent.items():
        print(f"  {k:6s} {a:#x}")
    print(f"  hook open  {POOL_OPEN:#x}: {ORIG_OPEN:#x} -> {ent['lopen']:#x}")
    for i, (pool, what, _) in enumerate(READ_HOOKS):
        print(f"  hook read  {pool:#x}: {ORIG_READ:#x} -> {ent[f'lrs{i}']:#x}  ({what})")
    print(f"  hook col   {CENTRE_POOL:#x}: {RD.DCAVE:#x} -> {ent['lcol']:#x}  (centre painter)")
    pw = {m: overview_pool_word(m) for m in RO.MODES}
    print(f"  hook snap  {pw['rgb']:#x}: {RO.COPY5:#x} -> {ent['snap']:#x}  "
          f"(same word in every mode: {len(set(pw.values())) == 1})")


if __name__ == "__main__":
    main()
