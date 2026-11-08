#!/usr/bin/env python3
"""permanent_build.py STOCK.UPD OUT.UPD [--mode rgb] [--banner 1.16]
             [--no-pads] [--no-versions] [--extras all|centre,keyshift,...]
EXPERIMENTAL: bake the mods into an update file ("permanent mode").

!!! NEVER CONFIRMED ON A REAL DECK. Flashing firmware is the one thing in
!!! this project that can leave a deck needing a service centre. Early tests
!!! (Sept 2026) that changed the main firmware showed "MAIN CPU ERROR" and
!!! were recovered by re-flashing the official file; the cause (a block
!!! checksum the old packer did not update) has been fixed since, and every
!!! file this tool writes is checked and BOOTED in the emulator through the
!!! deck's own startup code before it is kept.

What it does, from the user's own official XDJ-700 v1.15 update file:
  * decodes the main firmware block and applies, at the same addresses the
    RAM installers use: overview waveform (mode), key/stem version
    switching, the UTILITY settings rows and the PERFORM pads -- by running each
    mod's own emulator installer against the firmware image instead of
    live memory;
  * --extras: mods whose code lives in RAM OUTSIDE the firmware image
    (centre colours 0x098A0000, phase meter 0x09798000, 8 hot cues
    0x09790000, live stems 0x09836000, live key shift = a patched copy of
    the DSP program at 0x097A0000). Their bytes ride on the END of the
    decoded image (compressed with it) and a 52-byte boot copier, called
    from startup's empty init hook (pool 0x080009E0 -> 0x08CB8714 = rts)
    with the caches off and before the DSP boots, zero-fills each RAM home
    and copies the bytes in. The pool words that arm them (display flush
    chain hc -> pm -> pads, audio read hook, centre painter, DSP program
    source/length, BD8 store NOP) are baked into the image, exactly the
    end state the USB installers leave. Key shift needs no hot patch: the
    deck's own cold DSP boot loads the patched program copy.
    NOT bakeable: theme switching (tables >= 42 KB compressed even for one
    theme) and FLAC/Opus (~120 KB): the flash slot has ~27 KB free after
    the in-image mods, ~16.6 KB after all five extras;
  * version record -> BANNER (UTILITY shows it) and the update banner ->
    BANNER, so the deck's updater accepts it (it needs a different
    version from the installed one);
  * re-compresses with our encoder (37.8 KB tighter than Pioneer's on
    stock, which is the room the code needs), refuses if it does not fit,
    recomputes the block checksums, round-trips the file, and leaves the
    boot stub, block 0 and the PANL section byte-identical to stock.

To go back to stock: flash the OFFICIAL v1.15 file (its version differs
from BANNER, so the updater accepts it).

Writing a file needs --i-know-flashing-can-brick (and read this header
first). The --emu boot-test now also taps through the baked PERFORM
screen and checks every boot-copied byte + hook chain (with XDJ_DSP_EMU=1
also that the real DSP booted the key-shift program); SAVE=x.pkl keeps the
booted snapshot for tools/permanent_extras_emu.py (feature test).
What --emu can NOT prove: a real flash (power loss mid-write, a worn
flash chip) -- that part is inherently deck-side. Never flash
without the official recovery file on hand and a full battery/UPS.

   permanent_build.py STOCK OUT --i-know-flashing-can-brick
   then  permanent_build.py --emu OUT
"""
import argparse
import hashlib
import os
import re
import struct
import sys

sys.path[:0] = [".", "tools", "mods"]
import fwtool                       # noqa: E402
import lzss_boot as L               # noqa: E402

BASE = 0x08000000
OFFICIAL = "73edec9802da51672257c2599efc04209dc92478fcbaa1a0425b3b122e33f99c"
VERSION_AT = 0x740                  # "1.15" in the version record (decoded block 1)


class ImgMem:
    """r8/w8/r16/w16/r32/w32 over the decoded firmware image (SH address
    space at 0x08000000); anything outside the image is refused."""
    def __init__(self, buf):
        self.b = buf

    def _o(self, a, n):
        o = a - BASE
        if not (0 <= o and o + n <= len(self.b)):
            raise ValueError(f"{a:#x} is outside the firmware image")
        return o

    def r8(self, a):
        return self.b[self._o(a, 1)]

    def w8(self, a, v):
        self.b[self._o(a, 1)] = v & 0xFF

    def r16(self, a):
        return struct.unpack_from("<H", self.b, self._o(a, 2))[0]

    def w16(self, a, v):
        struct.pack_into("<H", self.b, self._o(a, 2), v & 0xFFFF)

    def r32(self, a):
        return struct.unpack_from("<I", self.b, self._o(a, 4))[0]

    def w32(self, a, v):
        struct.pack_into("<I", self.b, self._o(a, 4), v & 0xFFFFFFFF)


class SplitMem(ImgMem):
    """ImgMem plus the RAM homes of the USB-only modules. Writes inside a
    declared region (`region()`) are collected instead of refused; the
    boot copier (boot_tail) fills every region at startup (zeros, or the
    firmware bytes it copies) and then copies the collected bytes there.
    Reads of a region return what the deck will hold after the copier ran."""
    def __init__(self, buf):
        super().__init__(buf)
        self.ram = {}
        self.regions = []            # (start, end, copy_src or None, name)

    def region(self, start, end, name, src=None):
        assert start % 4 == 0 and end % 4 == 0 and end > start
        assert start >= BASE + len(self.b) or end <= BASE
        for s, e, _, n in self.regions:
            if start < e and s < end:
                raise ValueError(f"{name} {start:#x}..{end:#x} overlaps {n}")
        self.regions.append((start, end, src, name))

    def _reg(self, a):
        for s, e, src, _ in self.regions:
            if s <= a < e:
                return s, src
        return None

    def baseline(self, a):
        s, src = self._reg(a)
        return 0 if src is None else self.b[src + (a - s) - BASE]

    def r8(self, a):
        if self._reg(a) is None:
            return super().r8(a)
        return self.ram.get(a, self.baseline(a))

    def w8(self, a, v):
        if self._reg(a) is None:
            return super().w8(a, v)
        self.ram[a] = v & 0xFF

    def r16(self, a):
        return self.r8(a) | self.r8(a + 1) << 8

    def w16(self, a, v):
        self.w8(a, v)
        self.w8(a + 1, v >> 8)

    def r32(self, a):
        return self.r16(a) | self.r16(a + 2) << 16

    def w32(self, a, v):
        self.w16(a, v)
        self.w16(a + 2, v >> 16)

    def write(self, a, data):
        for i, x in enumerate(data):
            self.w8(a + i, x)


