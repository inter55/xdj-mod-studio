#!/usr/bin/env python3
"""stems_mute_deck.py -- do the stem mute from the shell, and report WHERE the
rewrite landed.

The PERFORM STEMS pad only writes one byte (PMASK bit i = stem i muted), so
tapping it with a finger is the slow, unmeasurable part of a deck test. This
writes the same byte, samples the engine's counters the moment it changes, and
puts the mask back. The counters then answer the only question that matters:

    did the rewrite reach the blocks the chip is about to PLAY (instant), or
    blocks further ahead (heard that many blocks late)?

    .venv/bin/python tools/stems_mute_deck.py              # mute VOCALS now
    STEM=0 .venv/bin/python tools/stems_mute_deck.py       # 0..3 = vocals..other
    WAIT=240 SETTLE=8 .venv/bin/python tools/stems_mute_deck.py --wait

--wait: do nothing until the deck has a STEMS file open (valid != 0), then let
playback settle SETTLE seconds so the decoded store is full, then fire. That
makes the hard case (mute mid-song, with ~28 s decoded ahead) the default one,
with no tap and no stopwatch. Nothing is installed or changed: the mask byte is
restored to whatever it was.
"""
import argparse
import os
import struct
import sys
import time

sys.path[:0] = [".", "tools"]
import hid                                                       # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain        # noqa: E402
from rgb_overview_apply import br, bw, send                       # noqa: E402
import perform_pads as PP                                         # noqa: E402

SYMS = "build/stems/sm.syms"
RING_BLOCK_MS = 588 / 44.1                                        # one block = 13.3 ms


def load(path):
    s = {}
    for line in open(path):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stem", type=int, default=int(os.environ.get("STEM", "0")),
                    help="0 vocals, 1 drums, 2 bass, 3 other (default 0)")
    ap.add_argument("--wait", nargs="?", const=600, type=int,
                    default=int(os.environ.get("WAIT", "0")),
                    help="seconds to wait for a STEMS file to be opened "
                         "(bare --wait = 600)")
    ap.add_argument("--settle", type=float, default=float(os.environ.get("SETTLE", "8")),
                    help="seconds of playback before the mute (with --wait)")
    ap.add_argument("--symbols", default=SYMS)
    ap.add_argument("--restore", action="store_true",
                    help="leave the stem muted (default: restore the old mask)")
    a = ap.parse_args()

    s = load(a.symbols)
    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if not c:
        sys.exit("deck not found on USB (usage 1)")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        u32 = lambda ad: struct.unpack("<I", bytes(br(h, ad, 4)))[0]
        u8 = lambda ad: bytes(br(h, ad, 1))[0]

        def snap():
            d = {k: u32(s[k]) for k in ("dr_next", "dr_ablk", "dr_aslot", "dr_on",
                                        "valid", "rfront") if k in s}
            for k in ("drblk", "drnoslot", "drcal", "calp", "cals", "drps",
                      "drlab0", "drlastp", "drlastb", "drlasth", "drwfail",
                      "drskip", "drbusy", "calls", "files", "polls", "hits",
                      "hdrmatch", "hdrps", "hdrp", "caps",
                      "hpibusy", "dranc", "drfix", "drscan", "lastpos"):
                n = "sm_" + k
                if n in s:
                    d[k] = u32(s[n])
            return d

        def show(tag, d):
            print(f"  {tag}: blocks {d.get('drblk')} noslot {d.get('drnoslot')} "
                  f"wfail {d.get('drwfail')} skip {d.get('drskip')} "
                  f"busy {d.get('drbusy')} cal {d.get('drcal')} "
                  f"(blk {d.get('calp')} slot {d.get('cals')} "
                  f"old-said {d.get('drps')} slot0 {d.get('drlab0')}) "
                  f"want {d.get('drlastb')} playing {d.get('drlastp')} "
                  f"hdr-seen {d.get('drlasth')} hpibusy {d.get('hpibusy')} "
                  f"anc {d.get('dranc')} fix {d.get('drfix')} scan {d.get('drscan')} "
                  f"readpos {d.get('lastpos')} calls {d.get('calls')}", flush=True)

        if a.wait:
            print(f"waiting up to {a.wait}s for a STEMS file to be opened ...", flush=True)
            t0 = time.time()
            while time.time() - t0 < a.wait:
                if u32(s["sm_files"]):        # a STEMS file has been opened
                    print(f"STEMS file open after {time.time() - t0:.1f}s; "
                          f"settling {a.settle:.0f}s", flush=True)
                    time.sleep(a.settle)
                    break
                time.sleep(1.5)
            else:
                sys.exit("no STEMS file was opened - load one and run again")

        pre = snap()
        print("before:")
        show("pre", pre)
        old = u8(PP.PMASK)
        new = old ^ (1 << a.stem) if a.stem < 4 else 0
        print(f"mask {old:#04x} -> {new:#04x} (stem {a.stem})", flush=True)
        bw(h, PP.PMASK, new)

        marks = []
        for t in (0.05, 0.20, 0.75, 2.0):
            time.sleep(t if not marks else t - marks[-1][0])
            marks.append((t, snap()))
            show(f"+{t:.2f}s", marks[-1][1])

        if "caps" in pre or any("caps" in m[1] for m in marks):
            print("\n  deck's header at the slot PSLOT names:")
            last = marks[-1][1]
            print(f"    PLAYBLK {last.get('hdrp')}  PSLOT {last.get('hdrps')}  "
                  f"word holding PLAYBLK: {last.get('hdrmatch')} "
                  f"(255 = none)  captures {last.get('caps')}")
            if "sm_hdr" in s:
                words = [u32(s["sm_hdr"] + 4 * i) for i in range(12)]
                print("    first 12 words: " + " ".join(f"{w:08x}" for w in words))

        first = marks[0][1]
        newb = (first.get("drblk") or 0) - (pre.get("drblk") or 0)
        cal = (first.get("drcal") or 0) - (pre.get("drcal") or 0)
        lead = None
        if first.get("dr_ablk") and first.get("drlastp"):
            lead = first["dr_ablk"] - first["drlastp"]
        print()
        print(f"blocks rewritten in the first 50 ms: {newb}")
        if cal:
            print(f"lookup calibrated {cal}x: block {first.get('calp')} at slot "
                  f"{first.get('cals')}; the old PSLOT formula said slot "
                  f"{first.get('drps')} (slot 0 holds block {first.get('drlab0')})")
        if lead is None:
            total = (marks[-1][1].get("drblk") or 0) - (pre.get("drblk") or 0)
            print(f"{total} blocks rewritten in 2 s" if total else
                  "no rewrite happened (no STEMS file open, or the mask was already "
                  "what you asked for)")
        else:
            print(f"first rewritten block is {lead} blocks after the playing block "
                  f"({lead * RING_BLOCK_MS:.0f} ms) -> "
                  + ("AT the playhead: the change is heard at once"
                     if lead <= 4 else
                     f"that far ahead: heard {abs(lead) * RING_BLOCK_MS / 1000:.2f} s "
                     f"{'later' if lead > 0 else 'in the past (impossible)'}"))
        if not a.restore:
            bw(h, PP.PMASK, old)
            print(f"mask restored to {old:#04x}")
    finally:
        h.close()


if __name__ == "__main__":
    main()
