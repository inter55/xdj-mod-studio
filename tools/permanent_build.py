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
EXTRAS = ("centre", "phasemeter", "hotcue8", "stems", "keyshift", "utility")
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
    mem.region(base, (s["hc_count"] + 4 + 3) & ~3, "8 hot cues")
    live = mem.r32(FLUSH_POOL)
    mem.write(base, img)
    mem.w32(s["hc_prev"], live)
    mem.w32(FLUSH_POOL, s["hc_entry"])
    log(f"8 hot cues ({base:#x}, chains {live:#x})")


STEMS_DIRECT = 0x08DDFFD4               # media API direct read slot (stems_apply)


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


def ks_parts():
    ks = _blob("build/keyshift/ks.bin")
    pbuf = _syms("build/keyshift/ks.syms")["KS"]
    hs = _syms("build/keyshift/ksh.syms")
    hb = _blob("build/keyshift/ksh.bin")
    o = hs["ks_dsp"] - hs["ksh_entry"]
    assert hb[o:o + len(ks)] == ks, "ks.bin differs from the hardware-proven ksh build"
    return ks, pbuf


def ks_newcall():
    disp = ((KS_IMG_LEN - (KS_CALL_OFF & ~31)) >> 2) & 0x1FFFFF
    return (KS_CALL_OLD & ~(0x1FFFFF << 7)) | disp << 7


def bake_keyshift(mem, log):
    ks, pbuf = ks_parts()
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
    log(f"live key shift (DSP program copy at {KS_COPY:#x}, {new_len:#x} B, "
        f"loaded by the deck's own DSP boot)")


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
    log(f"UTILITY rows ({base:#x}; table pools + count 16 -> 21 in the image)")


BAKERS = {"centre": bake_centre, "phasemeter": bake_phasemeter,
          "hotcue8": bake_hotcue8, "stems": bake_stems,
          "keyshift": bake_keyshift, "utility": bake_utility}


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


def apply_mods(mem, mode="rgb", versions=True, pads=True, log=print,
               extras=(), custom=None):
    """custom: 1-3 RGB565 colours for mode "custom" (overview table in the
    cave, centre tables in the colour module when "centre" is an extra)"""
    import rgb_overview as RO
    import rgb_detail as RD
    import vredirect as VR
    import mysettings as MS
    if mode == "custom" and not custom:
        raise SystemExit("REFUSED: --mode custom needs --colours #rrggbb[,#rrggbb[,#rrggbb]]")
    for a, b in RO.plan(RO.MODES[mode], custom if mode == "custom" else None)[1]:
        mem.w8(a, b)
    log(f"overview waveform: {mode} (+ snapshot routine)"
        + (f", colours {', '.join(f'{c:#06x}' for c in custom)}" if mode == "custom" else ""))
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
        PP.install(mem)
        log("PERFORM pads (BEAT JUMP / KEY SHIFT / STEMS tabs)")
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
    if not pads and set(extras) & {"hotcue8", "stems"}:
        raise SystemExit("REFUSED: 8 hot cues / live stems need the PERFORM pads")
    for e in EXTRAS:                    # fixed order: pm chains pads, hc chains pm
        if e in extras:
            BAKERS[e](mem, log)
            if e == "centre" and mode == "custom":
                RD.install_custom(mem, custom)
                log("centre waveform: custom colour tables")


def build(stock, out, mode="rgb", banner="1.16", versions=True,
          pads=True, log=print, extras=(), custom=None):
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
    apply_mods(mem, mode, versions, pads, log, extras, custom)
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
               "keyshift": KS_COPY, "utility": 0x09794000}
EXTRA_SYMS = {"centre": "build/centre/cw.syms", "hotcue8": "build/hotcue8/hc.syms",
              "phasemeter": "build/phasemeter/pm.syms",
              "stems": "build/stems/sm.syms", "keyshift": "build/keyshift/ks.syms",
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
        out.append(("UTILITY table pools + count 21",
                    all(m.mem.r32(p) == t for p in U.POOLS)
                    and m.mem.r32(U.COUNT) == U.STOCK_N + U.NOURS))
    if "stems" in extras:
        out.append(("stems read hook", m.mem.r32(0x08D5FFA8) == extras["stems"]["sm_fread"]))
    if "keyshift" in extras:
        out.append(("key shift: DSP boot reads the patched copy",
                    m.mem.r32(KS_POOL_SRC) == KS_COPY | 0xA0000000 and
                    m.mem.r16(KS_NOP_SITE) == 0x0009))
        d = getattr(m.mem.dev, "dspemu", None)
        if d is not None:
            ks, pbuf = ks_parts()
            call = d.bus.read(0xC0000000 + KS_CALL_OFF, 4)
            land = bytes(d.bus.read(0xC005F4C0 + i, 1) for i in range(len(ks)))
            p0 = d.bus.read(KS_PADDR[0], 4)
            log(f"  DSP DDR: call {call:#010x} (want {ks_newcall():#010x}), "
                f"ks code {'present' if land == ks else 'MISSING'}, "
                f"staging load {p0:#010x}, dsp err={d.error}")
            out.append(("key shift: the real DSP booted the key-shift program",
                        call == ks_newcall() and land == ks and p0 != KS_POLD[0]
                        and not d.error))
    return out


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
    import rgb_overview as RO
    import rgb_detail as RD
    import vredirect as VR
    import mysettings as MS
    ok = True
    dec = F.decode_block1(main)[2]
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
    for k in range(4):
        touch((PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[k] + PP.TAB_H // 2))
        log(f"      tab{k}: mode={m.mem.r8(PP.PST + 2)} latch={m.mem.r8(PP.PST)} "
            f"zone={m.mem.r32(PP.ZSTATE):#x}")
    tabs = m.mem.r8(PP.PST + 2) == 3    # tapped 0,1,2,3: ends on STEMS
    log(f"  {'ok  ' if tabs else 'FAIL'} all four tabs switch")
    ok &= tabs
    touch((200, 263))
    clean = m.exc_total == e0
    log(f"  {'ok  ' if clean else 'FAIL'} pad tap, no exceptions "
        f"({m.exc_total - e0} new)")
    ok &= clean
    return ok, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--mode", default="rgb", choices=["rgb", "3band", "blue", "custom"])
    ap.add_argument("--colours", default="",
                    help="--mode custom: 1-3 colours '#rrggbb,#rrggbb,#rrggbb'")
    ap.add_argument("--banner", default="1.16")
    ap.add_argument("--no-pads", action="store_true")
    ap.add_argument("--no-versions", action="store_true")
    ap.add_argument("--extras", default="",
                    help="comma list of mods copied to RAM at boot: "
                         + ",".join(EXTRAS) + " (or 'all')")
    ap.add_argument("--emu", action="store_true", help="boot-test a built file")
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
    import usbpack
    custom = usbpack.parse_colours(a.colours)
    build(a.files[0], a.files[1], a.mode, a.banner,
          not a.no_versions, not a.no_pads, extras=extras, custom=custom)
    print("RESULT: built (NOT flash-tested). Boot-test: permanent_build.py --emu", a.files[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
