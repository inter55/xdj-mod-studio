#!/usr/bin/env python3
"""usbloader_test.py -- loader core (build/usbloader/lc.bin) emulator proof.

docs/USBLOADER.md milestone 2, first half: the CORE (pack parse without
the FAT part -- the pack is fed from emu memory) must install payloads
BYTE EXACT into their RAM homes and arm exactly the payloads whose CRC
holds, and fail closed on corruption.

Direct-call harness per stems_emu_test.py / pv_core_test.py conventions:
set r4/r5, park pr at HALT, PC = entry, halt on reaching the park, read
r0 = armed mask. Snapshot cache/emu_track_loaded.pkl supplies memory.

Variants:
  A good pack (pm + stems + hotcue8): arm mask 0b0111, homes byte-exact
    vs the Python round trip (pack_builder.parse + lzss_boot.decode);
  B pm's entry CRC corrupted: pm NOT armed (mask 0b0110), other homes
    still byte-exact;
  C bad magic: arm 0, no payload written.
"""
import os
os.environ.setdefault("XDJ_HLE_DSP", "1")   # before emu.machine import
import sys
sys.path[:0] = [".", "tools"]
from emu.machine import Machine
import pack_builder as P                    # tools/pack_builder.py

LC = "build/usbloader/lc.bin"
BASE = 0x0BC00000                           # loader core (bss below it)
PACK = 0x0BC20000                           # pack image in emu DDR
HALT = 0x0F300000                           # parked return address
HOMES = {"pm": (0x09798000, "build/phasemeter/pm.bin"),
         "stems": (0x09836000, "build/stems/sm.bin"),
         "hotcue8": (0x09790000, "build/hotcue8/hc.bin")}
FLASH_SNAP = 0x0BC0E000                     # end of the loader's footprint


def wr(mem, addr, data):
    for i, b in enumerate(data):
        mem.w8(addr + i, b)


def rd(mem, addr, n):
    return bytes(mem.r8(addr + i) for i in range(n))


class PcHalt(Exception):
    pass


