#!/usr/bin/env python3
"""deck_ram_state.py -- read-only: is the deck clean, and what is in its RAM?

The authority for "stock" is the firmware image, exactly as tools/deck_check.py
uses it: a pool word is stock when it still equals the firmware's own value at
that address. Anything else is a mod leftover, and the two kinds are worth
telling apart:

    0x09xxxxxx   our module's cave -- something is installed and working
    0x081d8/0x081dxxxx   the installer's own low-RAM trampolines. These look
                    like ordinary firmware addresses, which is exactly how a
                    half-finished install gets misread as "stock": the run
                    died after re-pointing the pool words and before writing
                    the module, so the deck is half-linked until it is cycled.

    .venv/bin/python tools/deck_ram_state.py
"""
import os
import struct
import sys

sys.path[:0] = [".", "tools"]
import hid                                                       # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain        # noqa: E402
from rgb_overview_apply import br, send                           # noqa: E402

BASE = 0x08000000
POOLS = [0x08DBC5C8, 0x08DC3D94, 0x08D5278C, 0x08C92E20, 0x08D5FFA8,
         0x08B8AFE8, 0x08B91828]


def blk1():
    try:
        from emu import fwhome
        p = fwhome.path("blk1.bin")
    except Exception:
        p = None
    for c in (p, "cache/blk1.bin"):
        if c and os.path.exists(c):
            return open(c, "rb").read()
    sys.exit("firmware image not set up (Home > Firmware: Add)")


def main():
    img = blk1()
    c = [d for d in hid.enumerate()
         if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(c) != 1:
        sys.exit("deck not found over USB (usage 1)")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 0x01)
    try:
        drain(h, 0.8)
        send(h, "$CDJ", 1.5)
        leftovers = []
        print("hook words (stock = firmware image value):")
        for a in POOLS:
            live = struct.unpack("<I", bytes(br(h, a, 4)))[0]
            stock = struct.unpack("<I", img[a - BASE:a - BASE + 4])[0]
            if live == stock:
                kind = "stock"
            elif (live >> 24) == 0x09:
                kind = "OUR MODULE (cave)"
                leftovers.append(a)
            else:
                kind = "LEFTOVER trampoline" if (live >> 24) == 0x08 else "LEFTOVER"
                leftovers.append(a)
            print(f"  {a:#x} = {live:#010x}  firmware {stock:#010x}  {kind}")
        print()
        if not leftovers:
            print("RESULT: CLEAN - every hook word still holds its stock value")
        else:
            print(f"RESULT: {len(leftovers)} of {len(POOLS)} hook words are "
                  "re-pointed -> this deck is half- or fully linked")
            print("  Switch the XDJ-700 OFF and ON, wait for its home screen, "
                  "then install once.")
    finally:
        h.close()


if __name__ == "__main__":
    main()
