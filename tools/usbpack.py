#!/usr/bin/env python3
"""usbpack.py -- build PIONEER/MODS/PACK.BIN, the mods pack the stage-2
installer (tools/usbloader/s2.c) streams into deck RAM from the stick.

The pack is made by running the SAME installers the UPD baker runs
(permanent_build.apply_mods: overview, versions, PERFORM pads, centre,
phase meter, 8 hot cues, live stems, UTILITY rows) plus theme switching,
against a recording memory over the decoded stock firmware image. Every
byte they write is classified:
  * a write to a known arming site (pool / hook / entry / instruction
    patch word) -> an ARM record: the word to write + the value the deck
    must hold there first (stock, or the earlier arm of a chain);
  * anything else -> DATA, coalesced into runs, in write order.
Records: first every RAM home as ZERO|SURVEY (must read zero on the deck,
as the cable installers check), then the data runs, lit button pictures
as EXPSUM data (written only while they read exactly stock), and the arms
LAST, in the cable installers' order. A write anywhere else is refused.

Format (s2.c): header 32 B {"XPK1", 1, nrec, bytes, sum, 0,0,0}; records
{u32 dst, u32 len, u32 flags, u32 expect} + data (none for ZERO).

  .venv/bin/python tools/usbpack.py [--mode rgb] [--themes a b ..]
                   [--no-extras] [--out build/usbloader/PACK.BIN]
"""
import argparse
import os
import struct
import sys

sys.path[:0] = [".", "tools", "mods"]
import permanent_build as PB            # noqa: E402

F_ZERO, F_SURVEY, F_EXPSUM, F_ARM = 1, 2, 4, 8
MAGIC = 0x314B5058                      # "XPK1"
EXTRAS = ("centre", "phasemeter", "hotcue8", "stems")   # keyshift: hot patch, not yet


def arm_sites():
    import rgb_overview as RO
    import rgb_detail as RD
    import vredirect as VR
    import perform_pads as PP
    import utility as U
    import theme_switch as TS
    s = {RO.HOOK_POOL, RD.POOL, RD.INSN & ~3, RD.ROWINC[0] & ~3, RD.POOL2,
         RD.INSN2 & ~3, RD.ROWINC2[0] & ~3, VR.HOOK, PP.HOOK_TOUCH, PP.HOOK_BLIT,
         U.COUNT, PB.STEMS_DIRECT, TS.TS_BASE}
    s.update(U.POOLS)
    return s


class Op:
    __slots__ = ("kind", "dst", "data", "flags", "expect", "name")

    def __init__(self, kind, dst, data=b"", flags=0, expect=0, name=""):
        self.kind, self.dst, self.data = kind, dst, bytearray(data)
        self.flags, self.expect, self.name = flags, expect, name


class RecMem(PB.SplitMem):
    """SplitMem that logs every write as DATA runs or ARM ops."""
    def __init__(self, buf, sites):
        super().__init__(buf)
        self.sites = sites
        self.ops = []
        self.band = None                 # (lo, hi) in-image zero run the mods use

    def w8(self, a, v):
        w = a & ~3
        if w in self.sites:
            before = self.r32(w)
            super().w8(a, v)
            after = self.r32(w)
            last = self.ops[-1] if self.ops else None
            if last is not None and last.kind == "arm" and last.dst == w:
                last.data = bytearray(struct.pack("<I", after))
            else:
                self.ops.append(Op("arm", w, struct.pack("<I", after), F_ARM, before))
            return
        if self._reg(a) is None and not (self.band and self.band[0] <= a < self.band[1]):
            raise SystemExit(f"REFUSED: write at {a:#x} is outside every RAM home "
                             f"and not an arming site")
        super().w8(a, v)
        last = self.ops[-1] if self.ops else None
        if last is not None and last.kind == "data" and last.dst + len(last.data) == a:
            last.data.append(v & 0xFF)
        else:
            self.ops.append(Op("data", a, bytes([v & 0xFF])))

    def expsum(self, a, data, stock):
        """data written only if the live bytes still sum like `stock`"""
        assert len(data) == len(stock)
        self.ops.append(Op("data", a, data, F_EXPSUM, sum(stock) & 0xFFFFFFFF))
        for i, x in enumerate(data):
            PB.SplitMem.w8(self, a + i, x)


