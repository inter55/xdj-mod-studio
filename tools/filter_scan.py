#!/usr/bin/env python3
"""filter_scan.py -- map the XDJ-1000's browse FILTER code (1.1 track filter).

The 700 has no filter; the 1000 MK2 does, and the two decks share the browse
module and the BPM-range data layer, so the filter is a port, not a new
feature. This finds the `DISPCMD FILTER ...` log tags in the 1000's image and
resolves which routine emits each one, so the three behaviours to mirror
(category state, parameter edit, apply) can be read off concretely.

The image is a container of LZSS blocks: [u32 len][stream][u16 sum], where the
sum is a plain byte sum over [hdr, hdr+4+len) -- the same framing the boot stub
validates. Strings survive compression as literals, but their addresses only
exist after decoding, so --decode works on the decoded image.

    filter_scan.py IMAGE [--decode] [--md OUT.md]

Base addresses are found, not assumed: the code and data windows differ per
image, so the tool takes the most common (pointer - string offset) value.
"""
import argparse
import os
import re
import struct
import sys

TAG = re.compile(rb"DISPCMD FILTER[A-Z0-9 _<>]*")


def decode_blocks(path, limit=None):
    """Decode the container's LZSS blocks; returns (bytes, [(hdr, len, out)])."""
    sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), "."]
    import numpy as np
    import lzss_boot as L

    d = open(path, "rb").read()
    a = np.frombuffer(d[:len(d) // 4 * 4], dtype=np.uint8).astype(np.int64)
    c = np.concatenate(([0], np.cumsum(a)))
    n = len(d)
    ln = np.frombuffer(d[:n // 4 * 4], dtype="<u4")
    pos = np.arange(len(ln)) * 4
    end = pos + 4 + ln
    ok = (ln > 1024) & (end + 2 <= n)
    idx = np.flatnonzero(ok)
    e = end[idx]
    got = ((c[e] - c[pos[idx]]) & 0xFFFF).astype(np.uint32)
    want = (a[e] | (a[e + 1] << 8)).astype(np.uint32)
    out, blocks, last = bytearray(), [], -1
    for i in idx[np.flatnonzero(got == want)]:
        h, length = int(pos[i]), int(ln[i])
        if h <= last:
            continue
        if limit and len(blocks) >= limit:
            break
        try:
            dec = L.decode(d[h + 4:h + 4 + length])
        except Exception:
            continue
        blocks.append((h, length, len(dec)))
        out += dec
        last = h + 4 + length
    return bytes(out), blocks


def tags(data):
    return [(m.start(), data[m.start():m.start() + 64].split(b"\0")[0]
             .decode("latin-1")) for m in TAG.finditer(data)]


def guess_base(data, off):
    """Most common pointer-minus-offset value: the window the image uses."""
    from collections import Counter
    c = Counter()
    for i in range(0, len(data) - 4, 2):
        w = struct.unpack_from("<I", data, i)[0]
        v = w - off
        if 0x08000000 <= v < 0x90000000 and v % 0x10000 == 0:
            c[v] += 1
    return c.most_common(1)[0][0] if c else None


def pointer_sites(data, addr):
    pat = struct.pack("<I", addr)
    return [i for i in range(0, len(data) - 4, 4) if data[i:i + 4] == pat]


def literal_loads(data, lit, reach=4096):
    hits = []
    for a in range(max(0, lit - reach) & ~1, lit, 2):
        v = data[a] | (data[a + 1] << 8)
        if (v & 0xF000) != 0xD000:
            continue
        if (((a + 4) & ~3) + (v & 0xFF) * 4) == lit:
            hits.append(a)
    return hits


def func_start(data, addr):
    i = addr & ~1
    while i > 4:
        if data[i:i + 4] == b"\x0b\x00\x09\x00":
            j = (i + 8 + 3) & ~3
            while data[j:j + 2] == b"\x09\x00":
                j += 2
            return j
        i -= 2
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--decode", action="store_true")
    ap.add_argument("--base", type=lambda x: int(x, 16), default=None,
                    help="pointer window for this image, e.g. 0x08000000")
    ap.add_argument("--md")
    a = ap.parse_args()
    if a.decode:
        data, blocks = decode_blocks(a.image)
        print("%s: %d blocks decoded, %d bytes"
              % (os.path.basename(a.image), len(blocks), len(data)))
    else:
        data, blocks = open(a.image, "rb").read(), []
    rows = [{"off": off, "text": text} for off, text in tags(data)]
    print("%d DISPCMD FILTER tags" % len(rows))
    if not a.base:
        print("pointer window: not established for this image -- showing file "
              "offsets; pass --base once the load address is known")
    for r in rows:
        if not a.base:
            print("  file +%08X  %s" % (r["off"], r["text"].strip()))
            continue
        r["addr"] = a.base + r["off"]
        r["sites"] = pointer_sites(data, r["addr"])
        r["loads"] = sorted({x for s_ in r["sites"] for x in literal_loads(data, s_)})
        r["funcs"] = sorted({func_start(data, x) for x in r["loads"]
                             if func_start(data, x)})
        print("  %08X %-46s ptr=%-2d loads=%-2d routine=%s"
              % (r["addr"], r["text"][:46], len(r["sites"]), len(r["loads"]),
                 ", ".join("%08X" % (a.base + f) for f in r["funcs"]) or "?"))
    if a.md:
        with open(a.md, "w") as f:
            f.write("# Track filter on the XDJ-700 (1.1, in progress)\n\n")
            f.write("Generated by `tools/filter_scan.py %s`: %d tags.\n\n"
                    % (os.path.basename(a.image), len(rows)))
            f.write("pointer window: %s\n\n```\n"
                    % ("%08X" % a.base if a.base else "not established"))
            for r in rows:
                if a.base:
                    f.write("%08X  %s\n" % (a.base + r["off"], r["text"].strip()))
                else:
                    f.write("file +%08X  %s\n" % (r["off"], r["text"].strip()))
            f.write("```\n")
        print("wrote " + a.md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
