#!/usr/bin/env python3
"""dsp_ringmap.py -- map the audio chip's decoded store, offline.

The stem mute has to write the copy of a block that the chip will PLAY, and
three things about that store were guesses until now: where a block sits
relative to the playhead, whether a block exists more than once, and which DSP
word (if any) really names the slot being played. 0x1182E81C ("PSLOT") is NOT
that word: on the deck it pointed 1182 slots away from where the playing block
actually was.

This reads the store out of the emulator (real DSP program, so the layout is
the deck's) and prints:

  * the label map: which block number each slot's header names, its span,
    whether labels repeat, and the slot-minus-label offsets involved;
  * every slot carrying the PLAYING block, with its 100-byte header in hex, so
    a live copy can be told from a stale one;
  * a scan of the DSP control area for a word equal to the playing slot -- the
    candidate for the real play-slot word.

    .venv/bin/python tools/dsp_ringmap.py                     # cache/emu_flush_playing.pkl
    SNAP=... .venv/bin/python tools/dsp_ringmap.py
"""
import os
import sys
from collections import Counter

sys.path[:0] = [".", "tools"]

RING = 0xC0090000
SLOT = 4804
NSLOT = 13846
HDR = 4704
PLAYBLK = 0x11837CCC
PSLOT = 0x1182E81C
CTL_LO, CTL_HI = 0x1182E000, 0x11830000


def main():
    from emu.machine import Machine

    snap = os.environ.get("SNAP", "cache/emu_flush_playing.pkl")
    if not os.path.exists(snap):
        sys.exit(f"missing snapshot {snap}")
    print(f"loading {snap} ...", flush=True)
    m = Machine.load(snap)
    d = m.mem.dev.dspemu
    rd = lambda a: d.bus.read(a, 4) & 0xFFFFFFFF

    p, ps = rd(PLAYBLK), rd(PSLOT)
    print(f"PLAYBLK {p}   PSLOT(0x1182E81C) {ps}   status {rd(0x11837BF8)} "
          f"play-state {rd(0x11837CD0)}", flush=True)

    labels = {}
    for s in range(NSLOT):
        v = rd(RING + s * SLOT + HDR)
        if v != 0xFFFFFFFF:
            labels[s] = v
    nz = {s: v for s, v in labels.items() if v}
    print(f"slots scanned {NSLOT}; headers that read as a block number {len(nz)}")
    if not nz:
        sys.exit("no labels found -- wrong base/offset for this firmware")
    span = (min(nz.values()), max(nz.values()))
    print(f"label range {span[0]}..{span[1]}  ({span[1] - span[0] + 1} blocks named)")
    print(f"slot 0..5 labels: {[labels.get(s, 0) for s in range(6)]}")

    dup = Counter(nz.values())
    rep = {b: c for b, c in dup.items() if c > 1}
    print(f"blocks present in more than one slot: {len(rep)}"
          + (f"  e.g. {list(rep.items())[:6]}" if rep else ""))

    off = Counter(s - v for s, v in nz.items())
    print(f"slot-minus-label offsets (top 6): {off.most_common(6)}")

    where = [s for s, v in nz.items() if v == p]
    print(f"\nslots whose header names the PLAYING block {p}: {where or 'none'}")
    for s in where[:3]:
        base = RING + s * SLOT
        hdr = [rd(base + HDR + 4 * i) for i in range(12)]
        print(f"  slot {s}: header words " + " ".join(f"{w:08x}" for w in hdr))
        print(f"           data[0..3] " + " ".join(f"{rd(base + 4 * i):08x}"
                                                  for i in range(4)))
    for t in (p + 1, p + 2):
        w = [s for s, v in nz.items() if v == t]
        print(f"  next block {t} is in slots {w}")

    if where:
        want = where[0]
        hits = []
        for a in range(CTL_LO, CTL_HI, 4):
            if rd(a) == want:
                hits.append(a)
        print(f"\ncontrol words equal to the playing slot {want}: "
              f"{[hex(a) for a in hits[:12]] or 'none'}")
        for a in hits[:6]:
            print(f"  {a:#x}: {rd(a)}   neighbours "
                  f"{rd(a - 4)} {rd(a + 4)}")

    print("\nPSLOT is " + ("the playing slot" if ps in where else
                          "NOT the playing slot"))


if __name__ == "__main__":
    main()
