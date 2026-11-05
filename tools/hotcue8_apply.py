#!/usr/bin/env python3
"""hotcue8_apply.py --survey | (install) | --revert -- 8 hot cue pages on
the DECK (tools/hotcue8/, emulator PASS: tools/hotcue8_test.py).
RAM only, reboot = stock. NOT YET RUN ON HARDWARE.

On the PERFORM screen, tapping HOT CUE again pages the three hot cue
pads A-C -> D-F -> G-H (rekordbox hot cues; the deck already loads all
eight). REC is blocked and faded on pages 2-3 so a recording can never
overwrite A-C on the stick. New track = back to A-C.

  --survey   READ-ONLY: code + state area must read zero on THIS deck;
             writes build/hotcue8/survey_ok.<base> on success
  (no flag)  chunked code write (resumable, foreign-data check), then a
             fresh run arms: hc_prev = live flush hook (chains the phase
             meter / PERFORM pads), LW self-test, LW hook (atomic)
  --revert   hook back to the value it chained (LW, verified)
Order: PERFORM pads first (they hold the tab code that calls us), then
the phase meter (optional), then this.
"""
import os
import struct
import subprocess
import sys

sys.path[:0] = [".", "tools"]
HOOK = 0x08C92E20
OUT = "build/hotcue8"


def build():
    import perform_pads as PP
    import mysettings as MS
    env = dict(os.environ, HC_BASE=hex(PP.HC_BASE), HCPAGE=hex(PP.HCPAGE),
               PMODE=hex(PP.PST + 2), MSOFF=hex(MS.MSOFF))
    import shbuild
    shbuild.build("tools/hotcue8/build.sh", [f"{OUT}/hc.bin", f"{OUT}/hc.syms"], env)
    s = {}
    for line in open(f"{OUT}/hc.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return open(f"{OUT}/hc.bin", "rb").read(), s


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    img, s = build()
    base = s["hc_table"]
    end = s.get("end", s["hc_count"] + 4)
    ok_file = os.path.expanduser(f"~/.xdj700_hc_survey_ok.{base:08x}")  # never in the app
    import hid
    import perform_pads as PP
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, bw, read_block, send, CHUNK_B

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    if arg not in ("--survey", "--revert", "--precheck") and not os.path.exists(ok_file):
        sys.exit(f"REFUSED: run --survey first for {base:#x}")
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        live = rd32(h, HOOK)
        entry = s["hc_entry"]
        print(f"flush hook = {live:#x} (ours {entry:#x})")
        if arg == "--precheck":
            arg = ""                      # write step reads the whole area first
        if arg == "--survey":
            blk = read_block(h, base, end - base)
            nz = [i for i, b in enumerate(blk) if b]
            if nz:
                sys.exit(f"SURVEY FAIL: {len(nz)} nonzero bytes, first at "
                         f"{base + nz[0]:#x} - nothing written")
            os.makedirs(OUT, exist_ok=True)
            open(ok_file, "w").write("zero\n")
            print(f"SURVEY OK: {base:#x}..{end:#x} all zero")
            return 0
        if arg == "--revert":
            if live != entry:
                print("not armed - nothing to do")
                return 0
            prev = rd32(h, s["hc_prev"])
            lw(h, HOOK, prev)
            if rd32(h, HOOK) != prev:
                sys.exit("FAIL: hook not restored - REBOOT the deck")
            print(f"RESULT: reverted (hook = {prev:#x})")
            return 0
        if live == entry:
            print("RESULT: PASS (already armed)")
            return 0
        if not (0x08000000 <= live < 0x10000000):
            sys.exit(f"FAIL: unexpected flush hook {live:#x} - nothing written")
        if rd32(h, PP.PC0) == 0:
            sys.exit("REFUSED: PERFORM pads not installed (install them first)")
        want = bytearray(img) + bytes(max(0, end - base - len(img)))  # + zero state
        po = s["hc_prev"] - base
        want[po:po + 4] = struct.pack("<I", live)
        import fastload
        left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                       allow_live=range(s["hc_prev"], s["hc_prev"] + 4))
        if left:
            print(f"RESULT: PARTIAL ({left} B left) - run again")
            return 0
        if fastload.arm_later():
            print("code complete - run again to arm (fresh short session)")
            return 0
        scratch = s["hc_draws"]
        for val in (0x12345678, 0):
            lw(h, scratch, val)
            if rd32(h, scratch) != val:
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        lw(h, HOOK, entry)
        if rd32(h, HOOK) != entry:
            lw(h, HOOK, live)
            sys.exit("FAIL: hook verify - restored; REBOOT before playing")
        print("RESULT: PASS - PERFORM > swipe left/right along the HOT CUE tab to page A-C / D-F / G-H")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