def stock_image():
    """decoded block 1 of the stock firmware (SH address 0x08000000 + i):
    the firmware home's blk1.bin, else decoded from an update file"""
    from emu import fw as FW, fwhome
    b1 = fwhome.path("blk1.bin")
    if os.path.exists(b1):
        return open(b1, "rb").read()
    upd = FW.find_upd()
    if not upd or not upd.upper().endswith(".UPD"):
        raise SystemExit("stock firmware not found (set up the firmware home first)")
    raw, main, panl = FW.load_upd(upd)
    return FW.decode_block1(main)[2]


def build_ops(mode="rgb", names=None, extras=EXTRAS, log=print):
    """-> (mem, records) where records = [(dst, len, flags, expect, data)]"""
    from emu import fw as FW
    import rgb_overview as RO
    import perform_pads as PP
    import utility as U
    import theme_switch as TS
    import mysettings as MS
    try:
        import shbuild
        shbuild.stage_prebuilt()
    except Exception:
        pass
    dec = bytearray(stock_image())
    mem = RecMem(dec, arm_sites())
    mem.band = (RO.CAVE, PP.PEND)
    # the cable installers' surveys: the in-image zero run the mods share and
    # the snapshot area (read-only survey there; zero-filling zero is harmless)
    surveys = [(RO.CAVE, PP.PEND, "mod band (overview, centre shim, versions, "
                "settings state, PERFORM pads)"),
               (RO.SNAP, RO.SLOT5 + RO.SLOT5_MAX, "waveform snapshot area")]
    # the UPD baker's utility step patches the IMAGE count word (meaningless
    # in a running deck): stubbed out, the RAM-side step is below
    saved = PB.BAKERS["utility"]
    PB.BAKERS["utility"] = lambda mem_, log_: None
    try:
        PB.apply_mods(mem, mode, True, True, log, tuple(e for e in extras if e != "utility"))
    finally:
        PB.BAKERS["utility"] = saved
    # PERFORM pads: the cable tool arms blit first, touch last
    it = next(i for i, o in enumerate(mem.ops) if o.kind == "arm" and o.dst == PP.HOOK_TOUCH)
    ib = next(i for i, o in enumerate(mem.ops) if o.kind == "arm" and o.dst == PP.HOOK_BLIT)
    if it < ib:
        mem.ops.insert(ib, mem.ops.pop(it))
    # UTILITY rows: RAM table copy (not the image copy the UPD baker patches)
    stock = bytes(mem.r8(PB.UT_IMG_TAB + i) for i in range(U.STOCK_N * U.REC))
    assert struct.unpack_from("<I", stock)[0] == 0xA80D486C, "UTILITY table moved"
    base, img, s = U.image(stock)
    mem.region(base, (s["end"] + 3) & ~3, "UTILITY rows")
    mem.write(base, img)
    for p in U.POOLS:
        assert mem.r32(p) == U.STOCK_TAB, hex(p)
        mem.w32(p, s["ut_tab"])
    # the count word is .data in RAM: expect the deck's live 16 there
    mem.ops.append(Op("arm", U.COUNT, struct.pack("<I", U.STOCK_N + U.NOURS), F_ARM, U.STOCK_N))
    log(f"UTILITY rows ({base:#x}; 4 table pools + RAM count 16 -> 21)")
    # theme switching: code (entry word = arm), tables, lit pictures, row text
    names = names or TS.default_names()
    (cb, code), (db, data) = TS.plan(names)
    mem.region(cb, db + len(data), "theme switcher + tables")
    mem.write(cb + 4, code[4:])
    mem.write(db, data)
    for a, st, pv in TS.lit_writes(names):
        mem.expsum(a, pv, st)
    mem.write(MS.MSTNAME, b"OFF\0")
    mem.w32(cb, struct.unpack("<I", code[:4])[0])
    log(f"theme switching: {len(names)} themes ({', '.join(names)}), "
        f"{len(data):,} B of tables, {len(TS.lit_writes(names))} lit pictures")
    # ---- records: surveys, data (aligned, padded from the final memory), arms
    recs = []
    for lo, hi, name in surveys:
        recs.append((lo, hi - lo, F_ZERO | F_SURVEY, 0, b""))
    for s_, e_, src, name in mem.regions:
        assert src is None, f"{name}: copy-from-firmware regions need the boot copier"
        recs.append((s_, e_ - s_, F_ZERO | F_SURVEY, 0, b""))
    for o in mem.ops:
        if o.kind != "data":
            continue
        lo, hi = o.dst & ~3, (o.dst + len(o.data) + 3) & ~3
        if o.flags & F_EXPSUM:
            # pad with stock (artwork never changes): keep the expected sum right
            pad = [mem.r8(x) for x in range(lo, o.dst)] + [mem.r8(x) for x in range(o.dst + len(o.data), hi)]
            exp = (o.expect + sum(pad)) & 0xFFFFFFFF
        else:
            exp = 0
        b = bytes(mem.r8(x) for x in range(lo, o.dst)) + bytes(o.data) + \
            bytes(mem.r8(x) for x in range(o.dst + len(o.data), hi))
        recs.append((lo, hi - lo, o.flags, exp, b))
    for o in mem.ops:
        if o.kind == "arm":
            recs.append((o.dst, 4, F_ARM, o.expect, bytes(o.data)))
    return mem, recs


