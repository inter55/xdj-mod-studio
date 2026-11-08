#!/usr/bin/env python3
"""phasemeter_apply.py --survey | (install) | --revert -- Type 1 phase meter
on the DECK (tools/phasemeter/, emulator PASS: tools/phasemeter_test.py).
RAM only, reboot = stock. NOT YET RUN ON HARDWARE.

Build for the deck address first:  PM_BASE=0x........ sh tools/phasemeter/build.sh
  --survey   READ-ONLY: the whole cave (code + 20 KB state) must read zero
             on THIS deck; writes build/phasemeter/survey_ok.<base> on success
             (deck RAM differs from the emulator: always survey, CLAUDE.md §38)
  (no flag)  chunked code write (resumable, foreign-data check), then a
             fresh run arms: pm_prev = live hook value (chains perform_pads
             if installed), pm_mode = 1, LW self-test, LW hook (atomic: the
             flush pool is read every frame)
  --revert   hook back to the value it chained (LW, verified)
Order with perform_pads: install the pads FIRST (their installer only
accepts a stock pool), the phase meter second.
"""
import os
import struct
import sys

sys.path.insert(0, "tools")
HOOK = 0x08C92E20
STOCK = 0x08E7D278
OUT = "build/phasemeter"


def syms():
    s = {}
    for line in open(f"{OUT}/pm.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    s = syms()
    base = s["pm_entry"]
    img = open(f"{OUT}/pm.bin", "rb").read()
    end = max(s["pm_mode"], s["pm_n"], s["pm_layer"]) + 4
    # REFUSE to touch a deck if any symbol we own falls outside the region
    # this tool reserves and the survey proves zero. pm_dy sat 4 bytes past
    # it until 2026-11-08 (declared after pm_mode, i.e. last in the bss).
    outside = sorted((v, k) for k, v in s.items() if v >= end)
    if outside:
        sys.exit(f"REFUSED: {outside[0][1]} at {outside[0][0]:#x} is outside "
                 f"the cave end {end:#x} -- move it into the state block")
    ok_file = os.path.expanduser(f"~/.xdj700_pm_survey_ok.{base:08x}")  # never in the app
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, bw, read_block, send, CHUNK_B
    import perform_pads as PP
    pads_entry = PP.hooks()[1][2]

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    if not arg and not os.path.exists(ok_file):
        sys.exit(f"REFUSED: run --survey first for {base:#x} (or rebuild with "
                 f"PM_BASE at a surveyed address)")
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        live = rd32(h, HOOK)
        print(f"flush hook = {live:#x} (stock {STOCK:#x}, pads {pads_entry:#x}, "
              f"ours {base:#x})")
        if arg == "--survey":
            blk = read_block(h, base, end - base)
            nz = [i for i, b in enumerate(blk) if b]
            if nz:
                sys.exit(f"SURVEY FAIL: {len(nz)} nonzero bytes, first at "
                         f"{base + nz[0]:#x} - pick another PM_BASE")
            open(ok_file, "w").write("zero\n")
            print(f"SURVEY OK: {base:#x}..{end:#x} all zero")
            return 0
        if arg == "--revert":
            if live != base:
                print("not armed - nothing to do")
                return 0
            prev = rd32(h, s["pm_prev"])
            lw(h, HOOK, prev)
            if rd32(h, HOOK) != prev:
                sys.exit("FAIL: hook not restored - REBOOT the deck")
            print(f"RESULT: reverted (hook = {prev:#x})")
            return 0
        # 2026-10-18: the 8-hot-cue mod also hooks this pool (it chains to
        # whatever was live) -- chaining on top of it is equally safe
        hc_entry = None
        try:
            for line in open("build/hotcue8/hc.syms"):
                a_, _, n_ = line.split()
                if n_ == "_hc_entry":
                    hc_entry = int(a_, 16)
        except OSError:
            pass
        if live not in (STOCK, pads_entry, base, hc_entry):
            sys.exit(f"FAIL: unknown flush hook {live:#x} - nothing written")
        if live == base:
            print("RESULT: PASS (already armed)")
            return 0
        # code + data; pm_prev must chain to what is live right now
        want = bytearray(img)
        po = s["pm_prev"] - base
        want[po:po + 4] = struct.pack("<I", live)
        cur = read_block(h, base, len(want))
        diff = [i for i, (a, b) in enumerate(zip(cur, want)) if a != b]
        if diff:
            bad = [i for i in diff if cur[i] and i not in range(po, po + 4)]
            if bad:
                sys.exit(f"FAIL: cave +{bad[0]:#x} holds foreign data - nothing armed")
            import fastload
            left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                           allow_live=range(base + po, base + po + 4),
                                           cur=cur)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
            if fastload.arm_later():
                print("code complete - run again to arm (fresh short session)")
                return 0
        scratch = s["pm_draws"]                      # our own counter word
        for val in (0x12345678, 0):
            lw(h, scratch, val)
            if rd32(h, scratch) != val:
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        bw(h, s["pm_mode"], 1)
        lw(h, HOOK, base)
        if rd32(h, HOOK) != base:
            lw(h, HOOK, live)
            sys.exit("FAIL: hook verify - restored; REBOOT before playing")
        print("RESULT: PASS - load a track: Type 1 rulers above the centre "
              "waveform (MODS OFF in MY SETTINGS = stock meter).")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
