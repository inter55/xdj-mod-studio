#!/usr/bin/env python3
"""linkrgb_overview_dump.py -- READ-ONLY: how the overview the deck keeps
for the loaded track (PREVIEW 0x0BA3BCCC, 400 x {height, whiteness}) differs
from the analysis buffer's own PWAV (ctx+0xDB0, 1 B/col h | w<<5) when the
buffer IS the loaded track (length word matches) but not all columns match.
Prints match counts for heights / whiteness / both, any column shift that
fits better, a height scale check, and the first 40 columns side by side.
Writes nothing.
"""
import struct
import sys

sys.path[:0] = [".", "tools"]
import rgb_overview as RO
import rgb_detail as RD


def main():
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    import hid
    import fastload
    from rgb_overview_apply import send

    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if len(c) != 1:
        sys.exit(f"found {len(c)} shell interface(s) - plug in exactly one deck")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        prev = fastload.BR32(h, RD.PREVIEW, 800)
        ctx = struct.unpack("<I", fastload.BR32(h, RO.CTX_TAB, 4))[0]
        hdr = fastload.BR32(h, ctx + 0xD9C, 20)
        raw = fastload.BR32(h, ctx + 0xD9C + 20, 400)
        tl, pos = struct.unpack("<II", fastload.BR32(h, 0x0C0E6D18, 8))[::-1]
    finally:
        h.close()
    ph = [prev[2 * i] for i in range(400)]
    pw = [prev[2 * i + 1] for i in range(400)]
    ch = [b & 0x1F for b in raw]
    cw = [b >> 5 for b in raw]
    print(f"buffer PWAV header: {hdr.hex(' ')}")
    print(f"player length word {tl} ({tl / 150:.1f} s), position {pos} ({pos / 150:.1f} s)")
    print(f"PREVIEW height range {min(ph)}..{max(ph)}, whiteness values {sorted(set(pw))}")
    print(f"buffer  height range {min(ch)}..{max(ch)}, whiteness values {sorted(set(cw))}")
    eh = sum(a == b for a, b in zip(ph, ch))
    ew = sum(a == b for a, b in zip(pw, cw))
    eb = sum(a == b and x == y for a, b, x, y in zip(ph, ch, pw, cw))
    print(f"equal heights {eh}/400, equal whiteness {ew}/400, both {eb}/400")
    best = max(((sum(ph[i] == ch[i + s] for i in range(400) if 0 <= i + s < 400), s)
                for s in range(-20, 21)))
    print(f"best column shift for heights: {best[1]:+d} -> {best[0]}/400")
    diffs = [a - b for a, b in zip(ph, ch)]
    from collections import Counter
    print(f"height differences (PREVIEW - buffer), most common: {Counter(diffs).most_common(6)}")
    played = [i for i in range(400) if pw[i] != cw[i]]
    if played:
        print(f"whiteness differs in columns {played[0]}..{played[-1]} "
              f"({len(played)} cols); PREVIEW w there: {Counter(pw[i] for i in played).most_common(3)}, "
              f"buffer w there: {Counter(cw[i] for i in played).most_common(3)}")
    print("col  PREVIEW(h,w)  buffer(h,w)")
    for i in range(0, 400, 10):
        print(f"{i:3d}  ({ph[i]:2d},{pw[i]})        ({ch[i]:2d},{cw[i]})")


if __name__ == "__main__":
    sys.exit(main())