# ---- mods that live OUTSIDE block1 (copied to their RAM homes at boot) ----
# The USB installers survey + write these areas and then arm one pool word;
# here the pool words are baked into the image and boot_tail() makes the
# areas hold exactly what the installers leave there. Themes (tables.bin:
# >= 42 KB compressed even for ONE theme) and FLAC/Opus (vfile.bin: ~120 KB
# compressed) are each bigger than all the room left in the flash slot
# (~27 KB after the in-image mods): those stay USB-only.
EXTRAS = ("centre", "phasemeter", "hotcue8", "stems", "keyshift", "touchfx",
          "utility")
# keyshift and touchfx share the DSP hook + home: one or the other
DSP_EXCLUSIVE = ("keyshift", "touchfx")
FLUSH_POOL = 0x08C92E20                 # display flush (pads / pm / hc chain)
ENTRY_POOL, ENTRY_STUB = 0x080009E0, 0x08CB8714   # startup's empty init hook
TAIL_ALIGN = 32


def _path(rel):
    try:
        from emu.paths import repo_root
        root = repo_root()
    except Exception:
        root = None
    p = os.path.join(root, rel) if root else rel
    if not os.path.exists(p):
        import shbuild                  # public checkout: build/ is
        t = shbuild.twin(rel)           # gitignored, prebuilt/deck isn't
        if t:
            tp = os.path.join(root, t) if root else t
            if os.path.exists(tp):
                p = tp
    return p


def _syms(rel):
    s = {}
    for line in open(_path(rel)):
        a_, _, n_ = line.split()
        s[n_.lstrip("_")] = int(a_, 16)
    return s


def _blob(rel):
    return open(_path(rel), "rb").read()


def bake_centre(mem, log):
    import rgb_detail as RD
    _, syms = RD.cw_build()
    mem.region(RD.CW_BASE, (syms["end"] + 3) & ~3, "centre colours")
    mem.region(RD.SLOT7, RD.SLOT7 + 4, "centre PWV7 slot header")
    RD.install(mem)
    log(f"centre waveform colours (module copied to {RD.CW_BASE:#x} at boot)")


def bake_phasemeter(mem, log):
    import perform_pads as PP
    s = _syms("build/phasemeter/pm.syms")
    img = _blob("build/phasemeter/pm.bin")
    base = s["pm_entry"]
    end = (max(s["pm_mode"], s["pm_n"], s["pm_layer"]) + 4 + 3) & ~3
    for k, v in s.items():   # every byte we own must be inside the cave
        assert v < end, f"phase-meter symbol {k} at {v:#x} is past {end:#x}"
    mem.region(base, end, "phase meter")
    live = mem.r32(FLUSH_POOL)
    assert live in (PP.ORIG_BLIT, PP.hooks()[1][2]), hex(live)
    mem.write(base, img)
    mem.w32(s["pm_prev"], live)
    mem.w32(s["pm_mode"], 1)
    mem.w32(FLUSH_POOL, base)
    log(f"phase meter ({base:#x}, chains {live:#x})")


def bake_hotcue8(mem, log):
    import perform_pads as PP
    s = _syms("build/hotcue8/hc.syms")
    img = _blob("build/hotcue8/hc.bin")
    base = s["hc_table"]
    assert base == PP.HC_BASE, "hc.bin built for another HC_BASE"
    if mem.r32(PP.PC0) == 0:
        raise SystemExit("REFUSED: 8 hot cues need the PERFORM pads")
    mem.region(base, (s.get("end", s["hc_count"] + 4) + 3) & ~3, "8 hot cues")
    live = mem.r32(FLUSH_POOL)
    live_t = mem.r32(PP.HOOK_TOUCH)
    mem.write(base, img)
    mem.w32(s["hc_prev"], live)
    mem.w32(s["hc_tprev"], live_t)
    mem.w32(s["hc_armed"], 1)
    mem.w32(FLUSH_POOL, s["hc_entry"])
    mem.w32(PP.HOOK_TOUCH, s["hc_tentry"])
    log(f"8 hot cues ({base:#x}, flush chains {live:#x}, touch chains {live_t:#x})")


STEMS_DIRECT = 0x08DDFFD4               # media API direct read slot (stems_apply)
STEMS_POLL, STEMS_POLL_ACC = 0x08C1D56C, 0x08BF2BCC   # fast mute trigger (stems_apply)


def bake_stems(mem, log):
    import perform_pads as PP
    import vredirect as VR
    s = _syms("build/stems/sm.syms")
    img = _blob("build/stems/sm.bin")
    base, end, entry = s["sm_table"], (s["end"] + 3) & ~3, s["sm_fread"]
    if mem.r32(PP.PC0) == 0:
        raise SystemExit("REFUSED: live stems need the PERFORM pads")
    mem.region(base, end, "live stems")
    live = mem.r32(VR.HOOK)
    assert live in (VR.FREAD, VR.VCAVE), hex(live)
    mem.write(base, img)
    mem.w32(s["sm_prev"], live)
    mem.w32(VR.HOOK, entry)
    if mem.r32(STEMS_DIRECT) == VR.FREAD:
        mem.w32(STEMS_DIRECT, entry)
    # fast mute trigger: the player task's buffer-level poll (stems_apply)
    if "sm_poll_entry" in s and mem.r32(STEMS_POLL) == STEMS_POLL_ACC:
        mem.w32(STEMS_POLL, s["sm_poll_entry"])
    log(f"live stems ({base:#x}, chains {live:#x})")


