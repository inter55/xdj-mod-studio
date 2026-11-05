#!/usr/bin/env python3
"""perform_pads_apply.py [--revert] [--hide-ks] [--hide-stems] --
PERFORM-screen BEAT JUMP pads + KEY area on the DECK
(tools/perform_pads.py, emulator PASS). RAM only, reboot = stock.

Both hook pools are read CONSTANTLY (touch: every panel frame; draw: every
screen flush), so they must change in ONE 32-bit store: this tool writes them
with the shell's LW command (handler = single mov.l, never used on hardware
before). Run 1..n: code bytes (chunked, resumable, foreign-data check).
Final run: LW self-test on a scratch word inside our own cave (write, read
back, restore) -- only if that is exact are the hooks armed, each verified.
Gate: not on the PERFORM screen while installing (mode word != 7).
--revert: hooks back to stock FIRST (LW, verified), REV zone byte restored.
--hide-ks / --hide-stems: install only the chosen tabs (the KEY SHIFT /
STEMS tab is neither drawn nor touchable); the byte is updated on every
run, including an already-armed install.
"""
import argparse
import struct
import sys

sys.path.insert(0, "tools")
import hid
from xdj700_usb_shell import PIONEER_VID, open_deck, drain
from rgb_overview_apply import br, bw, read_block, send, CHUNK_B
import perform_pads as PP

SCRATCH = PP.PEND - 4                # beyond the label bitmap, inside the cave
STATE = (PP.PST, PP.PST + 0x40)      # runtime state: differs once armed


def rd32(h, a):
    return struct.unpack("<I", bytes(br(h, a, 4)))[0]


def lw(h, addr, val):
    assert addr % 4 == 0
    q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
    h.write(q + b"\x00" * (26 - len(q)))
    drain(h, 0.5)


def image(bits=0):
    img = bytearray(PP.PEND - PP.PC0)
    for a, b in PP.blobs(bits):
        img[a - PP.PC0:a - PP.PC0 + len(b)] = b
    return bytes(img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--hide-ks", action="store_true",
                    help="hide the KEY SHIFT tab (live key shift not installed)")
    ap.add_argument("--hide-stems", action="store_true",
                    help="hide the STEMS tab (live stems not installed)")
    opts = ap.parse_args()
    bits = PP.featbits(opts.hide_ks, opts.hide_stems)
    img = image(bits)
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        live_hooks = [rd32(h, pool) for pool, _, _ in PP.hooks()]
        mode = rd32(h, PP.MODEW)
        print(f"hooks {[hex(v) for v in live_hooks]}, screen mode {mode}")
        for (pool, old, new), v in zip(PP.hooks(), live_hooks):
            if v not in (old, new):
                sys.exit(f"FAIL: hook {pool:#x} = {v:#x}, not stock/ours")
        if opts.revert:
            for pool, old, new in PP.hooks()[::-1]:
                lw(h, pool, old)
                if rd32(h, pool) != old:
                    sys.exit(f"FAIL: {pool:#x} not restored - REBOOT the deck")
            for a, old, new in PP.patches():
                bw(h, a, old)
            print("RESULT: reverted (stock PERFORM screen)")
            return 0
        if mode == 7:
            sys.exit("REFUSED: close the PERFORM screen first.")
        armed = live_hooks == [n for _, _, n in PP.hooks()]
        live = read_block(h, PP.PC0, len(img))
        skip = range(STATE[0] - PP.PC0, STATE[1] - PP.PC0) if armed else range(0)
        diff = [i for i, (a, b) in enumerate(zip(live, img))
                if a != b and i not in skip and PP.PC0 + i != SCRATCH]
        if diff:
            if armed:
                sys.exit("FAIL: armed but code differs - REBOOT the deck")
            bad = [i for i in diff if live[i] and live[i] != img[i]]
            if bad:
                sys.exit(f"FAIL: cave +{bad[0]:#x} holds foreign data "
                         f"({live[bad[0]]:#04x}) - nothing armed")
            import fastload
            left = fastload.install_region(h, PP.PC0, img, fastload.SESSION_BUDGET,
                                           allow_live=range(SCRATCH, SCRATCH + 4),
                                           cur=live)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
            if fastload.arm_later():
                print("code complete - run again to arm (fresh short session)")
                return 0
        # Which tabs are drawn / touchable. The byte is runtime state, so
        # update it even when the code is already armed (reinstall with a
        # different feature pick must not need a power-cycle).
        if br(h, PP.FEATB, 1)[0] != bits:
            bw(h, PP.FEATB, bits)
            if br(h, PP.FEATB, 1)[0] != bits:
                sys.exit("FAIL: feature byte verify - tabs unchanged; REBOOT")
        tabs = [n for k, n in enumerate(("HOT CUE", "BEAT JUMP", "KEY SHIFT",
                                         "STEMS"))
                if k < 2 or not bits & (PP.FB_KEYSHIFT if k == 2
                                        else PP.FB_STEMS)]
        if armed:
            print(f"RESULT: PASS (already installed; tabs: {' / '.join(tabs)})")
            return 0
        # LW self-test on our own scratch word (never used by the hooks)
        for val in (0x12345678, 0):
            lw(h, SCRATCH, val)
            got = rd32(h, SCRATCH)
            if got != val:
                sys.exit(f"FAIL: LW self-test wrote {got:#x}, wanted {val:#x} "
                         f"- hooks NOT armed (deck unchanged)")
        print("LW self-test OK (atomic 32-bit write works)")
        for a, old, new in PP.patches():
            cur = br(h, a, 1)[0]
            if cur not in (old, new):
                sys.exit(f"FAIL: {a:#x} = {cur:#x}, not stock - nothing armed")
            bw(h, a, new)
            if br(h, a, 1)[0] != new:
                sys.exit("FAIL: REV zone verify - nothing armed")
        for pool, old, new in PP.hooks()[::-1]:      # draw first, touch last
            lw(h, pool, new)
            if rd32(h, pool) != new:
                lw(h, pool, old)
                sys.exit(f"FAIL: hook {pool:#x} verify - restored stock; "
                         f"REBOOT before using PERFORM")
        print(f"RESULT: PASS - open PERFORM: {' / '.join(tabs)} tabs.")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