def encode(recs):
    body = bytearray()
    for dst, ln, fl, exp, data in recs:
        assert dst % 4 == 0 and ln % 4 == 0 and ln, (hex(dst), ln)
        assert (fl & F_ZERO and not data) or len(data) == ln
        body += struct.pack("<IIII", dst, ln, fl, exp) + data
    hdr = struct.pack("<8I", MAGIC, 1, len(recs), len(body), sum(body) & 0xFFFFFFFF, 0, 0, 0)
    return bytes(hdr + body)


def decode(blob):
    """-> records, for tests"""
    m, v, n, ln, sm, *_ = struct.unpack_from("<8I", blob)
    assert m == MAGIC and v == 1 and sum(blob[32:]) & 0xFFFFFFFF == sm
    recs, p = [], 32
    for _ in range(n):
        dst, l, fl, exp = struct.unpack_from("<IIII", blob, p)
        p += 16
        data = b"" if fl & F_ZERO else bytes(blob[p:p + l])
        p += 0 if fl & F_ZERO else l
        recs.append((dst, l, fl, exp, data))
    assert p == len(blob)
    return recs


def summary(recs, log=print):
    z = sum(l for d, l, f, e, x in recs if f & F_ZERO)
    dat = sum(l for d, l, f, e, x in recs if not f & (F_ZERO | F_ARM))
    arms = [(d, e, struct.unpack("<I", x)[0]) for d, l, f, e, x in recs if f & F_ARM]
    log(f"{len(recs)} records: {z:,} B zero-filled (surveyed), {dat:,} B of data, "
        f"{len(arms)} arm words")
    for d, e, v in arms:
        log(f"  arm {d:#x}: {e:#x} -> {v:#x}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="rgb", choices=["rgb", "3band", "blue"])
    ap.add_argument("--themes", nargs="*", default=None)
    ap.add_argument("--no-extras", action="store_true")
    ap.add_argument("--out", default="build/usbloader/PACK.BIN")
    a = ap.parse_args()
    import theme_switch as TS
    names = TS.theme_names(a.themes) if a.themes else None
    mem, recs = build_ops(a.mode, names, () if a.no_extras else EXTRAS)
    blob = encode(recs)
    assert decode(blob) == recs
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "wb").write(blob)
    summary(recs)
    print(f"written {a.out}: {len(blob):,} B")


if __name__ == "__main__":
    main()
