#!/usr/bin/env python3
"""usbloader_test.py -- loader core (build/usbloader/lc.bin) emulator proof.

docs/USBLOADER.md milestone 2, first half: the CORE (pack parse without
the FAT part -- the pack is fed from emu memory) must replay a pack into
its RAM homes BYTE EXACT, and fail closed on anything it cannot verify.

Pack format: REV 2 only (tools/stick_pack.py -- a recorded cable install,
24 B entries, two-phase apply). This test built rev-1 packs
(pack_builder.py) until 2026-11-08 and had been failing at the header ever
since the loader moved to rev 2: the gate was asserting a format the
loader no longer speaks, on arm bits that no longer exist. lc_entry now
returns the number of APPLIED entries, and lc_why / lc_bad / lc_seen say
what happened.

Direct-call harness per stems_emu_test.py / pv_core_test.py conventions:
set r4/r5, park pr at HALT, PC = entry, halt on reaching the park.

Variants:
  A good pack (pm + stems + hotcue8 caves) -> 3 applied, homes byte-exact
  B one body corrupted -> replay stops AT it (0 applied, lc_bad = 1)
  C bad magic -> nothing, and lc_seen stays 0 (never even a header)
  D home outside the address window -> refused in phase 1 (nothing written)
  E a run whose "old" bytes do not match RAM -> refused in phase 1 (W2_OLD),
    i.e. a live pool is never decoded over
  F empty pack (0 entries) -> W2_COUNT
"""
import os
os.environ.setdefault("XDJ_HLE_DSP", "1")   # before emu.machine import
import re
import sys
sys.path[:0] = [".", "tools"]
from emu.machine import Machine
import stick_pack as SP                     # tools/stick_pack.py (rev 2)

LC = "build/usbloader/lc.bin"
BASE = 0x0BC00000                           # loader core (bss below it)
PACK = 0x0BC20000                           # pack image in emu DDR
HALT = 0x0F300000                           # parked return address
HOMES = {"pm": (0x09798000, "build/phasemeter/pm.bin"),
         "stems": (0x09836000, "build/stems/sm.bin"),
         "hotcue8": (0x09790000, "build/hotcue8/hc.bin")}


def w_codes():
    """lc.c's own W_* enum, so a renumbering there cannot make this test
    assert the wrong reason."""
    src = open("tools/usbloader/lc.c").read()
    out = {}
    for blk in re.findall(r"enum\s*\{([^}]*)\}", src):
        if "W_OK" not in blk and "W2_" not in blk:
            continue
        nxt = 0
        for item in blk.split(","):
            name, _, val = item.strip().partition("=")
            name = name.strip()
            if not name or not name.startswith("W"):
                continue
            if val.strip():
                nxt = int(val.strip(), 0)
            out[name] = nxt
            nxt += 1
    assert "W2_COUNT" in out, "W_* enums not found in lc.c"
    return out