def main():
    lc = open(LC, "rb").read()
    files = {n: open(f, "rb").read() for n, (h, f) in HOMES.items()}
    good, payload = make_pack([n for n in HOMES])
    expect = {}
    ents, errs = P.parse(good)
    assert not errs, errs
    for e in ents:
        raw = P.L.decode(e["body"])
        assert len(raw) == e["declen"]
        expect[e["name"]] = raw

    m = Machine.load("cache/emu_track_loaded.pkl")
    mem, c = m.mem, m.cpu

    def tr(pc, op):
        if pc == HALT:
            raise PcHalt(pc)
    c.trace = tr

    def run_variant(pack, packlen_override=None, prep=None):
        # loader + bss + pack + homes, every run, from a known-clean state
        for a in range(BASE, PACK, 4):
            mem.w32(a, 0)
        wr(mem, BASE, lc)
        plen = packlen_override if packlen_override is not None else len(pack)
        wr(mem, PACK, b"\x00" * min(0x20000, max(plen * 2, 0x400)))
        wr(mem, PACK, pack)
        for name, (home, _f) in HOMES.items():     # homes clean each run
            for i in range(0, max(len(files[name]), 0x2200), 4):
                mem.w32(home + i, 0)
        if prep:
            prep(mem)
        c.set_gr(4, PACK)
        c.set_gr(5, plen)
        c.pr = HALT
        c.pc = BASE
        try:
            c.run(limit=6_000_000)
        except PcHalt:
            pass
        else:
            print("  ! never returned (no park reached)")
            return None
        return c.gr(0) & 0xFFFFFFFF

    ok = True

    print("A) good pack, 3 payloads")
    mask = run_variant(good)
    want = 0
    for i, e in enumerate(ents):
        want |= 1 << i
    print(f"   mask {mask:#06b} (want {want:#06b})")
    ok &= mask == want
    for name, (home, _f) in HOMES.items():
        got = rd(mem, home, len(expect[name]))
        same = got == expect[name]
        print(f"   {name:<8} {len(got):>6,} bytes byte-exact: {same}")
        ok &= same

    # pm is the first payload: its body lands at the first 16-aligned
    # offset after the entry table (0x10 + 3*28 = 100, aligned -> 112).
    # The pack CRC covers ONLY the table, so a body flip must skip pm
    # alone, not the whole pack.
    bad = bytearray(good)
    bad[0x70 + 5] ^= 0xFF                    # inside pm's body
    print("B) pm body corrupted -> pm must NOT arm, others must")
    mask = run_variant(bytes(bad))
    pm_i = [i for i, e in enumerate(ents) if e["name"] == "pm"][0]
    expect_b = want & ~(1 << pm_i)
    print(f"   mask {mask:#06b} (want {expect_b:#06b})")
    ok &= mask == expect_b
    for name, (home, _f) in HOMES.items():
        got = rd(mem, home, len(expect[name]))
        armed_here = bool((1 << pm_i) & mask)
        if armed_here:
            same = got == expect[name]
            print(f"   {name:<8} armed and byte-exact: {same}")
            ok &= same
        elif name == "pm":
            print(f"   {name:<8} not armed (content may be decoded junk; "
                  f"unused because never armed)")
        else:
            same = got == expect[name]
            print(f"   {name:<8} still armed -> must be exact: {same}")
            ok &= same

    print("C) bad magic -> arm 0, nothing written")
    bad2 = bytearray(good)
    bad2[0:4] = b"XXXX"
    mask = run_variant(bytes(bad2))
    print(f"   mask {mask:#06b} (want 0)")
    ok &= mask == 0
    leftover = any(any(rd(mem, HOMES[n][0], len(expect[n])))
                   for n in HOMES)
    print(f"   homes untouched: {not leftover}")
    ok &= not leftover

    print("D) home outside the surveyed bands -> skipped, nothing written")
    rogue = 0x0BC30000                       # emu DDR, NOT a deck band
    for i in range(0, 0x2000, 4):
        mem.w32(rogue + i, 0)
    pk = P.build([{"name": "rogue", "ram": rogue, "arm": 0,
                   "data": files["pm"]}] + make_pack(["stems"])[1])
    mask = run_variant(pk)
    hit = any(rd(mem, rogue, 0x2000))
    print(f"   mask {mask:#06b} (want 0b0010), rogue area written: {hit}")
    ok &= mask == 0b10 and not hit

    print("E) live (non-zero) home -> skipped, never decoded over")
    def pre(mem_):
        mem_.w32(HOMES["pm"][0] + 0x40, 0xDEADBEEF)
    mask = run_variant(good, prep=pre)
    kept = mem.r32(HOMES["pm"][0] + 0x40) == 0xDEADBEEF and \
        not any(rd(mem, HOMES["pm"][0], 0x40))
    print(f"   mask {mask:#06b} (want 0b0110), live home untouched: {kept}")
    ok &= mask == 0b0110 and kept

    print("F) 33 entries (mask is 32 bits) -> whole pack refused")
    many = P.build([{"name": f"s{i}", "ram": 0x09836000 + i * 0x100,
                     "arm": 0, "data": b"\x01" * 16} for i in range(33)])
    mask = run_variant(many)
    print(f"   mask {mask:#06b} (want 0)")
    ok &= mask == 0

    print("G) declen shorter than the body -> stop AT the cap, no overrun")
    import struct as _st
    short = bytearray(make_pack(["pm"])[0])
    _st.pack_into("<I", short, 0x10 + 12, 0x100)     # declen 7476 -> 256
    tc = __import__("binascii").crc32(bytes(short[0x10:0x10 + 28])) & 0xFFFFFFFF
    _st.pack_into("<I", short, 0xC, tc)
    mask = run_variant(bytes(short))
    past = any(rd(mem, HOMES["pm"][0] + 0x100, 0x40))
    print(f"   mask {mask:#06b} (want 0), bytes past the cap: {past}")
    ok &= mask == 0 and not past

    c.trace = None
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def make_pack(names):
    payloads = []
    for n in names:
        home, f = HOMES[n]
        payloads.append({"name": n, "ram": hex(home), "arm": 0,
                         "data": open(f, "rb").read()})
    return P.build(payloads), payloads


if __name__ == "__main__":
    sys.exit(main())
