#!/usr/bin/env python3
"""usbloader_hot.py [--check|--revert] -- put the USB-stick mods loader in
the deck's RAM over the cable. RAM only: a power-cycle is stock again.

This is the file-on-a-stick path WITHOUT flashing anything. The loader
(build/usbloader/ld.bin, 2.7 KB) sits on the deck's own mount-reader open
pool 0x08DE39A4; at every stick mount it opens <drive>:/XDJMODS/PACK.BIN,
replays the recorded install in it, then opens what the deck asked for. No
pack on the stick = a stock deck.

  (no flag)  read the whole area first (must be all zero), write the loader
             + its zeroed state area, then arm the pool (one LW, read back)
  --check    read-only: pool value, whether the loader is already there,
             and whether the area is free. Writes nothing.
  --revert   pool back to the stock open before us.

Preconditions it refuses on: the pool is not stock (something else hooked
the mount reader), or the cave area is not empty (never decode over live
code).
"""
import os
import struct
import sys

sys.path[:0] = [".", "tools"]

POOL, STOCK_OPEN = 0x08DE39A4, 0x08DDF53C
OUT = "build/usbloader"


def build():
    """build ld.bin if it is missing; return (image, symbols)"""
    if not os.path.exists(f"{OUT}/ld.bin") or not os.path.exists(f"{OUT}/ld.syms"):
        import shbuild
        shbuild.build("tools/usbloader/build.sh", [], {})
    s = {}
    for line in open(f"{OUT}/ld.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return open(f"{OUT}/ld.bin", "rb").read(), s


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    img, s = build()
    base, end, entry = s["ld_head"], s["end"], s["ld_open"]
    size = (end - base + 3) & ~3
    want = bytes(img) + bytes(size - len(img))
    print(f"loader {len(img)} B at {base:#x}, state to {end:#x} ({size} B), "
          f"entry {entry:#x}")

    import hid
    import fastload
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, send

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    def zrun(h, a, n):
        """bytes of the run from a that are zero (reads 32 at a time)"""
        k = 0
        while k < n:
            m = min(32, n - k)
            got = bytes(br(h, a + k, m))
            for i, b in enumerate(got):
                if b:
                    return k + i
            k += m
        return n

    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if len(c) != 1:
        sys.exit(f"found {len(c)} shell interface(s) - plug in exactly one deck")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        live = rd32(h, POOL)
        print(f"mount-open pool = {live:#x} (stock {STOCK_OPEN:#x}, ours {entry:#x})")
        if arg == "--check":
            if live == entry:
                print("RESULT: already armed")
            elif live == STOCK_OPEN:
                n = zrun(h, base, size)
                if n == size:
                    print("RESULT: not installed; cave free")
                elif bytes(br(h, base, 4)) == img[:4]:
                    print("RESULT: not installed; cave holds OUR loader "
                          "(a --revert leftover - installing re-arms it)")
                else:
                    print(f"RESULT: not installed; cave holds FOREIGN data "
                          f"from +{n:#x} - an install would refuse")
            else:
                print("RESULT: pool is neither stock nor ours - someone else owns it")
            return 0
        if arg == "--revert":
            if live != entry:
                print("RESULT: not armed - nothing to revert")
                return 0
            lw(h, POOL, STOCK_OPEN)
            if rd32(h, POOL) != STOCK_OPEN:
                sys.exit("FAIL: pool not restored - REBOOT the deck")
            print(f"RESULT: reverted (pool = {STOCK_OPEN:#x})")
            return 0
        if live == entry:
            print("RESULT: PASS (already armed)")
            return 0
        if live != STOCK_OPEN:
            sys.exit(f"REFUSED: mount-open pool is {live:#x}, not stock - nothing written")
        n = zrun(h, base, size)
        if n != size and bytes(br(h, base, 4)) == img[:4]:
            # our own bytes left by a --revert: nothing to write, just re-arm
            lw(h, POOL, entry)
            if rd32(h, POOL) != entry:
                sys.exit("FAIL: arm verify - pool unchanged")
            print("RESULT: PASS - loader already in RAM, re-armed")
            return 0
        if n != size:
            sys.exit(f"REFUSED: cave holds foreign data from {base + n:#x} - nothing written")
        left = fastload.install_region(h, base, want, fastload.SESSION_BUDGET)
        if left:
            print(f"RESULT: PARTIAL ({left} B left) - run again")
            return 0
        lw(h, POOL, entry)
        if rd32(h, POOL) != entry:
            lw(h, POOL, STOCK_OPEN)
            sys.exit("FAIL: arm verify - pool restored; REBOOT before playing")
        print("RESULT: PASS - insert a stick with XDJMODS/PACK.BIN; the mods "
              "appear when it mounts (power-cycle = stock)")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