def syms():
    s = {}
    for line in open("build/usbloader/lc.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def wr(mem, addr, data):
    for i, b in enumerate(data):
        mem.w8(addr + i, b)


def rd(mem, addr, n):
    return bytes(mem.r8(addr + i) for i in range(n))


class PcHalt(Exception):
    pass


def main():
    W = w_codes()
    s = syms()
    lc = open(LC, "rb").read()
    files = {n: open(f, "rb").read() for n, (_h, f) in HOMES.items()}
    good = SP.build([(home, bytes(len(files[n])), files[n])
                     for n, (home, _f) in HOMES.items()])

    m = Machine.load("cache/emu_track_loaded.pkl")
    mem, c = m.mem, m.cpu

    def run_variant(pack, prep=None):
        """loader + bss + pack + clean homes, every run, from a known state.
        Returns (applied, why, bad, seen)."""
        for a in range(BASE, PACK, 4):
            mem.w32(a, 0)
        wr(mem, BASE, lc)
        wr(mem, PACK, b"\x00" * min(0x20000, max(len(pack) * 2, 0x400)))
        wr(mem, PACK, pack)
        for name, (home, _f) in HOMES.items():     # homes clean each run
            for i in range(0, max(len(files[name]), 0x2200), 4):
                mem.w32(home + i, 0)
        if prep:
            prep(mem)
        c.set_gr(4, PACK)
        c.set_gr(5, len(pack))
        c.pr = HALT
        c.pc = BASE
        try:
            c.run(limit=6_000_000)
        except PcHalt:
            pass
        else:
            print("  ! never returned (no park reached)")
            return None, None, None, None
        return (c.gr(0) & 0xFFFFFFFF, mem.r32(s["lc_why"]),
                mem.r32(s["lc_bad"]), mem.r32(s["lc_seen"]))

    def tr(pc, op):
        if pc == HALT:
            raise PcHalt(pc)
    c.trace = tr

    ok = True

    def chk(label, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + label)
        ok &= bool(cond)

    print("A) good pack, 3 cave payloads")
    applied, why, bad, seen = run_variant(good)
    chk(f"applied {applied} == 3, why {why} (W_OK={W['W_OK']}), bad {bad}",
        applied == 3 and why == W["W_OK"] and bad == 0 and seen == 1)
    for name, (home, _f) in HOMES.items():
        same = rd(mem, home, len(files[name])) == files[name]
        chk(f"{name:8s} {len(files[name]):,} bytes byte-exact: {same}", same)

    print("B) one body corrupted -> stop AT it")
    bad_pack = bytearray(good)
    first_body = 0x10 + 3 * 24                  # three 24 B entries, then bodies
    first_body += (-first_body) % 0x10          # bodies are FILE-aligned
    bad_pack[first_body + 5] ^= 0xFF            # inside pm's compressed body
    applied, why, bad, _seen = run_variant(bytes(bad_pack))
    chk(f"applied {applied} == 0, lc_bad {bad} == 1, why {why} in "
        f"(W_SHORT={W['W_SHORT']}, W_CRC={W['W_CRC']})",
        applied == 0 and bad == 1 and why in (W["W_SHORT"], W["W_CRC"]))
    untouched = not any(rd(mem, HOMES["stems"][0], 0x40))
    chk(f"nothing after the bad body (stems home still empty): {untouched}",
        untouched)

    print("C) bad magic -> nothing, and no header seen")
    broken = bytearray(good)
    broken[0:4] = b"XXXX"
    applied, why, bad, seen = run_variant(bytes(broken))
    chk(f"applied {applied} == 0, lc_seen {seen} == 0", applied == 0 and seen == 0)

    print("D) home outside 0x08000000..0x10000000 -> refused in phase 1")
    rogue = SP.build([(0x07000000, bytes(64), b"\x5a" * 64)])
    applied, why, bad, _seen = run_variant(rogue)
    chk(f"applied {applied} == 0, why {why} == W2_WINDOW {W['W2_WINDOW']}, "
        f"bad {bad} == 1", applied == 0 and why == W["W2_WINDOW"] and bad == 1)
    chk("rogue area untouched", not any(rd(mem, 0x07000000, 0x40)))

    print("E) a run's 'old' bytes do not match RAM -> refused, nothing written")
    # old != zeros -> stick_pack marks it F_OLD with the CRC of those bytes;
    # the emulated RAM holds zeros, so phase 1 must reject the whole pack.
    live = SP.build([(HOMES["stems"][0], b"\x11" * 64, b"\x22" * 64),
                     (HOMES["pm"][0], bytes(len(files["pm"])), files["pm"])])
    applied, why, bad, _seen = run_variant(live)
    chk(f"applied {applied} == 0, why {why} == W2_OLD {W['W2_OLD']}, bad {bad}",
        applied == 0 and why == W["W2_OLD"])
    chk("the good pm run after it was NOT applied either",
        not any(rd(mem, HOMES["pm"][0], 0x40)))

    print("F) empty pack (0 entries) -> W2_COUNT")
    empty = SP.MAGIC + b"\x02\x00" + b"\x00\x00" + b"\x00" * 4
    applied, why, bad, _seen = run_variant(empty)
    chk(f"applied {applied} == 0, why {why} == W2_COUNT {W['W2_COUNT']}",
        applied == 0 and why == W["W2_COUNT"])

    c.trace = None
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