# Key shift: the DSP program boots from SH RAM through two pools (sole users
# in the DSP boot module 0x08C37xxx, CLAUDE.md §95). The boot copier makes a
# copy at KS_COPY (before startup boots the DSP); the copy gets ks.bin
# appended at DSP 0xC005F4C0 (+ its zeroed state), the block-puller call
# re-pointed and the two stretcher staging loads re-pointed at KS.pbuf (the
# v2 patch, hardware-proven over USB by ksh.c). The firmware's own cold DSP
# boot then loads the patched copy: no hot patch, no DSP reset. The
# original program in the image stays stock.
KS_POOL_LEN, KS_POOL_SRC = 0x08D78AC8, 0x08C37918
KS_IMG, KS_IMG_LEN = 0x0800E770, 0x0005F4C0
KS_COPY = 0x097A0000                    # the reload route's surveyed area
KS_CALL_OFF, KS_CALL_OLD = 0x8D64, 0x0027D290
KS_PADDR = (0xC001CD40, 0xC001CD98, 0xC001CE34, 0xC001CE48)
KS_POLD = (0x3356B828, 0x0308C168, 0x01D6B828, 0x0188C168)
KS_NOP_SITE = 0x08D36B3A                # tempo-copy store -> mailbox BD8
KS_STATE = 0x260                        # zeroed after the code (as ksh.c)


def ks_parts(engine="keyshift"):
    """(DSP code, its pbuf address) for the key-shift or TOUCH FX engine,
    checked against the hardware-proven ksh installer that carries it"""
    d, code, pb = (("build/keyshift", "ks", "KS") if engine == "keyshift"
                   else ("build/fx", "fx", "FX"))
    ks = _blob(f"{d}/{code}.bin")
    pbuf = _syms(f"{d}/{code}.syms")[pb]
    hs = _syms(f"{d}/ksh.syms")
    hb = _blob(f"{d}/ksh.bin")
    o = hs["ks_dsp"] - hs["ksh_entry"]
    assert hb[o:o + len(ks)] == ks, "ks.bin differs from the hardware-proven ksh build"
    return ks, pbuf


def ks_newcall():
    disp = ((KS_IMG_LEN - (KS_CALL_OFF & ~31)) >> 2) & 0x1FFFFF
    return (KS_CALL_OLD & ~(0x1FFFFF << 7)) | disp << 7


def bake_keyshift(mem, log, engine="keyshift"):
    ks, pbuf = ks_parts(engine)
    new_len = KS_IMG_LEN + ((len(ks) + 7) & ~7) + KS_STATE
    assert new_len <= 0x60000                     # 0xC0060000 = runtime cos table
    assert mem.r32(KS_POOL_LEN) == KS_IMG_LEN
    assert mem.r32(KS_POOL_SRC) == KS_IMG | 0xA0000000
    assert mem.r32(KS_IMG + KS_CALL_OFF) == KS_CALL_OLD
    assert mem.r16(KS_NOP_SITE) == 0x1C15
    mem.region(KS_COPY, KS_COPY + KS_IMG_LEN, "key shift: DSP program copy", src=KS_IMG)
    mem.region(KS_COPY + KS_IMG_LEN, KS_COPY + new_len, "key shift: DSP code + state")
    mem.write(KS_COPY + KS_IMG_LEN, ks)
    mem.w32(KS_COPY + KS_CALL_OFF, ks_newcall())
    for i, (a, old) in enumerate(zip(KS_PADDR, KS_POLD)):
        assert mem.r32(KS_COPY + a - 0xC0000000) == old, hex(a)
        c = (pbuf >> 16) if i & 1 else (pbuf & 0xFFFF)
        mem.w32(KS_COPY + a - 0xC0000000, (old & ~(0xFFFF << 7)) | c << 7)
    mem.w32(KS_POOL_SRC, KS_COPY | 0xA0000000)
    mem.w32(KS_POOL_LEN, new_len)
    mem.w16(KS_NOP_SITE, 0x0009)
    log(f"{'live key shift' if engine == 'keyshift' else 'TOUCH FX engine'} "
        f"(DSP program copy at {KS_COPY:#x}, {new_len:#x} B, "
        f"loaded by the deck's own DSP boot)")


TFX_TOUCH = 0x08D5278C                  # touch hook pool (pads chain it too)


def bake_touchfx(mem, log):
    """TOUCH FX: the DSP effects engine (same slot as key shift) + the
    full-area PERFORM panel (tools/touchfx), chained in front of the
    flush and touch hooks like touchfx_apply.py does over USB."""
    import perform_pads as PP
    if mem.r32(PP.PC0) == 0:
        raise SystemExit("REFUSED: TOUCH FX needs the PERFORM pads")
    bake_keyshift(mem, log, engine="touchfx")
    s = _syms("build/touchfx/tfx.syms")
    img = _blob("build/touchfx/tfx.bin")
    base = s["tfx_flush_entry"]
    mem.region(base, (s["end"] + 3) & ~3, "TOUCH FX panel")
    mem.write(base, img)
    lit = PP.pbo_lit()                 # behind the pads: draws after them
    lf, lt = mem.r32(lit), mem.r32(TFX_TOUCH)
    assert lf == PP.ORIG_BLIT, hex(lf)
    mem.w32(s["tfx_prev_flush"], lf)
    mem.w32(s["tfx_prev_touch"], lt)
    mem.w32(s["tfx_engine"], 1)
    mem.w32(TFX_TOUCH, s["tfx_touch_entry"])
    mem.w32(lit, base)
    log(f"TOUCH FX panel ({base:#x}, chains {lf:#x} / {lt:#x})")


