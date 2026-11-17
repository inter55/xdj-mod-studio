#!/usr/bin/env python3
"""stems_probe.py -- READ-ONLY: the live-stems engine's counters on the deck
(calls, biggest/last read, files recognised, reads mixed), the fast-rewrite
counters, and the mute mask.

The deck may run an OLDER stems build than build/stems: the probe reads the
deck's fread pool and picks the symbol map whose sm_fread matches
(build/stems/sm.syms, else build/stems/sm_deck_*.syms). Reading with the
wrong map gives garbage (2026-11-03: it did)."""
import builtins
import glob
import os
import struct
import sys
import time
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


# 2026-11-10: three deck sessions' counters were lost (read on screen, never
# written down). Every run is now also appended to docs/deck_logs/.
os.makedirs("docs/deck_logs", exist_ok=True)
_LOG = open(time.strftime("docs/deck_logs/stems_probe_%Y-%m-%d.log"), "a")
_LOG.write(time.strftime("\n==== %H:%M:%S ") + " ".join(sys.argv[1:]) + "\n")


def print(*a, **k):                                 # noqa: A001
    builtins.print(*a, **k)
    builtins.print(*a, file=_LOG)
    _LOG.flush()


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
        # Another module (FLAC) chained on top of sm_fread: the pool is that
        # module's reader, not sm_fread. The counters are read by absolute
        # symbol address, so they stay valid -- say so instead of refusing
        # (2026-11-07: the FLAC step of a full install did exactly this).
        known = ", ".join(f"{p}: {s.get('sm_fread', 0):#x}" for p, s in maps)
        pick = maps[0]
        print(f"note: pool {pool:#x} is chained above sm_fread "
              f"({pick[1].get('sm_fread', 0):#x}); known maps: {known}")
        print("      reading counters by symbol address (counters unaffected "
              "by the chain)")
    path, syms = pick
    print(f"stems build on the deck: {path}")
    rd = lambda n: u32(syms[n]) if n in syms else None
    base = {k: rd("sm_" + k) for k in ("calls", "hits", "files", "valid", "maxn", "lastn",
                                       "lastpos", "remix", "remixb")}
    fast = {k: rd("sm_" + k) for k in ("polls", "parm", "drblk", "drscan", "drskip", "drbusy",
                                       "drnoslot", "drwfail", "hpibusy", "dranc", "drbump", "drms", "drfade", "drlate", "drsub", "drsubf", "drchk", "drbat", "drtot", "drpk", "drlastp", "drlastb",
                                       "drcal", "calp", "cals", "drps", "drlab0")}
    inner = {k: rd(k) for k in ("dr_on", "dr_mask", "dr_next", "dr_end", "dr_ablk", "dr_aslot",
                                "valid", "rfront", "d_off")}
    print("mask", bytes(br(h, PP.PMASK, 1)).hex(), {k: v for k, v in base.items() if v is not None})
    print("  fast rewrite:", {k: v for k, v in fast.items() if v is not None})
    print("  state:", {k: (hex(v) if v is not None and v > 0xFFFF else v)
                       for k, v in inner.items() if v is not None})
    if fast.get("drbat"):
        print(f"  rewrite cost: {fast['drbat']} batches, {fast['drtot']} ms in them "
              f"(average {fast['drtot'] / fast['drbat']:.1f} ms, longest {fast['drms']} ms), "
              f"longest masked section {fast.get('drpk', '-')} samples")
    tm = {k: rd("sm_" + k) for k in ("tget", "tusb", "tput", "tn")}
    if tm.get("tn"):
        n = tm["tn"]
        print(f"  per block: chip read {tm['tget'] / n:.1f} ms, stick read {tm['tusb'] / n:.1f} ms, "
              f"chip write {tm['tput'] / n:.1f} ms ({n} blocks)")
    thr = {k: rd("sm_" + k) for k in ("thr", "thrms", "thrpause")}
    if thr.get("thr") is not None:
        print(f"  short read-ahead: {thr['thr']} reads held, {thr['thrms']} ms held, "
              f"{thr['thrpause']} let through (paused/cued)")
    # 2026-11-16: is the chip kept fed? (values are stored +1, 0 = none yet)
    fed = {k: rd("sm_" + k) for k in ("thrlo", "thrlopre", "lvl", "lvlmin", "lvlpre", "thrmaxw",
                                       "rdms", "rdtot", "runs", "lvlh", "posh")}
    if fed.get("thrlo") is not None:
        m1 = lambda v: "-" if not v else v - 1          # noqa: E731
        print(f"  chip fed? lowest read-ahead when a read arrived: {m1(fed['thrlo'])} blocks since the "
              f"last tap, {m1(fed['thrlopre'])} before it (normal: just under the limit; "
              f"under ~260 = reads are late)")
        print(f"            chip buffer level: now {fed['lvl']}, lowest {m1(fed['lvlmin'])} since the "
              f"last tap, {m1(fed['lvlpre'])} before it (the deck slows playback near 240)")
        nh = base.get("hits") or 0           # not `h`: that is the deck handle below
        print(f"            muted reads: {nh}, average {fed['rdtot'] / nh if nh else 0:.1f} ms, longest "
              f"{fed['rdms']} ms; holds that ran out: {fed['thrmaxw']}; read runs (loads/jumps): {fed['runs']}")
        if fed.get("lvlh") is not None or fed.get("posh") is not None:
            print(f"            holds: chip buffer {fed.get('lvlh') or 0}, "
                  f"position fallback {fed.get('posh') or 0} (fallback > 0 = the level word "
                  f"was unreadable or implausible on this deck)")
    print("  pool", hex(pool), "direct", hex(u32(0x08DDFFD4)))
finally:
    h.close()
