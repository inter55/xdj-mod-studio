#!/usr/bin/env python3
"""rgb_detail_apply.py [--revert] -- RGB / 3-BAND centre waveform on the DECK.

Emulator PASS (tools/rgb_detail.py emu, WMODE=rgb and WMODE=3band: 249
cols, 0 mismatches). Uses the overview cave's mode byte (1 = RGB, 2 =
3-band on both waveforms), so the overview install (rgb_overview_apply.py)
must be live first. Pieces: the C colour module (tools/centre/cw.c,
CW_BASE, deck-free in idle/loaded/playing), the 52-B shim at DCAVE, and
three hook halves: palette pool -> shim, `shll r2` -> `jsr @r4`, and the
row-loop load `mov.w @r9,r2` -> `mov.w @r9+,r2` (one colour per row).

SAFETY GATE: the centre painter runs every frame while a track is loaded
and never with no track. The 6 hook bytes are only written when the
painter's track length [0x0BA3C050] reads 0 (no track) -- i.e. right
after a reboot, before loading anything. Otherwise: refuse, nothing
written. Chunked (<=120 B/session) like the overview installer; rerun
until PASS.
"""
import struct
import sys
import time

sys.path.insert(0, "tools")
import hid
from xdj700_usb_shell import PIONEER_VID, open_deck, drain
from rgb_overview_apply import br, bw, read_block, send, CHUNK_B
from rgb_overview import HOOK_POOL, CAVE
import rgb_overview as RO
from rgb_detail import custom_writes
from rgb_detail import (POOL2, PALETTE2, INSN2, INSN2_NEW, ROWINC2, DCAVE2,
                        dcave2_bytes)
from rgb_detail import (POOL, PALETTE, INSN, INSN_OLD, INSN_NEW, DCAVE,
                        DSCRATCH, dcave_bytes, copy_bytes, COPY5, ROWINC,
                        CW_BASE, cw_build)

TRACK_LEN = 0x0BA3C050


def rd32(h, a):
    return struct.unpack("<I", bytes(br(h, a, 4)))[0]


def rd16(h, a):
    return struct.unpack("<H", bytes(br(h, a, 2)))[0]


