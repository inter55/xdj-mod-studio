#!/usr/bin/env python3
"""linkrgb_probe.py -- READ-ONLY look at where the colour slots came from.

Prints, from the deck on the cable:
  * the RGB snapshot slots (PWV4 / PWV5 / PWV6): tag, length, and the track
    length PWV5 implies (150 entries/s), plus the first bytes;
  * the USB ANLZ context [0x09826BE4] the stock snapshot copies from: its
    pointer, and every extra section in its index (tag, length, duration);
  * whether each slot is byte-identical to the context's section (= it was
    copied from the context, not fetched over Link);
  * linkrgb's new-track decision counters.
Writes nothing.
"""
import struct
import sys

sys.path[:0] = [".", "tools"]
import linkrgb as L
import rgb_overview as RO


def main():
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    import hid
    import fastload
    from rgb_overview_apply import br, send

    def rd(h, a, n):
        n4 = (n + 3) & ~3
        return fastload.BR32(h, a & ~3, n4)[:n]

    def u32(h, a):
        return struct.unpack("<I", rd(h, a, 4))[0]

    def be32(b, o):
        return struct.unpack(">I", b[o:o + 4])[0]

    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if len(c) != 1:
        sys.exit(f"found {len(c)} shell interface(s) - plug in exactly one deck")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        print("slots:")
        slots = {}
        for name, a, tag in (("PWV4", RO.SLOT4, b"PWV4"), ("PWV5", RO.SLOT5, b"PWV5"),
                             ("PWV6", RO.SLOT6, b"PWV6")):
            head = rd(h, a, 16)
            ok = head[:4] == tag
            ln = be32(head, 8) if ok else 0
            extra = ""
            if ok and name == "PWV5":
                hl = be32(head, 4)
                extra = f"  -> {((ln - hl) // 2) / 150:.1f} s of track"
            print(f"  {name} @{a:#x}: tag {head[:4]!r} {'valid' if ok else 'INVALID'}"
                  f"{f', {ln} B' if ok else ''}{extra}")
            if ok:
                slots[name] = (a, ln)
        ctx = u32(h, RO.CTX_TAB)
        print(f"USB ANLZ context [{RO.CTX_TAB:#x}] = {ctx:#x}")
        if 0x08000000 <= ctx < 0x10000000 and ctx % 4 == 0:
            idx = ctx + RO.IDX_OFF
            n = u32(h, idx)
            print(f"  extra sections in its index: {n}")
            for i in range(min(n, 32)):
                e = rd(h, idx + 4 + 16 * i, 16)
                off, tag, ln, ptr = struct.unpack("<IIII", e)
                tg = struct.pack("<I", tag)
                extra = ""
                if tg == b"PWV5" and 0x08000000 <= ptr < 0x10000000:
                    hd = rd(h, ptr, 12)
                    extra = f"  -> {((be32(hd, 8) - be32(hd, 4)) // 2) / 150:.1f} s of track"
                same = ""
                if tg.decode("latin1") in slots and 0x08000000 <= ptr < 0x10000000:
                    a, sl = slots[tg.decode("latin1")]
                    k = min(sl, 256)
                    same = ("  == slot (copied from here)" if rd(h, ptr, k) == rd(h, a, k)
                            else "  != slot")
                print(f"    {tg!r} len {ln} @ {ptr:#x}{extra}{same}")
        else:
            print("  (no valid context)")
        # the loaded track's length / position words (phase meter notes:
        # 0x0C0E6D1C = track length, 0x0C0E6D18 = position, 1/150 s)
        tl, pos = u32(h, 0x0C0E6D1C), u32(h, 0x0C0E6D18)
        print(f"loaded track: length word {tl} (= {tl / 150:.1f} s if 1/150 s), "
              f"position word {pos} (= {pos / 150:.1f} s)")
        v = fastload.BR32(h, L.LV, 0x100)
        g = lambda k: struct.unpack_from("<I", v, L.V[k] - L.LV)[0]
        print(f"linkrgb: Link loads {g('GEN')}, fetched gen {g('DONE')}, "
              f"new-track decisions Link {g('NLINK')} / local {g('NLOCAL')}, bound gen {g('BOUND')}")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
