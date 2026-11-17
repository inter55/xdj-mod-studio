#!/usr/bin/env python3
"""filter_scan.py -- map the XDJ-1000's browse FILTER code (1.1 track filter).

The 700 has no filter; the 1000 MK2 does, and the two decks share the browse
module and the BPM-range data layer, so the filter is a port, not a new
feature. This finds the `DISPCMD FILTER ...` log tags in the 1000's image and
resolves the code that emits each one.

The image is a container of LZSS blocks: [u32 len][stream][u16 sum], where the
sum is a plain byte sum over [hdr, hdr+4+len) -- the same framing the boot stub
validates. Strings survive compression as literals, but their addresses only
exist after decoding, so --decode works on the decoded image.

    filter_scan.py IMAGE [--decode] [--md OUT.md]

Mapping (solved 2026-11-08). The decoded image is flat: file offset F is the
cached address 0x08000000+F. The browse messages, however, are referenced
through the *uncached alias* 0xA8000000+F, which is why a sweep at 0x08... only
(or one that assumes a shifted window) finds nothing. A pointer word is only
believed when a real `mov.l @(disp,PC),Rn` loads it -- the messages sit in one
run 24 bytes apart, so any stray data word would otherwise "explain" 20 of them
at once.
"""
import argparse
import os
import re
import struct
import sys

TAG = re.compile(rb"@Ui_Browse> (?:DISPCMD|RESET)[^\x00]*")
PREFIX = b"@Ui_Browse> DISPCMD"
SHIFT = 0xA0000000            # cached -> uncached alias
EMIT_ID_FN = 0x09511A38       # log(id, ...); id = (16<<8) + n, built in r4


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
    """(file offset, text) for every DISPCMD/RESET message, prefix included."""
    return [(m.start(), data[m.start():m.start() + 96].split(b"\0")[0]
             .decode("latin-1")) for m in TAG.finditer(data)]


def pointer_sites(data, addr):
    pat = struct.pack("<I", addr)
    return [i for i in range(0, len(data) - 4, 4) if data[i:i + 4] == pat]


def pool_loaders(data, site, reach=0x400):
    """`mov.l @(disp,PC),Rn` instructions whose pool word is at `site`."""
    out = []
    for a in range(max(0, site - reach) & ~1, site, 2):
        v = data[a] | (data[a + 1] << 8)
        if (v & 0xF000) != 0xD000:
            continue
        if (((a + 4) & ~3) + (v & 0xFF) * 4) == site:
            out.append(a)
    return out


def emit_id(data, load):
    """The DISPCMD id built in r4 just before the message is pushed.

    The emitter does `mov #16,r4; shll8 r4; ... add #n,r4` and passes r4 to
    the log function. Returns None when the pattern is absent.
    """
    add = None
    for a in range(load, max(0, load - 0x80), -2):
        w = data[a] | (data[a + 1] << 8)
        if (w & 0xF000) == 0x7000 and ((w >> 8) & 0xF) == 4:
            add = w & 0xFF
            break
    if add is None:
        return None
    seen_sh = seen_mov = False
    for a in range(load, max(0, load - 0x80), -2):
        w = data[a] | (data[a + 1] << 8)
        if w == 0x4418:
            seen_sh = True
        if w == 0xE410:
            seen_mov = True
    return (0x1000 | add) if (seen_sh and seen_mov) else None


def resolve(data, off, base=None):
    """Verified pointer word + load site for one message, or None."""
    if base is not None:
        cands = [base + off]
    else:
        cands = [0x08000000 + off, (SHIFT + 0x08000000) + off]
    for addr in cands:
        for site in pointer_sites(data, addr):
            for load in pool_loaders(data, site):
                return {"addr": addr, "site": site, "load": load}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--decode", action="store_true")
    ap.add_argument("--base", type=lambda x: int(x, 16), default=None,
                    help="force the pointer window instead of the alias test")
    ap.add_argument("--md")
    a = ap.parse_args()
    if a.decode:
        data, blocks = decode_blocks(a.image)
        print("%s: %d blocks decoded, %d bytes"
              % (os.path.basename(a.image), len(blocks), len(data)))
    else:
        data, blocks = open(a.image, "rb").read(), []
    rows = [{"off": off, "text": text} for off, text in tags(data)]
    print("%d browse DISPCMD messages" % sum(1 for r in rows
                                             if PREFIX in r["text"].encode()))
    print("pointer window: %s"
          % ("forced %08X" % a.base if a.base else
             "auto (cached 0x08... or uncached 0xA8... alias)"))
    n_ok = 0
    for r in rows:
        m = resolve(data, r["off"], a.base)
        r["map"] = m
        if not m:
            print("  file +%08X  UNRESOLVED  %s" % (r["off"], r["text"][:60]))
            continue
        n_ok += 1
        r["id"] = emit_id(data, m["load"])
        print("  %08X  pool %08X  load %08X  id %s  %s"
              % (m["addr"], 0x08000000 + m["site"], 0x08000000 + m["load"],
                 ("%04X" % r["id"]) if r["id"] else "----",
                 r["text"][:60]))
    print("%d/%d messages resolved to a real pool load" % (n_ok, len(rows)))
    if a.md:
        with open(a.md, "w") as f:
            f.write("# Track filter on the XDJ-700 (1.1, in progress)\n\n")
            f.write("Generated by `tools/filter_scan.py %s`: %d messages, "
                    "%d resolved.\n\n```\n"
                    % (os.path.basename(a.image), len(rows), n_ok))
            for r in rows:
                m = r["map"]
                if m:
                    f.write("%08X  pool %08X  load %08X  id %s  %s\n"
                            % (m["addr"], 0x08000000 + m["site"],
                               0x08000000 + m["load"],
                               ("%04X" % r["id"]) if r["id"] else "----",
                               r["text"].strip()))
                else:
                    f.write("file +%08X  UNRESOLVED  %s\n"
                            % (r["off"], r["text"].strip()))
            f.write("```\n")
        print("wrote " + a.md)
    return 0 if n_ok else 1


if __name__ == "__main__":
    sys.exit(main())