def main():
    args = sys.argv[1:]
    cols = None
    if "--colours" in args:                   # custom waveform: 1-3 '#rrggbb'
        i = args.index("--colours")
        cols = [RO.hex565(c) for c in args[i + 1].split(",") if c.strip()][:3]
        del args[i:i + 2]
    revert = args == ["--revert"]
    code = dcave_bytes()
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        tl = rd32(h, TRACK_LEN)
        pool, insn = rd32(h, POOL), struct.unpack("<H", bytes(br(h, INSN, 2)))[0]
        print(f"track length {tl}, pool {pool:#x}, insn {insn:#06x}")
        if tl != 0:
            sys.exit("REFUSED: a track is loaded (centre painter running). "
                     "Reboot the deck and run this BEFORE loading a track.")
        if revert:
            if rd32(h, POOL2) == DCAVE2:                     # PERFORM strip
                for i, b in enumerate(struct.pack("<H", ROWINC2[1])):
                    bw(h, ROWINC2[0] + i, b)
                for i, b in enumerate(struct.pack("<H", INSN_OLD)):
                    bw(h, INSN2 + i, b)
                for i, b in enumerate(struct.pack("<I", PALETTE2)):
                    bw(h, POOL2 + i, b)
            for i, b in enumerate(struct.pack("<H", ROWINC[1])):
                bw(h, ROWINC[0] + i, b)
            for i, b in enumerate(struct.pack("<H", INSN_OLD)):
                bw(h, INSN + i, b)
            for i, b in enumerate(struct.pack("<I", PALETTE)):
                bw(h, POOL + i, b)
            print("RESULT: reverted (stock centre waveform)")
            return 0
        if rd32(h, HOOK_POOL) != CAVE:
            sys.exit("FAIL: overview cave not installed (mode byte lives "
                     "there) - run rgb_overview_apply.py first")
        if insn not in (INSN_OLD, INSN_NEW) or pool not in (PALETTE, DCAVE) \
                or rd16(h, ROWINC[0]) not in ROWINC[1:]:
            sys.exit("FAIL: hook site not stock/ours - aborting")
        twin = (rd32(h, POOL2), rd16(h, INSN2), rd16(h, ROWINC2[0]))
        if twin[0] not in (PALETTE2, DCAVE2) or twin[1] not in (INSN_OLD, INSN2_NEW) \
                or twin[2] not in ROWINC2[1:]:
            sys.exit("FAIL: PERFORM-strip hook site not stock/ours - aborting")
        # C colour module first (the shim calls it)
        import fastload
        cw, syms = cw_build()
        want = cw + bytes(syms["end"] - CW_BASE - len(cw))
        left = fastload.install_region(h, CW_BASE, want, fastload.SESSION_BUDGET)
        if left:
            print(f"RESULT: PARTIAL ({left} B left) - run again")
            return 0
        if cols:                              # custom colour tables (ours: any
            cw_ = custom_writes(cols, syms)   # earlier set may be there)
            lo, hi = cw_[0][0], cw_[-1][0] + 1
            tab = bytearray(hi - lo)
            for a_, b_ in cw_:
                tab[a_ - lo] = b_
            left = fastload.install_region(h, lo, bytes(tab), fastload.SESSION_BUDGET,
                                           allow_live=list(range(lo, hi)))
            if left:
                print(f"RESULT: PARTIAL (colour tables, {left} B left) - run again")
                return 0
            print(f"custom colour tables written ({hi - lo} B)")
        live = read_block(h, DCAVE, DSCRATCH + 2 - DCAVE)
        img = bytearray(code + bytes(DSCRATCH + 2 - DCAVE - len(code)))
        cp = copy_bytes()                      # snapshot routine (written
        img[COPY5 - DCAVE:COPY5 - DCAVE + len(cp)] = cp   # by either tool)
        c2 = dcave2_bytes()                    # PERFORM-strip adapter
        img[DCAVE2 - DCAVE:DCAVE2 - DCAVE + len(c2)] = c2
        img = bytes(img)
        if live != img:
            bad = [i for i, (a, b) in enumerate(zip(live, img)) if a and a != b]
            if bad:
                sys.exit(f"FAIL: dcave +{bad[0]:#x} holds foreign data")
            todo = [i for i, (a, b) in enumerate(zip(live, img)) if a != b]
            t = time.time()
            try:
                import fastload
                w0 = todo[0] & ~3
                w1 = (todo[-1] + 4) & ~3
                # fast path only if every byte between w0/w1 is zero-live
                # or a target (no foreign data possible: `bad` empty)
                safe = all(live[j] == 0 or j in todo for j in range(w0, w1))
                if todo and safe:
                    fastload.fastblock(h, DCAVE + w0, img[w0:w1])
                    if read_block(h, DCAVE, len(img)) != img:
                        sys.exit("FAIL: dcave verify")
                    print(f"fast path: {w1 - w0} B in {time.time() - t:.0f}s")
                    print("dcave complete - run again to hook")
                    return 0
                raise RuntimeError("partial-word danger")
            except SystemExit:
                raise
            except Exception as e:
                print(f"fast path unavailable ({e}); falling back to BW")
            for i in todo[:CHUNK_B]:
                bw(h, DCAVE + i, img[i])
            if len(todo) > CHUNK_B:
                print(f"RESULT: PARTIAL ({len(todo) - CHUNK_B} B left) - "
                      f"run again")
                return 0
            if read_block(h, DCAVE, len(img)) != img:
                sys.exit("FAIL: dcave verify")
            print("dcave complete - run again to hook")
            return 0
        # still no track loaded -> painter idle -> any byte order is safe
        for i, b in enumerate(struct.pack("<I", DCAVE)):
            bw(h, POOL + i, b)
        for i, b in enumerate(struct.pack("<H", INSN_NEW)):
            bw(h, INSN + i, b)
        for i, b in enumerate(struct.pack("<H", ROWINC[2])):
            bw(h, ROWINC[0] + i, b)
        for i, b in enumerate(struct.pack("<I", DCAVE2)):    # PERFORM strip
            bw(h, POOL2 + i, b)
        for i, b in enumerate(struct.pack("<H", INSN2_NEW)):
            bw(h, INSN2 + i, b)
        for i, b in enumerate(struct.pack("<H", ROWINC2[2])):
            bw(h, ROWINC2[0] + i, b)
        if rd32(h, POOL) != DCAVE or rd16(h, INSN) != INSN_NEW \
                or rd16(h, ROWINC[0]) != ROWINC[2] or rd32(h, POOL2) != DCAVE2 \
                or rd16(h, INSN2) != INSN2_NEW or rd16(h, ROWINC2[0]) != ROWINC2[2]:
            sys.exit("FAIL: hook verify - REBOOT before loading a track")
        print("RESULT: PASS - load a track: centre waveform follows "
              "MY SETTINGS [WAVEFORM] (RGB / 3-BAND).")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
