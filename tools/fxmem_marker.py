#!/usr/bin/env python3
"""fxmem_marker.py -- is the occupied block LIVE code/audio, or LEFTOVER?

    .venv/bin/python tools/fxmem_marker.py             # look only, write nothing
    .venv/bin/python tools/fxmem_marker.py --write     # write 4 marker words
    .venv/bin/python tools/fxmem_marker.py --self-test # check the logic, no deck

The survey found `0xC0073400 .. 0xC0080000` full of data on the real deck, so
Echo cannot be installed there. Full of data does not say whether the chip is
USING it (a delay line, a scratch buffer) or whether it is dead leftovers from
boot. Only a write answers that: put a word in, watch whether it stays.

What a stayed marker proves, exactly: the chip did not rewrite those words
while we watched, and it accepted ours. That makes the block a *candidate*,
not proven free -- the deck has to keep sounding right, which is why the last
step is a listening check.

Marker value: a denormal float (`0x00000001`..`0x00000007`, ~1.4e-45). If the
block turns out to be a live audio buffer, one near-silent sample replaces one
sample -- a soft tick at worst. A "loud-looking" marker would be a full-scale
click, which is how you damage ears and tweeters; never use one here.

Safety:
  * read-only unless --write is given;
  * landmarks are checked first (program words + the 1.0f cosine table), so a
    bad read can never be mistaken for a free block;
  * originals are read first and ALWAYS put back (also on Ctrl-C / error),
    each write is one atomic LW, and every write is read back;
  * only four words, only inside the surveyed block, only 4-aligned.

    Python 3 -- py_compile clean; logic covered by --self-test.
"""
import os
import struct
import sys
import time

sys.path.insert(0, "tools")
import hid                                                  # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain  # noqa: E402
from rgb_overview_apply import send, read_block             # noqa: E402
import fxmem_probe as P                                     # noqa: E402

PROG = 0xC0000000           # DSP program start (landmark)
COS = 0xC0060000            # key-shift cosine table: 1.0f, 0, 0, 1.0f
EXP_PROG = (0x000ED02A, 0x0008C06A, 0x00000362, 0x004003E2)   # fw 1.15
EXP_COS = 0x3F800000
MARKS = (0x00000001, 0x00000003, 0x00000005, 0x00000007)
WAITS = (0.15, 0.6, 1.5)    # read the markers back at these times (s)
CHURN_S = 0.4               # neighbour re-read gap for the look-only check


def targets():
    """four words spread evenly through the surveyed block"""
    lo, hi = P.BLK_LO, P.BLK_HI
    step = (hi - lo) // 4
    return [(lo + step * i) & ~3 for i in range(4)]


def win(h, addr):
    """32-B window with `addr` as its 4th word: (target, neighbours)"""
    w = struct.unpack("<8I", bytes(read_block(h, addr - 12, 32)))
    return w[3], w[:3] + w[4:]


def landmarks(h):
    prog = struct.unpack("<4I", bytes(read_block(h, PROG, 16)))
    cos = struct.unpack("<2I", bytes(read_block(h, COS, 8)))
    ok = tuple(prog) == EXP_PROG and cos[0] == EXP_COS
    print("landmarks: program " + " ".join(f"{v:08X}" for v in prog) +
          "  cos " + " ".join(f"{v:08X}" for v in cos) +
          "  (" + ("real, correctly addressed memory" if ok else
                   "DO NOT MATCH fw 1.15 - refusing to write") + ")")
    return ok


def classify(imm, seen, mark, orig):
    """held / changed / rejected.

    `imm` is the read-back taken straight after the write (no waiting), so a
    word that never took is 'rejected'; a word that took and later showed
    something else was overwritten by the chip -> 'changed'. Without that
    separate immediate read, a live buffer's rewrite looks like a refused
    write (it did in the emulator).
    """
    if orig == mark:
        return "same-as-original"          # cannot tell: pick another word
    if imm != mark:
        return "rejected"                  # the write never landed
    if all(v == mark for v in seen):
        return "held"
    return "changed"                       # the chip put its own data back


def self_test():
    cases = [
        (1, [1, 1], 1, 0, "held"),
        (1, [1, 7], 1, 0, "changed"),
        (0, [0, 0], 1, 0, "rejected"),
        (1, [], 1, 0, "held"),
        (1, [1, 1], 1, 1, "same-as-original"),
    ]
    bad = 0
    for imm, seen, mark, orig, want in cases:
        got = classify(imm, seen, mark, orig)
        bad += got != want
        print(f"  now {imm} later {str(seen):10} -> {got:16} "
              f"(wanted {want}) {'ok' if got == want else 'WRONG'}")
    print("self-test:", "PASS" if not bad else f"FAIL ({bad} wrong)")
    return 0 if not bad else 1