def bake_themes(mem, log, names=None, start=None):
    """On-deck theme switching (tools/theme_switch.py): code + tables at
    their RAM home (UTILITY > THEME). `start` = a theme name: its colours
    are pre-applied to the image's artwork and the switcher is told so, so
    the deck boots in that theme and THEME > OFF restores stock exactly.
    Too big for a flashed update (76 KB): emulator images only."""
    import numpy as np
    import theme_switch as TS
    import mysettings as MS
    names = list(names or TS.default_names())
    code, _ = TS.build_code()
    w = np.frombuffer(bytes(mem.b[:len(mem.b) // 2 * 2]), "<u2")
    data = bytearray(TS.blob(names, w))
    end = (TS.TS_DATA + len(data) + 3) & ~3
    assert TS.TS_BASE + len(code) <= TS.TS_DATA and end <= TS.TS_END
    mem.region(TS.TS_BASE, end, "theme switching")
    if start in names:
        t = names.index(start) + 1
        regs, S, Ds, _ = TS.tables(names, w)
        new = TS.model_switch(w, regs, S, Ds[t - 1])
        diff = np.nonzero(new != w)[0]
        for i in diff:
            mem.w16(BASE + 2 * int(i), int(new[i]))
        data[32] = data[33] = t                      # header cur / target
        log(f"theme {start} pre-applied ({len(diff)} px)")
    mem.write(TS.TS_DATA, bytes(data))
    mem.write(TS.TS_BASE, code)
    nm = (start.upper().encode()[:15] if start in names else b"OFF")
    for i, b in enumerate(nm.ljust(16, b"\0")):
        mem.w8(MS.MSTNAME + i, b)
    log(f"theme switching ({len(names)} themes, {TS.TS_BASE:#x})")


# UTILITY rows (tools/utility, CLAUDE.md sec. 127): the stock UTILITY table
# is .data -- image 0x08E634B4 (16 x 52 B), copied by startup to RAM
# 0x0E736410, count word image 0x08E637F4 -> RAM 0x0E736750. The module
# (boot-copied to its RAM home) carries a copy of the 16 image records +
# our 5; the four table pools (code) point at it and the image's count
# word says 21, so the .data copy brings the right count up with the deck.
UT_IMG_TAB, UT_IMG_COUNT = 0x08E634B4, 0x08E637F4


def bake_utility(mem, log):
    import utility as U
    assert U.STOCK_TAB - UT_IMG_TAB == U.COUNT - UT_IMG_COUNT == 0x058D2F5C
    stock = bytes(mem.r8(UT_IMG_TAB + i) for i in range(U.STOCK_N * U.REC))
    assert struct.unpack_from("<I", stock)[0] == 0xA80D486C, "UTILITY table moved"
    assert mem.r32(UT_IMG_COUNT) == U.STOCK_N
    base, img, s = U.image(stock)
    mem.region(base, (s["end"] + 3) & ~3, "UTILITY rows")
    mem.write(base, img)
    for p in U.POOLS:
        assert mem.r32(p) == U.STOCK_TAB, hex(p)
        mem.w32(p, s["ut_tab"])
    mem.w32(UT_IMG_COUNT, U.STOCK_N + U.NOURS)
    log(f"UTILITY rows ({base:#x}; table pools + count 16 -> {U.STOCK_N + U.NOURS} in the image)")


# USB-stick loader (tools/usbloader, docs/USBLOADER.md): the ONLY change a
# loader-only update makes. At every stick mount the deck's own
# MYSETTING.DAT reader opens files through pool 0x08DE39A4 (users only
# 0x08DE38BA / 0x08DE3904); ld_open reads XDJMODS/PACK.BIN first (a
# recorded cable install, tools/stick_pack.py) and replays it, then opens
# what the deck asked for. No pack on the stick = a stock deck.
LD_POOL, LD_STOCK_OPEN = 0x08DE39A4, 0x08DDF53C


def bake_stickloader(mem, log):
    # dev tree: never flash a loader older than its source (the app bundle
    # ships a fresh build and no source, so this check is skipped there)
    here = os.path.dirname(os.path.abspath(__file__))
    srcs = [os.path.join(here, "usbloader", n) for n in ("lc.c", "ldentry.S")]
    bin_ = _path("build/usbloader/ld.bin")
    if all(os.path.exists(p) for p in srcs + [bin_]):
        assert max(map(os.path.getmtime, srcs)) <= os.path.getmtime(bin_), \
            "build/usbloader/ld.bin is older than its source: run tools/usbloader/build.sh"
    s = _syms("build/usbloader/ld.syms")
    img = _blob("build/usbloader/ld.bin")
    base = s["ld_head"]
    assert base == 0x09890000, "ld.bin built for another LD_BASE"
    assert img[:4] == b"LDR2", "not the stick loader"
    assert mem.r32(LD_POOL) == LD_STOCK_OPEN, "open pool not stock"
    mem.region(base, (s["end"] + 3) & ~3, "USB-stick mods loader")
    mem.write(base, img)
    mem.w32(LD_POOL, s["ld_open"])
    log(f"USB-stick mods loader ({base:#x}; opens XDJMODS/PACK.BIN at every stick mount)")


BAKERS = {"centre": bake_centre, "phasemeter": bake_phasemeter,
          "hotcue8": bake_hotcue8, "stems": bake_stems,
          "keyshift": bake_keyshift, "touchfx": bake_touchfx,
          "utility": bake_utility}


COPIER_SRC = """
copier:                     ; jobs: {{dst, src (0 = zero-fill), words}}, dst 0 ends
  mov.l @({table:#x},pc),r1
next:
  mov.l @r1+,r2
  tst r2,r2
  bt done
  mov.l @r1+,r3
  mov.l @r1+,r4
  tst r3,r3
  bt zero
copy:
  mov.l @r3+,r0
  mov.l r0,@r2
  add #4,r2
  dt r4
  bf copy
  bra next
  nop
zero:
  mov #0,r0
zl:
  mov.l r0,@r2
  add #4,r2
  dt r4
  bf zl
  bra next
  nop
done:
  rts                       ; the hook we replace is a bare rts
  nop
.pool
"""


def boot_tail(mem, img_len):
    """-> (tail bytes appended to the decoded image, tail address, jobs).
    Tail = boot copier + job table + payloads. Startup (0x08000800) calls
    the EMPTY init hook 0x08CB8714 (rts; nop) through pool 0x080009E0 with
    the caches off (CCR 0x808 just before, 0x105 just after), right after
    the .data copy and before the DSP boot (0x080008EA) and the RTOS. The
    pool points at the copier instead: every region is zero-filled (or
    filled from the firmware bytes it copies), then the collected bytes are
    copied in, and it returns."""
    from shasm import assemble
    tail = BASE + ((img_len + TAIL_ALIGN - 1) & ~(TAIL_ALIGN - 1))
    jobs = [(s, src or 0, (e - s) // 4) for s, e, src, _ in sorted(mem.regions)]
    runs = []
    for s, e, src, _ in sorted(mem.regions):
        diff = sorted(a for a in mem.ram if s <= a < e and mem.ram[a] != mem.baseline(a))
        run = None
        for a in diff:
            if run and a - run[1] <= 32:
                run[1] = a + 1
                continue
            if run:
                runs.append(run)
            run = [a, a + 1]
        if run:
            runs.append(run)
    data = [(a & ~3, bytes(mem.r8(x) for x in range(a & ~3, (b + 3) & ~3)))
            for a, b in runs]
    code = assemble(COPIER_SRC.format(table=0x12345678))
    table = tail + ((len(code) + 15) & ~15)
    code = assemble(COPIER_SRC.format(table=table))
    blobs = bytearray()
    at = table + 12 * (len(jobs) + len(data)) + 4
    for dst, b in data:
        jobs.append((dst, at + len(blobs), len(b) // 4))
        blobs += b
    out = bytearray(code.ljust(table - tail, b"\0"))
    for j in jobs:
        out += struct.pack("<III", *j)
    out += bytes(4) + blobs
    return bytes(out), tail, jobs


SPLASH_OFF, SPLASH_A, SPLASH_B = 0x08102E30, 0x000DC650, 0x000F9B10


def apply_mods(mem, mode="rgb", versions=True, pads=True, log=print,
               extras=()):
    import rgb_overview as RO
    import rgb_detail as RD
    import vredirect as VR
    import mysettings as MS
    for a, b in RO.plan(RO.MODES[mode])[1]:
        mem.w8(a, b)
    log(f"overview waveform: {mode} (+ snapshot routine)")
    if "centre" not in extras:
        # its colour module lives outside the image (boot-copied only
        # when baked as an extra): keep the stock blue centre
        log("centre waveform: stock")
    if versions:
        assert mem.r32(VR.HOOK) == VR.FREAD, "version hook not stock"
        for a, b in VR.plan():
            mem.w8(a, b)
        log("key/stem version switching")
    # 1.0: the mod settings live in the stock UTILITY list (extra
    # "utility", always baked below); no MY SETTINGS rows
    if pads:
        import perform_pads as PP
        # Only bake the tabs whose mods are in this file (the installer
        # writes the same byte over USB); 0 keeps every tab.
        # TOUCH FX takes the KEY SHIFT slot (DSP_EXCLUSIVE), so that slot
        # must NOT be marked hidden when touchfx is baked -- otherwise the
        # pads believe the slot is dead while the Touch FX tab still draws
        # and still takes taps (emu gate: "hidden tabs do nothing").
        bits = PP.featbits("keyshift" not in extras and "touchfx" not in extras,
                           "stems" not in extras)
        PP.install(mem, bits)
        log("PERFORM pads (BEAT JUMP"
            + (" / KEY SHIFT" if "keyshift" in extras else "")
            + (" / TOUCH FX" if "touchfx" in extras else "")
            + (" / STEMS" if "stems" in extras else "") + " tabs)")
        # Boot logo: the opening frame draws picture record 0x08102E10
        # (600x100, offset 0xDC650 from 0x08100000 = 0x081DC650), whose top
        # 12 rows ARE the pads' code area -> one frame of noise in a flashed
        # / emulator image (the cable install writes the pads after boot).
        # Point that record at the logo every later frame draws (0xF9B10).
        if mem.r32(SPLASH_OFF) == SPLASH_A:
            mem.w32(SPLASH_OFF, SPLASH_B)
            log("boot logo: opening frame uses the clean logo picture")
        if "hotcue8" not in extras and isinstance(mem, SplitMem):
            # the HOT CUE re-tap reads hc_next at HC_BASE+4: power-on RAM
            # is not guaranteed zero, so give it the zero it expects
            mem.region(PP.HC_BASE, PP.HC_BASE + 16, "hot cue entry table (zero)")
    if isinstance(mem, SplitMem) and "utility" not in extras:
        extras = tuple(extras) + ("utility",)    # the only settings menu
    bad = [e for e in extras if e not in BAKERS]
    if bad:
        raise SystemExit(f"REFUSED: unknown extra(s) {bad}; known: {list(BAKERS)}")
    if extras and not isinstance(mem, SplitMem):
        raise SystemExit("REFUSED: extras need the boot copier (SplitMem)")
    if all(e in extras for e in DSP_EXCLUSIVE):
        raise SystemExit("REFUSED: key shift and TOUCH FX share the DSP slot: pick one")
    if not pads and set(extras) & {"hotcue8", "stems", "touchfx"}:
        raise SystemExit("REFUSED: 8 hot cues / live stems need the PERFORM pads")
    for e in EXTRAS:                    # fixed order: pm chains pads, hc chains pm
        if e in extras:
            BAKERS[e](mem, log)


def mods_image(dec, mode="rgb", versions=False, pads=True,
               extras=tuple(e for e in EXTRAS if e != "touchfx"), log=print,
               themes=False, theme_start=None, theme_names=None):
    """The decoded firmware image (block 1, as the deck runs it) with the
    mods baked in exactly as a permanent update would carry them -- the
    boot copier included -- for an emulator that boots the image directly
    (the QEMU engine's "With mods", emu/qemu_app.py). No compression, no
    update container, nothing written."""
    dec = bytearray(dec)
    mem = SplitMem(dec)
    apply_mods(mem, mode, versions, pads, log, extras)
    if themes:              # after the pads: their art is themed too
        bake_themes(mem, log, theme_names, theme_start)
    if mem.regions:
        tail, at, jobs = boot_tail(mem, len(dec))
        mem.w32(ENTRY_POOL, at)
        dec += bytes(at - BASE - len(dec)) + tail
    return bytes(dec)


def build(stock, out, mode="rgb", banner="1.16", versions=True,
          pads=True, log=print, extras=(), stick_loader=False):
    h = hashlib.sha256(open(stock, "rb").read()).hexdigest()
    if h != OFFICIAL:
        raise SystemExit("REFUSED: not the official XDJ-700 v1.15 update file")
    if not re.fullmatch(r"\d\.\d\d", banner) or banner == "1.15":
        raise SystemExit("REFUSED: banner must look like 1.16 and differ from 1.15")
    raw, secs = fwtool.parse(stock)
    main = secs[0]
    hdr, ln = max(main.blocks(), key=lambda t: t[1])
    dec = bytearray(L.decode(bytes(main.img[hdr + 4:hdr + 4 + ln])))
    stock_dec = bytes(dec)
    mem = SplitMem(dec)
    if stick_loader:
        # stock + loader only: the stick's pack was recorded on a stock deck
        # and checks stock values before it writes anything
        bake_stickloader(mem, log)
    else:
        apply_mods(mem, mode, versions, pads, log, extras)
    if mem.regions:
        if (mem.r32(ENTRY_POOL) != ENTRY_STUB or mem.r16(ENTRY_STUB) != 0x000B
                or mem.r16(ENTRY_STUB + 2) != 0x0009):
            raise SystemExit("REFUSED: startup init hook is not the stock empty function")
        tail, at, jobs = boot_tail(mem, len(dec))
        mem.w32(ENTRY_POOL, at)
        dec += bytes(at - BASE - len(dec)) + tail
        for d, s_, n in jobs[:len(mem.regions)]:
            name = next(r[3] for r in mem.regions if r[0] == d)
            log(f"  boot copier: {name}: {d:#x}..{d + 4 * n:#x} "
                f"{'zeroed' if not s_ else f'from {s_:#x}'}")
        log(f"boot copier + {len(jobs)} jobs at {at:#x} "
            f"(+{len(tail):,} B on the end of the image)")
    assert dec[VERSION_AT:VERSION_AT + 4] == b"1.15", "version record not found"
    dec[VERSION_AT:VERSION_AT + 4] = banner.encode()
    changed = sum(1 for a, b in zip(dec, stock_dec) if a != b)
    log(f"{changed:,} bytes of the firmware image changed")
    log("compressing (about 30 s) ...")
    st = L.encode(bytes(dec))
    if L.decode(st) != bytes(dec):
        raise SystemExit("REFUSED: encoder round-trip failed")
    room = len(main.img) - (hdr + 4 + 2)
    log(f"compressed block: {ln:,} -> {len(st):,} bytes ({len(st) - ln:+,}); "
        f"{room - len(st):,} B of flash slot left")
    if len(st) > room:
        raise SystemExit(f"REFUSED: does not fit the flash slot ({len(st) - room:,} B "
                         f"over) -- leave out some extras")
    main.write_block(hdr, ln, st)          # refuses if it does not fit
    old = main.pre
    main.pre = re.sub(r"(MAINVer)[0-9.]+", lambda m: m.group(1) + banner, main.pre, count=1)
    if main.pre == old:
        raise SystemExit("REFUSED: update banner not found")
    open(out, "wb").write(fwtool.assemble(secs))
    # ---- verify the written file
    _, chk = fwtool.parse(out)
    if not all(okb for *_, okb in chk[0].check_blocks()):
        raise SystemExit("REFUSED: block checksum mismatch in the written file")
    h2, l2 = max(chk[0].blocks(), key=lambda t: t[1])
    if L.decode(bytes(chk[0].img[h2 + 4:h2 + 4 + l2])) != bytes(dec):
        raise SystemExit("REFUSED: written file does not decode to the intended image")
    s_raw, s_secs = fwtool.parse(stock)
    sh, sl = min(s_secs[0].blocks(), key=lambda t: t[0])
    if bytes(chk[0].img[:sh + 4 + sl + 2]) != bytes(s_secs[0].img[:sh + 4 + sl + 2]):
        raise SystemExit("REFUSED: boot stub / block 0 changed")
    if chk[1].emit() != s_secs[1].emit():
        raise SystemExit("REFUSED: PANL section changed")
    log(f"written {out}: checksums OK, decodes exactly, boot stub + block 0 + "
        f"PANL identical to stock, banner {banner}")
    return out


EXTRA_HOMES = {"centre": 0x098A0000, "hotcue8": 0x09790000,
               "phasemeter": 0x09798000, "stems": 0x09836000,
               "keyshift": KS_COPY, "touchfx": 0x09844000, "utility": 0x09794000}
EXTRA_SYMS = {"centre": "build/centre/cw.syms", "hotcue8": "build/hotcue8/hc.syms",
              "phasemeter": "build/phasemeter/pm.syms",
              "stems": "build/stems/sm.syms", "keyshift": "build/keyshift/ks.syms",
              "touchfx": "build/touchfx/tfx.syms",
              "utility": "build/utility/ut.syms"}


def baked_jobs(dec):
    """the boot copier's job table read back out of a decoded image ->
    (jobs, {extra: syms}) -- ([], {}) for a build without the copier"""
    pool = struct.unpack_from("<I", dec, ENTRY_POOL - BASE)[0]
    if pool == ENTRY_STUB:
        return [], {}
    w = struct.unpack_from("<H", dec, pool - BASE)[0]
    assert w >> 12 == 0xD, "copier does not start with its table load"
    table = struct.unpack_from("<I", dec, ((pool + 4) & ~3) + (w & 0xFF) * 4 - BASE)[0]
    jobs, p = [], table
    while struct.unpack_from("<I", dec, p - BASE)[0]:      # dst 0 = end
        jobs.append(struct.unpack_from("<III", dec, p - BASE))
        p += 12
    homes = {d for d, s, n in jobs if s and not KS_IMG <= s < KS_IMG + KS_IMG_LEN} | \
        {d for d, s, n in jobs if s == KS_IMG}         # extras = homes that get bytes
    extras = {e: _syms(EXTRA_SYMS[e]) for e, h in EXTRA_HOMES.items() if h in homes}
    if "touchfx" in extras:             # the DSP copy then carries fx.bin
        extras.pop("keyshift", None)
    return jobs, extras


def extras_checks(m, dec, jobs, extras, log):
    """after a cold boot: every byte the copier placed is still there (code,
    chained pointers, DSP program copy), the pools point at it, and -- with
    the real DSP modelled -- the DSP runs the key-shift build it booted"""
    import perform_pads as PP
    out = []
    if not jobs:
        return out
    ram = m.mem.r_block
    want = {}                                  # copier replayed: dst -> bytes
    for d, s, n in jobs:
        if s:
            want.update(zip(range(d, d + 4 * n), dec[s - BASE:s - BASE + 4 * n]))
    bad = []
    for d, s, n in jobs:
        if not s:
            continue
        got = bytes(ram(d, 4 * n))
        diff = [i for i in range(4 * n) if got[i] != want[d + i]]
        if diff:
            bad.append(f"{d:#x}+{diff[0]:#x} ({len(diff)} B)")
    out.append((f"boot copier: {len(jobs)} jobs, every copied byte intact"
                + (f" -- CHANGED: {', '.join(bad[:6])}" if bad else ""), not bad))
    chain = [m.mem.r32(FLUSH_POOL)]
    hops = {extras.get("hotcue8", {}).get("hc_entry"): extras.get("hotcue8", {}).get("hc_prev"),
            extras.get("phasemeter", {}).get("pm_entry"): extras.get("phasemeter", {}).get("pm_prev")}
    while chain[-1] in hops and chain[-1] is not None and len(chain) < 5:
        chain.append(m.mem.r32(hops[chain[-1]]))
    log("  flush chain: " + " -> ".join(f"{x:#x}" for x in chain))
    pads_baked = struct.unpack_from("<I", dec, PP.PC0 - BASE)[0] != 0
    want_chain = ([extras["hotcue8"]["hc_entry"]] if "hotcue8" in extras else []) + \
                 ([extras["phasemeter"]["pm_entry"]] if "phasemeter" in extras else []) + \
                 ([PP.hooks()[1][2]] if pads_baked else [PP.ORIG_BLIT])
    out.append(("flush hook chain (8 hot cues / phase meter / PERFORM pads)",
                chain == want_chain))
    if "utility" in extras:
        import utility as U
        t = extras["utility"]["ut_tab"]
        out.append((f"UTILITY table pools + count {U.STOCK_N + U.NOURS}",
                    all(m.mem.r32(p) == t for p in U.POOLS)
                    and m.mem.r32(U.COUNT) == U.STOCK_N + U.NOURS))
    if "stems" in extras:
        out.append(("stems read hook", m.mem.r32(0x08D5FFA8) == extras["stems"]["sm_fread"]))
        if "sm_poll_entry" in extras["stems"]:
            out.append(("stems fast-mute poll hook",
                        m.mem.r32(STEMS_POLL) == extras["stems"]["sm_poll_entry"]))
    eng = "keyshift" if "keyshift" in extras else "touchfx" if "touchfx" in extras else None
    if "touchfx" in extras:
        out.append(("TOUCH FX touch hook + behind the pads",
                    m.mem.r32(TFX_TOUCH) == extras["touchfx"]["tfx_touch_entry"] and
                    m.mem.r32(PP.pbo_lit()) == extras["touchfx"]["tfx_flush_entry"]))
    if eng:
        out.append((f"{eng}: DSP boot reads the patched copy",
                    m.mem.r32(KS_POOL_SRC) == KS_COPY | 0xA0000000 and
                    m.mem.r16(KS_NOP_SITE) == 0x0009))
        d = getattr(m.mem.dev, "dspemu", None)
        if d is not None:
            ks, pbuf = ks_parts(eng)
            call = d.bus.read(0xC0000000 + KS_CALL_OFF, 4)
            land = bytes(d.bus.read(0xC005F4C0 + i, 1) for i in range(len(ks)))
            p0 = d.bus.read(KS_PADDR[0], 4)
            log(f"  DSP DDR: call {call:#010x} (want {ks_newcall():#010x}), "
                f"ks code {'present' if land == ks else 'MISSING'}, "
                f"staging load {p0:#010x}, dsp err={d.error}")
            out.append((f"{eng}: the real DSP booted the patched program",
                        call == ks_newcall() and land == ks and p0 != KS_POLD[0]
                        and not d.error))
    return out


def stick_loader_check(m, dec, log=print):
    """Loader-only build, booted from reset: the loader is in RAM and armed,
    and (with PACK=... set) a stick carrying that pack mounts and the pack
    is applied in full."""
    import tempfile
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    s = _syms("build/usbloader/ld.syms")
    img = _blob("build/usbloader/ld.bin")
    mem = m.mem
    ok = True
    checks = [("loader copied to RAM", bytes(mem.r8(s["ld_head"] + i)
                                            for i in range(len(img))) == img),
              ("open pool armed", mem.r32(LD_POOL) == s["ld_open"]),
              ("nothing else changed: overview hook stock",
               mem.r32(0x08DBC5C8) == 0x08C2C984)]
    for name, c in checks:
        log(("  ok   " if c else "  FAIL ") + name)
        ok &= c
    pack = os.environ.get("PACK")
    if pack:
        d = tempfile.mkdtemp(prefix="ldupd_")
        os.makedirs(os.path.join(d, "PIONEER"))
        open(os.path.join(d, "PIONEER", "MYSETTING.DAT"), "wb").write(b"\0" * 160)
        os.makedirs(os.path.join(d, "XDJMODS"))
        p = open(pack, "rb").read()
        open(os.path.join(d, "XDJMODS", "PACK.BIN"), "wb").write(p)
        u = m.mem.dev._usb()
        if u.stick is not None:
            u.unplug()
            m.cpu.run(limit=20_000_000)
        disk = open_disk(d)
        m.mem.dev.usb_disk = disk
        u.stick = MassStorage(disk)
        for _ in range(8):
            m.cpu.run(limit=250_000_000)
        st = s["ld_state"]
        status, applied = mem.r32(st + 8), mem.r32(st + 16)
        n = struct.unpack_from("<H", p, 10)[0]
        c = status == 4 and applied == n
        log(("  ok   " if c else "  FAIL ") +
            f"stick pack applied at mount: status {status} (4 = done), {applied}/{n} records")
        ok &= c
    return ok, None


def emu_check(upd, steps=6_000_000_000, log=print):
    """Boot the built file FROM RESET in the emulator (the deck's own boot
    stub decompresses + checks it), then confirm the mods are live in RAM
    with nothing installed over USB, and the main screen came up."""
    from emu import fw as F
    from emu.machine import Machine
    from emu import panl
    raw, main, panl_sec = F.load_upd(upd)
    m = Machine(F.flash_image(main))
    m.mem.dev.panel_frame = panl.build_frame()
    done = 0
    while done < steps:
        m.cpu.run(limit=500_000_000)
        done += 500_000_000
    dec = F.decode_block1(main)[2]
    if struct.unpack_from("<I", dec, LD_POOL - BASE)[0] != LD_STOCK_OPEN:
        return stick_loader_check(m, dec, log)
    import rgb_overview as RO
    import rgb_detail as RD
    import vredirect as VR
    import mysettings as MS
    ok = True
    jobs, extras = baked_jobs(dec)
    checks = [("overview hook", m.mem.r32(RO.HOOK_POOL) == RO.CAVE),
              ("audio read hook as built (versions / stems / stock)",
               m.mem.r32(VR.HOOK) == struct.unpack_from("<I", dec, VR.HOOK - BASE)[0]
               in (VR.VCAVE, VR.FREAD, extras.get("stems", {}).get("sm_fread"))),
              ("MY SETTINGS left stock", all(m.mem.r32(p) == o for p, o, n in MS.hooks()))]
    if "centre" in extras:
        checks.append(("centre colour hook", m.mem.r32(RD.POOL) == RD.DCAVE))
    else:
        checks.append(("centre stock", m.mem.r32(RD.POOL) == RD.PALETTE))
    checks += extras_checks(m, dec, jobs, extras, log)
    for name, c in checks:
        log(("  ok   " if c else "  FAIL ") + name + " live after boot")
        ok &= c
    if os.environ.get("SAVE"):                 # booted snapshot for further tests
        m.save(os.environ["SAVE"])
        log(f"  saved {os.environ['SAVE']}")
    sys.path.insert(0, "tools")
    from ghosttap_emu import fb_png
    shots = os.environ.get("XDJ_SHOTS", "screenshots/permanent")
    os.makedirs(shots, exist_ok=True)
    fb_png(m, os.path.join(shots, "booted.png"))
    import numpy as np
    from PIL import Image
    a = np.array(Image.open(os.path.join(shots, "booted.png")).convert("L"))
    drawn = int((a > 40).sum())
    log(f"  {'ok  ' if drawn > 20000 else 'FAIL'} main screen drawn ({drawn:,} lit px)")
    ok &= drawn > 20000
    if "utility" in extras:
        # hold MENU = UTILITY, scroll to the end: our rows get drawn
        ut = extras["utility"]
        m.mem.dev.panel_frame = panl.build_frame(keys=["MenuUtility"])
        m.cpu.run(limit=200_000_000)
        m.mem.dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=60_000_000)
        for _ in range(20):
            m.mem.w16(panl.ROTARY_COUNTER, (m.mem.r16(panl.ROTARY_COUNTER) + 1) & 0xFFFF)
            m.cpu.run(limit=8_000_000)
        m.cpu.run(limit=20_000_000)
        fb_png(m, os.path.join(shots, "utility.png"))
        drew = m.mem.r32(ut["ut_draws"]) > 0
        log(f"  {'ok  ' if drew else 'FAIL'} UTILITY shows the mod rows "
            f"({m.mem.r32(ut['ut_draws'])} draws)")
        ok &= drew
        m.mem.dev.panel_frame = panl.build_frame(keys=["MenuUtility"])   # close
        m.cpu.run(limit=12_000_000)
        m.mem.dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=60_000_000)
    # PERFORM smoke on the baked pads (no track needed): open by touch,
    # every tab, one jump-pad tap; nothing may fault
    import perform_pads as PP
    e0 = m.exc_total

    def touch(p, hold=3_000_000, after=3_000_000):
        m.mem.dev.panel_frame = panl.build_frame(touch=p)
        m.cpu.run(limit=hold)
        m.mem.dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=after)
        for _ in range(40):                       # press + release settle
            if m.mem.r8(PP.PST) == 0 and m.mem.r32(PP.ZSTATE) == 0x1E:
                break
            m.cpu.run(limit=500_000)

    touch((709, 37))
    opened = m.mem.r32(0x0BA371D4) == 7
    log(f"  {'ok  ' if opened else 'FAIL'} PERFORM opens by touch")
    ok &= opened
    pads_baked = struct.unpack_from("<I", dec, PP.PC0 - BASE)[0] != 0
    if not pads_baked:
        # no PERFORM pads in this build: the stock PERFORM screen must open
        # and stay stock (no fault) -- there are no tabs to switch
        touch((709, 37))
        stock = m.exc_total == e0 and m.mem.r8(PP.PST + 2) == 0
        log(f"  {'ok  ' if stock else 'FAIL'} stock PERFORM, no exceptions "
            f"({m.exc_total - e0} new)")
        ok &= stock
        return ok, m
    featb = m.mem.r8(PP.FEATB)       # PP.FEATB (PST+5 = the drawn-state word)
    visible = [k for k in range(4)
               if k < 2 or not (featb & (1 << (k - 2)))]
    for k in visible:
        touch((PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[k] + PP.TAB_H // 2))
        log(f"      tab{k}: mode={m.mem.r8(PP.PST + 2)} latch={m.mem.r8(PP.PST)} "
            f"zone={m.mem.r32(PP.ZSTATE):#x}")
    tabs = m.mem.r8(PP.PST + 2) == visible[-1]
    log(f"  {'ok  ' if tabs else 'FAIL'} chosen tabs switch "
        f"(FEATB {featb:#x}: {' '.join(map(str, visible))})")
    ok &= tabs
    hidden = [k for k in range(4) if k not in visible]
    for k in hidden:
        touch((PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[k] + PP.TAB_H // 2))
    quiet = m.mem.r8(PP.PST + 2) == visible[-1]
    if hidden:
        log(f"  {'ok  ' if quiet else 'FAIL'} hidden tabs do nothing")
    ok &= quiet
    touch((200, 263))
    clean = m.exc_total == e0
    log(f"  {'ok  ' if clean else 'FAIL'} pad tap, no exceptions "
        f"({m.exc_total - e0} new)")
    ok &= clean
    return ok, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--mode", default="rgb", choices=["rgb", "3band", "blue"])
    ap.add_argument("--banner", default="1.16")
    ap.add_argument("--no-pads", action="store_true")
    ap.add_argument("--no-versions", action="store_true")
    ap.add_argument("--extras", default="",
                    help="comma list of mods copied to RAM at boot: "
                         + ",".join(EXTRAS) + " (or 'all')")
    ap.add_argument("--emu", action="store_true", help="boot-test a built file")
    ap.add_argument("--stick-loader", action="store_true",
                    help="stock firmware + the USB-stick mods loader only "
                         "(mods then come from XDJMODS/PACK.BIN on the stick)")
    ap.add_argument("--i-know-flashing-can-brick", action="store_true",
                    help="REQUIRED to write a file: flashing the wrong file, "
                         "or losing power mid-flash, can leave the deck "
                         "needing a service centre")
    a = ap.parse_args()
    if a.emu:
        ok, _ = emu_check(a.files[0])
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    if not a.i_know_flashing_can_brick:
        ap.error("refusing to write: re-run with --i-know-flashing-can-brick "
                 "after reading the warnings at the top of this tool")
    extras = EXTRAS if a.extras == "all" else tuple(x for x in a.extras.split(",") if x)
    build(a.files[0], a.files[1], a.mode, a.banner,
          not a.no_versions, not a.no_pads, extras=extras,
          stick_loader=a.stick_loader)
    print("RESULT: built. Boot-tested in the emulator only -- this build has not "
          "been flashed by this project (a tester flashed a v1.16 build of this "
          "family on a real deck). Boot-test: permanent_build.py --emu", a.files[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
