#!/usr/bin/env python3
"""stems_ringmap_deck.py -- with the ring-map build of the stems module
installed (SM_CFLAGS=-DSM_MAP), mute one stem from the shell, wait for the
module to read every slot label of the chip's decoded store (~2 s), print the
map, and put the mask back. Read-only apart from the one mask byte.

    WAIT=600 SETTLE=40 .venv/bin/python tools/stems_ringmap_deck.py

SETTLE: seconds of playback before the mute (the slow case is mid-song).
The output says where the copies of each block live and which one the chip
plays (PLAYBLK / PSLOT before and after the scan).
"""
import os
import struct
import sys
import time

sys.path[:0] = [".", "tools"]
import hid                                                       # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain        # noqa: E402
from rgb_overview_apply import br, bw, send, read_block           # noqa: E402
import perform_pads as PP                                         # noqa: E402
from stems_mute_deck import load                                  # noqa: E402


def main():
    s = load("build/stems/sm.syms")
    if "sm_map" not in s:
        sys.exit("build/stems is not the ring-map build (SM_CFLAGS=-DSM_MAP)")
    wait = int(os.environ.get("WAIT", "600"))
    settle = float(os.environ.get("SETTLE", "40"))
    stem = int(os.environ.get("STEM", "1"))
    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if not c:
        sys.exit("deck not found on USB (usage 1)")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        u32 = lambda a: struct.unpack("<I", bytes(br(h, a, 4)))[0]
        print(f"waiting up to {wait}s for a STEMS file ...", flush=True)
        t0 = time.time()
        while not u32(s["sm_files"]):
            if time.time() - t0 > wait:
                sys.exit("no STEMS file was opened - load one and run again")
            time.sleep(1.5)
        print(f"STEMS file open; letting it play {settle:.0f}s", flush=True)
        time.sleep(settle)
        done0 = u32(s["sm_mapdone"])
        old = bytes(br(h, PP.PMASK, 1))[0]
        new = old ^ (1 << stem)
        print(f"mask {old:#04x} -> {new:#04x}", flush=True)
        bw(h, PP.PMASK, new)
        t1 = time.time()
        while u32(s["sm_mapdone"]) == done0:
            if time.time() - t1 > 20:
                print("map did not finish in 20 s (is the track playing?)")
                break
            time.sleep(0.5)
        # read BEFORE restoring the mask: the restore starts a new scan
        n = u32(s["sm_mapn"])
        raw = bytes(read_block(h, s["sm_map"], max(12 * n, 4)))
        g = lambda k: u32(s["sm_" + k])
        p0, ps0, p1, ps1 = g("mapp0"), g("mapps0"), g("mapp1"), g("mapps1")
        junk, lost = g("mapjunk"), g("maplost")
        bw(h, PP.PMASK, old)
        print(f"mask restored to {old:#04x}")
    finally:
        h.close()
    runs = [struct.unpack_from("<III", raw, 12 * i) for i in range(n)]
    print(f"\nruns {n}  junk slots {junk}  runs lost {lost}")
    print(f"PLAYBLK {p0} -> {p1}   PSLOT {ps0} -> {ps1}")
    for sl, lab, ln in runs:
        tag = []
        if lab <= p0 < lab + ln:
            tag.append(f"holds PLAYBLK at slot {sl + p0 - lab}")
        if sl <= ps0 < sl + ln:
            tag.append(f"PSLOT here = block {lab + ps0 - sl}")
        print(f"  slots {sl:5d}..{sl + ln - 1:5d}  blocks {lab:7d}..{lab + ln - 1:7d}"
              f"  ({ln:5d})  {'; '.join(tag)}")
    with open("build/stems_ringmap_deck.log", "a") as f:
        f.write(f"{time.ctime()} p {p0}->{p1} ps {ps0}->{ps1} runs {runs}\n")


if __name__ == "__main__":
    main()
