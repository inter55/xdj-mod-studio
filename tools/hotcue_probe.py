#!/usr/bin/env python3
"""hotcue_probe.py -- READ-ONLY, one session: how THIS deck stores the loaded
track's hot cues.

Load a track that has hot cues A-D or more set in rekordbox, then:

    .venv/bin/python tools/hotcue_probe.py

Reads (RAM window only, no writes): the three live cue slots (0x0B6C4118,
0x40 each), the two 8-record pad models (0x0BA28800, 0x0BA2A52C, 0x38 each),
and -- when the hot-cue module is installed -- its counters. Everything is
also appended to docs/deck_logs/hotcue_probe_<date>.log.

Why: hc.c decided "pad has a cue" from the slot's state word == 6. That value
was only ever seen in the emulator with test cues written with status 4; real
rekordbox cues carry status 0 (2026-11-10, user report: A-H view empty).
"""
import builtins
import os
import struct
import sys
import time
sys.path[:0] = [".", "tools"]
import hid                                          # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain   # noqa: E402
from rgb_overview_apply import read_block, send     # noqa: E402

SLOTS, MODELS, MS = 0x0B6C4118, (("A", 0x0BA28800), ("B", 0x0BA2A52C)), 0x38

os.makedirs("docs/deck_logs", exist_ok=True)
_LOG = open(time.strftime("docs/deck_logs/hotcue_probe_%Y-%m-%d.log"), "a")
_LOG.write(time.strftime("\n==== %H:%M:%S\n"))


def print(*a, **k):                                 # noqa: A001
    builtins.print(*a, **k)
    builtins.print(*a, file=_LOG)
    _LOG.flush()


def main():
    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if not c:
        print("no deck found on USB")
        return 1
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        b = bytes(read_block(h, SLOTS, 0xC0))
        print("live slots (pad A, B, C):")
        for k in range(3):
            s = b[k * 0x40:(k + 1) * 0x40]
            st, num, ms, loop, pos = struct.unpack_from("<HHIII", s, 8)
            print(f"  {'ABC'[k]}: state {st:#06x} number {num:#06x} ms {ms} loop {loop:#x} "
                  f"pos150 {pos}   raw +0..+0x20: {s[:0x20].hex()}")
        for name, base in MODELS:
            b = bytes(read_block(h, base, 8 * MS))
            print(f"pad model {name}:")
            for k in range(8):
                st, num, ms, loop, pos = struct.unpack_from("<HHIII", b, k * MS)
                print(f"  {'ABCDEFGH'[k]}: state {st:#06x} number {num:#06x} ms {ms} "
                      f"loop {loop:#x} pos150 {pos}")
        try:
            syms = {}
            for line in open("build/hotcue8/hc.syms"):
                a, _, n = line.split()
                syms[n.lstrip("_")] = int(a, 16)
            w = lambda n: struct.unpack("<I", bytes(read_block(h, syms[n], 4)))[0]   # noqa: E731
            if w("hc_entry") if "hc_entry" in syms else True:
                print("hot-cue module counters (only meaningful if it is installed):",
                      {n: w(n) for n in ("hc_caps", "hc_count", "hc_view", "hc_sets") if n in syms})
        except Exception as e:                      # noqa: BLE001
            print("module counters not read:", e)
    finally:
        h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