def main():
    if "--self-test" in sys.argv:
        return self_test()
    do_write = "--write" in sys.argv
    for a in targets():
        if not (P.BLK_LO <= a and a + 4 <= P.BLK_HI) or a % 4:
            sys.exit(f"REFUSED: {a:#x} is outside the surveyed block")

    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"STOP: found {len(cands)} deck interface(s). Check the "
                 "cable, switch the deck on, quit rekordbox / DJ apps.")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    t0 = time.time()
    orig, back = {}, {}
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        if P.r32(h, P.POOL) != P.STOCK:
            sys.exit(f"STOP: the display-flush hook holds "
                     f"{P.r32(h, P.POOL):#x}, not stock. Switch the deck OFF "
                     "and ON, then run again. Nothing was written.")
        if not landmarks(h):
            return 1

        tg = targets()
        first = {a: win(h, a) for a in tg}
        for a in tg:
            orig[a] = first[a][0]
        print("block words (no writes yet):")
        for a in tg:
            print(f"  0x{a:08X} = {orig[a]:08X}  neighbours "
                  + " ".join(f"{v:08X}" for v in first[a][1]))

        # read-only churn check: is the chip writing near these words itself?
        time.sleep(CHURN_S)
        churn = {a: sum(1 for x, y in zip(first[a][1], win(h, a)[1]) if x != y)
                 for a in tg}
        moving = sum(churn.values())
        print(f"  neighbours that changed by themselves in {CHURN_S * 1000:.0f}"
              f" ms: {moving} of {8 * len(tg)}"
              + ("  -> the chip IS writing here" if moving else
                 "  -> nothing changing right now"))

        if not do_write:
            for a in tg:
                print(f"  0x{a:08X} = {orig[a]:08X}")
            print(f"RESULT: READ-ONLY LOOK in {time.time() - t0:.1f}s. "
                  + ("This block is being written by the chip, so it is in "
                     "use." if moving else
                     "Nothing changed on its own, which is not yet an "
                     "answer.") +
                  " Run again with --write to settle live-vs-leftover.")
            return 0

        print(f"writing {len(tg)} marker words (one LW each, read back) ...")
        imm = {}
        try:
            for a, m in zip(tg, MARKS):
                P.w32(h, a, m)
            imm = {a: P.r32(h, a) for a in tg}      # straight away
            hist = {a: [] for a in tg}
            base = time.time()
            for w in WAITS:
                time.sleep(max(0.0, base + w - time.time()))
                for a in tg:
                    hist[a].append(P.r32(h, a))
        finally:
            for a in tg:
                P.w32(h, a, orig[a])
                back[a] = P.r32(h, a)

        verdicts = {a: classify(imm[a], hist[a], m, orig[a])
                    for a, m in zip(tg, MARKS)}
        print("marker results:")
        for a, m in zip(tg, MARKS):
            print(f"  0x{a:08X}: wrote {m:08X}, now {imm[a]:08X}, then "
                  + " ".join(f"{v:08X}" for v in hist[a])
                  + f"  -> {verdicts[a]}")
        lost = [a for a in tg if back[a] != orig[a]]
        print(f"  originals put back: "
              + ("yes, all 4 verified" if not lost else
                 "NO - " + " ".join(f"0x{a:08X}" for a in lost)))
        if lost:
            print("RESULT: FAIL - a word did not go back. Re-run "
                  "tools/fxmem_probe.py --revert, then power-cycle.")
            return 1

        held = sum(1 for v in verdicts.values() if v == "held")
        if held == len(tg):
            print(f"RESULT: NOT BEING WRITTEN - all {held} markers held for "
                  f"{WAITS[-1]:.1f}s and nothing nearby moved. The block is a "
                  "candidate. Listen: any click or drop-out while this ran?")
            return 0
        if any(v in ("changed", "rejected") for v in verdicts.values()):
            print(f"RESULT: IN USE - {len(tg) - held} of {len(tg)} markers "
                  "were overwritten or refused. This block belongs to the "
                  "chip; Echo and Flanger cannot live here.")
            return 2
        print("RESULT: UNCLEAR - some words equalled our own marker already. "
              "Re-run with a different marker set.")
        return 2
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
