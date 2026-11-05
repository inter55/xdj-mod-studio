#!/usr/bin/env python3
"""stems_probe.py -- READ-ONLY: the live-stems engine's counters on the deck
(calls, biggest/last read, files recognised, reads mixed), the fast-rewrite
counters, and the mute mask.

The deck may run an OLDER stems build than build/stems: the probe reads the
deck's fread pool and picks the symbol map whose sm_fread matches
(build/stems/sm.syms, else build/stems/sm_deck_*.syms). Reading with the
wrong map gives garbage (2026-11-03: it did)."""
import glob
import struct
import sys
sys.path[:0] = [".", "tools"]
import hid                                          # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain   # noqa: E402
from rgb_overview_apply import br, send             # noqa: E402
import perform_pads as PP                            # noqa: E402


def load(path):
    s = {}
    for line in open(path):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


maps = [(p, load(p)) for p in ["build/stems/sm.syms"] + sorted(glob.glob("build/stems/sm_deck_*.syms"))]
c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
try:
    drain(h, 1.0)
    send(h, "$CDJ", 1.5)
    u32 = lambda a: struct.unpack("<I", bytes(br(h, a, 4)))[0]
    pool = u32(0x08D5FFA8)
    pick = next(((p, s) for p, s in maps if s.get("sm_fread") == pool), None)
    if pick is None:
        known = ", ".join(f"{p}: {s.get('sm_fread', 0):#x}" for p, s in maps)
        print(f"deck fread pool {pool:#x} matches NO known stems build ({known}) - nothing read")
        sys.exit(1)
    path, syms = pick
    print(f"stems build on the deck: {path}")
    rd = lambda n: u32(syms[n]) if n in syms else None
    base = {k: rd("sm_" + k) for k in ("calls", "hits", "files", "valid", "maxn", "lastn",
                                       "lastpos", "remix", "remixb")}
    fast = {k: rd("sm_" + k) for k in ("polls", "parm", "drblk", "drscan", "drskip", "drbusy",
                                       "drnoslot", "drwfail", "drlastp", "drlastb")}
    inner = {k: rd(k) for k in ("dr_on", "dr_mask", "dr_next", "dr_end", "dr_ablk", "dr_aslot",
                                "valid", "rfront", "d_off")}
    print("mask", bytes(br(h, PP.PMASK, 1)).hex(), {k: v for k, v in base.items() if v is not None})
    print("  fast rewrite:", {k: v for k, v in fast.items() if v is not None})
    print("  state:", {k: (hex(v) if v is not None and v > 0xFFFF else v)
                       for k, v in inner.items() if v is not None})
    print("  pool", hex(pool), "direct", hex(u32(0x08DDFFD4)))
finally:
    h.close()
