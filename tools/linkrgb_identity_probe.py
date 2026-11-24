#!/usr/bin/env python3
"""linkrgb_identity_probe.py BPM -- READ-ONLY: find a reliable "this analysis
buffer is the LOADED track" check (length alone collides: on one 1396-track
library ~110 pairs of different songs share an exact PWV5 length).

Run on a deck with a track loaded from its own stick, the colours correct,
passing the BPM the deck shows (e.g. 122.0). Prints:
  1. the overview the deck draws for the loaded track (PREVIEW 0x0BA3BCCC):
     where else it appears -- inside the USB ANLZ context, or in a section
     the context points at (tag PWAV) -- with offsets;
  2. every analysis section the context points at (tag, length);
  3. words near the loaded track's length word (0x0C0E6D1C) that equal the
     shown BPM in the usual encodings (x100, x10).
Writes nothing.
"""
import struct
import sys

sys.path[:0] = [".", "tools"]
import rgb_overview as RO
import rgb_detail as RD

TAGS = (b"PWAV", b"PWV2", b"PWV3", b"PWV4", b"PWV5", b"PQTZ", b"PQT2", b"PPTH",
        b"PCOB", b"PCO2", b"PVBR", b"PKEY", b"PMAI", b"PCPT", b"PCP2")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    bpm = float(sys.argv[1])
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    import hid
    import fastload
    from rgb_overview_apply import send

    def rd(h, a, n):
        a0 = a & ~3
        n4 = ((a + n - a0) + 3) & ~3
        return fastload.BR32(h, a0, n4)[a - a0:a - a0 + n]

    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if len(c) != 1:
        sys.exit(f"found {len(c)} shell interface(s) - plug in exactly one deck")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        prev = rd(h, RD.PREVIEW, 4 * RD.PREVIEW_WORDS)
        print(f"PREVIEW {RD.PREVIEW:#x}: first 32 B {prev[:32].hex(' ')}")
        nz = sum(1 for b in prev if b)
        print(f"  {nz} of {len(prev)} bytes non-zero")
        ctx = struct.unpack("<I", rd(h, RO.CTX_TAB, 4))[0]
        print(f"USB ANLZ context = {ctx:#x}")
        if not (0x08000000 <= ctx < 0x10000000):
            sys.exit("no context: load a track from THIS deck's stick first")
        span = 0x2000
        blob = rd(h, ctx, span)
        key = prev[:32]
        hits = [i for i in range(len(blob) - 31) if blob[i:i + 32] == key]
        print(f"  PREVIEW's first 32 B inside the context: "
              f"{[hex(x) for x in hits] or 'not found'}")
        # the same overview as 1 byte per column (PWAV style: height | whiteness<<5)
        raw = bytes((prev[2 * i] & 0x1F) | ((prev[2 * i + 1] & 7) << 5)
                    for i in range(len(prev) // 2))
        big = rd(h, ctx, 0x4000)
        for name, k in (("1 B/col (h | w<<5)", raw[:16]), ("heights only", bytes(prev[0:32:2]))):
            hit = [i for i in range(len(big) - 15) if big[i:i + 16] == k]
            print(f"  overview as {name} inside the context: {[hex(x) for x in hit[:4]] or 'not found'}")
        full = big[0xDB0:0xDB0 + 400]
        same = sum(1 for a, b in zip(full, raw) if a == b)
        print(f"  FULL CHECK (what the fix uses): {same} of 400 overview columns "
              f"equal -> {'buffer = LOADED track' if same == 400 else 'buffer = ANOTHER track'}")
        bg = rd(h, ctx + 0xD7C, 48)
        print(f"  beat grid at ctx+0xD7C: {bg.hex(' ')}")
        print("sections the context points at:")
        seen = set()
        for o in range(0, span, 4):
            p = struct.unpack_from("<I", blob, o)[0]
            for base in (0, 0xA0000000):
                q = p - base if base and p >= base else p
                if 0x08000000 <= q < 0x10000000 and q % 4 == 0 and q not in seen:
                    hd = rd(h, q, 12)
                    if hd[:4] in TAGS:
                        seen.add(q)
                        hl, tl = struct.unpack(">II", hd[4:12])
                        line = f"  ctx+{o:#06x} -> {q:#x} {hd[:4].decode()} hdr {hl} len {tl}"
                        if hd[:4] in (b"PWAV", b"PWV2"):
                            body = rd(h, q + hl, min(tl - hl, len(prev)))
                            same = body == prev[:len(body)]
                            sub = [i for i in range(0, len(prev) - 15)
                                   if prev[i:i + 16] == body[:16]]
                            line += (f"  body == PREVIEW" if same else
                                     f"  body[:16] in PREVIEW at {sub[:4]}" if sub else
                                     "  differs from PREVIEW")
                        print(line)
        if bpm <= 0:                    # no BPM given: skip that search
            return 0
        lo, hi = 0x0C0E6C00, 0x0C0E7000
        st = rd(h, lo, hi - lo)
        want = {round(bpm * 100), round(bpm * 10)}
        print(f"words near the length word equal to {bpm} BPM (x100 / x10):")
        found = False
        for o in range(0, len(st) - 1, 2):
            v16 = struct.unpack_from("<H", st, o)[0]
            v32 = struct.unpack_from("<I", st, o & ~3)[0] if o % 4 == 0 else None
            if v16 in want or v32 in want:
                found = True
                print(f"  {lo + o:#x}: u16 {v16}" + (f", u32 {v32}" if v32 is not None else ""))
        if not found:
            print("  none")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
