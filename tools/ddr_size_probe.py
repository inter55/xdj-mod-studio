#!/usr/bin/env python3
"""ddr_size_probe.py -- READ-ONLY on the audio chip: does the XDJ-700's audio
chip have 32 MB or 64 MB of main memory? (stems step 2, 2026-11-13)

The firmware only ever uses the chip's memory below 0xC2000000 (32 MB). If
the chip really has 64 MB, the top 32 MB is free room for the four stems
(instant stem mutes mixed inside the chip). If it has 32 MB, addresses above
32 MB wrap onto the lower half.

One short USB session. A small cave (build/ddrprobe/dp.bin @0x09800000,
the key-shift one-shot area) runs once on the next screen frame: it reads
16 words at 0xC0000000+off and the same 16 at 0xC2000000+off through the
host port, then unhooks itself. NOTHING is written to the audio chip; the
deck RAM writes are only the cave itself and its flag. Power-cycle = stock.

    .venv/bin/python tools/ddr_size_probe.py

Answers:
  "32 MB": every pair equal (the upper address mirrors the program words)
  "64 MB": the upper words differ from the program words
"""
import struct
import sys
import time

sys.path.insert(0, "tools")
OUT = "build/ddrprobe"
HOOK = 0x08C92E20
FLAG = 0x44445031          # 'DDP1'


def syms():
    s = {}
    for line in open(f"{OUT}/dp.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def main():
    s = syms()
    base = s["dp_entry"]
    img = open(f"{OUT}/dp.bin", "rb").read()

    import hid
    import fastload
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, read_block, send

    def rd32(h, addr):
        return struct.unpack("<I", bytes(br(h, addr, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)
        return rd32(h, addr) == val

    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        live = rd32(h, HOOK)
        if base <= live < base + len(img) + 0x1000:
            sys.exit("REFUSED: the screen hook points into the probe area (a key-shift "
                     "or earlier probe run has not finished) - wait and run again")
        want = bytearray(img)
        po = s["dp_prev"] - base
        want[po:po + 4] = struct.pack("<I", live)
        own = set(range(po, po + 4))
        for k in ("dp_flag", "dp_status", "dp_same", "dp_tries", "dp_hpia"):
            o = s[k] - base
            want[o:o + 4] = b"\0\0\0\0"
            own |= set(range(o, o + 4))
        for k in ("dp_lo", "dp_hi"):
            o = s[k] - base
            want[o:o + 64] = b"\0" * 64
            own |= set(range(o, o + 64))
        cur = read_block(h, base, len(want))
        if any(cur[i] != want[i] for i in range(len(want)) if i not in own):
            # idle area (key-shift / TOUCH FX installer one-shots live here
            # and are finished once the hook is off them): overwrite
            left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                           allow_live=range(base, base + len(want)), cur=cur)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
        for k in ("dp_status", "dp_tries", "dp_same"):
            if not lw(h, s[k], 0):
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        if not lw(h, s["dp_prev"], live) or not lw(h, s["dp_flag"], FLAG):
            sys.exit("FAIL: could not set up the probe - nothing armed")
        q = f"{HOOK:x},{base:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.3)
        t, status, polls = time.time(), 0, 0
        while time.time() - t < 15 or polls < 80:
            status = rd32(h, s["dp_status"])
            polls += 1
            if status:
                break
            time.sleep(0.3)
        hook = rd32(h, HOOK)
        if hook == base:
            lw(h, s["dp_flag"], 0)
            lw(h, HOOK, live)
            sys.exit("FAIL: probe did not run - unhooked again, nothing changed")
        if status != 1:
            sys.exit(f"FAIL: status {status} (2 = host port busy the whole time)")
        raw = read_block(h, s["dp_lo"], 128)
        w = [struct.unpack_from("<I", bytes(raw), i)[0] for i in range(0, 128, 4)]
        lo, hi = w[:16], w[16:]
        same = sum(a == b for a, b in zip(lo, hi))
        print("  off      0xC0000000+off  0xC2000000+off")
        for k in range(16):
            print(f"  {k * 0x4000 + 0x100:06x}   {lo[k]:08x}        {hi[k]:08x}"
                  f"{'   same' if lo[k] == hi[k] else ''}")
        nontriv = sum(1 for x in lo if x not in (0, 0xFFFFFFFF))
        print(f"  {same}/16 equal, {nontriv}/16 lower words are real program words")
        if nontriv < 8:
            print("RESULT: UNCLEAR - the lower words are not program words")
            return 1
        if same == 16:
            print("RESULT: 32 MB - the upper half mirrors the lower one (no free room up there)")
        elif same == 0:
            print("RESULT: 64 MB - the upper 32 MB is separate memory (free room for stems)")
        else:
            print("RESULT: UNCLEAR - some pairs equal, some not")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
