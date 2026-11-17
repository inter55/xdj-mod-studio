#!/usr/bin/env python3
"""hcpad_apply.py --survey | (install) | --revert -- hot cue pads page by
SWIPE across A B C on the DECK, like the BEAT JUMP row (tools/hcpad/hp.c;
emulator PASS: tools/hcpad_test.py). RAM only, power-cycle = stock.

  --survey   READ-ONLY: the module's area (0x09793000..) must read zero
  (none)     write the module (prev word = the live touch hook), LW
             self-test, then point the touch hook at it (one atomic LW)
  --revert   touch hook back to the value it chained

Needs the 8 hot cue pages installed first (it calls their page functions,
addresses baked in at build: rebuild with tools/hcpad/build.sh after any
hotcue8 rebuild). It steps aside while the TOUCH FX panel is open, so it
may sit in front of or behind TOUCH FX in the touch chain.
"""
import os
import struct
import sys

sys.path.insert(0, "tools")
HOOK_TOUCH = 0x08D5278C
HC_BASE = 0x09790000      # tools/hotcue8 module base
OUT = "build/hcpad"


def syms(path):
    return {l.split()[2].lstrip("_"): int(l.split()[0], 16) for l in open(path)}


def main():
    arg = next((x for x in sys.argv[1:] if x in ("--survey", "--revert")), "")
    s = syms(f"{OUT}/hp.syms")
    base, end = s["hp_touch_entry"], s["end"]
    img = open(f"{OUT}/hp.bin", "rb").read()
    hc = syms("build/hotcue8/hc.syms")
    known = {hc["hc_tentry"]}
    try:
        known.add(syms("build/touchfx/tfx.syms")["tfx_touch_entry"])
    except (OSError, KeyError):
        pass
    ok_file = os.path.expanduser(f"~/.xdj700_hcpad_survey_ok.{base:08x}.{end:08x}")
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, read_block, send

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    if not arg and not os.path.exists(ok_file):
        sys.exit(f"REFUSED: run --survey first for {base:#x}")
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        lt = rd32(h, HOOK_TOUCH)
        print(f"touch hook = {lt:#x} (ours {base:#x})")
        if arg == "--survey":
            blk = read_block(h, base, end - base)
            nz = [i for i, b in enumerate(blk) if b]
            if nz:
                sys.exit(f"SURVEY FAIL: {len(nz)} nonzero bytes, first at "
                         f"{base + nz[0]:#x} - nothing written")
            open(ok_file, "w").write("zero\n")
            print(f"SURVEY OK: {base:#x}..{end:#x} all zero")
            return 0
        if arg == "--revert":
            if lt != base:
                print("not first in the chain - nothing to do")
                return 0
            prev = rd32(h, s["hp_prev_touch"])
            lw(h, HOOK_TOUCH, prev)
            if rd32(h, HOOK_TOUCH) != prev:
                sys.exit("FAIL: touch hook not restored - power-cycle the deck")
            print("RESULT: reverted")
            return 0
        if lt == base:
            print("RESULT: PASS (already armed)")
            return 0
        if lt not in known:
            sys.exit(f"FAIL: touch hook {lt:#x} is not the hot cue pages or TOUCH FX "
                     "(install the 8 hot cue pages first) - nothing written")
        # the page functions must be the build that is on the deck
        hcb = open("build/hotcue8/hc.bin", "rb").read()
        if rd32(h, hc["hc_next"]) != struct.unpack_from("<I", hcb, hc["hc_next"] - HC_BASE)[0]:
            sys.exit("FAIL: the hot cue module on the deck is not this build - nothing written")
        want = bytearray(img) + bytes(end - base - len(img))
        o = s["hp_prev_touch"] - base
        want[o:o + 4] = struct.pack("<I", lt)
        live = list(range(base + o, base + o + 4))
        cur = read_block(h, base, len(want))
        bad = [i for i, (a, b) in enumerate(zip(cur, want)) if a != b and a and base + i not in live]
        if bad:
            sys.exit(f"FAIL: area +{bad[0]:#x} holds foreign data - nothing written")
        if bytes(cur) != bytes(want):
            import fastload
            left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                           allow_live=live, cur=cur)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
        scratch = s["hp_seen"]
        for val in (0x12345678, 0):
            lw(h, scratch, val)
            if rd32(h, scratch) != val:
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        lw(h, HOOK_TOUCH, base)
        if rd32(h, HOOK_TOUCH) != base:
            lw(h, HOOK_TOUCH, lt)
            sys.exit("FAIL: touch hook verify - restored")
        print("RESULT: PASS - PERFORM > HOT CUE: swipe across A B C to page "
              "(left = next), tap or hold a pad to play its cue")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
