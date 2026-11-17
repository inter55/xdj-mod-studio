#!/usr/bin/env python3
"""hotcue_probe.py -- READ-ONLY, one session: how THIS deck stores the loaded
track's hot cues.

    .venv/bin/python tools/hotcue_probe.py             # full dump + counters
    .venv/bin/python tools/hotcue_probe.py --check     # is the deck on THIS build?
    .venv/bin/python tools/hotcue_probe.py --selftest  # no deck: proves the check

Reads (RAM window only, no writes): the three live cue slots (0x0B6C4118,
0x40 each), the two 8-record pad models (0x0BA28800, 0x0BA2A52C, 0x38 each),
and -- only when the deck really runs this repo's module -- its counters.
Everything is also appended to docs/deck_logs/hotcue_probe_<date>.log.

Why the build check: the counters are read through the offsets in
build/hotcue8/hc.syms, and those belong to the image this tree just built. A
deck still on 1.0.9 (whose hc.bin writes cue numbers 4-8 into the pad slots)
has different code there, so every offset would be wrong and the numbers would
look real and mean nothing -- the trap stems_probe hit on 2026-11-03. So the
module is read back off the deck and compared byte for byte with
build/hotcue8/hc.bin BEFORE a single symbol offset is trusted.

Why: hc.c decided "pad has a cue" from the slot's state word == 6. That value
was only ever seen in the emulator with test cues written with status 4; real
rekordbox cues carry status 0 (2026-11-10, user report: A-H view empty).
"""
import hashlib
import builtins
import os
import struct
import sys
import time

sys.path[:0] = [".", "tools"]

SLOTS, MODELS, MS = 0x0B6C4118, (("A", 0x0BA28800), ("B", 0x0BA2A52C)), 0x38
IMG = os.path.join("build", "hotcue8", "hc.bin")
SYMS = os.path.join("build", "hotcue8", "hc.syms")
COUNTERS = ("hc_caps", "hc_count", "hc_view", "hc_sets", "hc_presses",
            "hc_swallowed", "hc_restores")

os.makedirs("docs/deck_logs", exist_ok=True)
_LOG = open(time.strftime("docs/deck_logs/hotcue_probe_%Y-%m-%d.log"), "a")
_LOG.write(time.strftime("\n==== %H:%M:%S ") + " ".join(sys.argv[1:]) + "\n")


def print(*a, **k):                                 # noqa: A001
    builtins.print(*a, **k)
    builtins.print(*a, file=_LOG)
    _LOG.flush()


def compare_image(img, read, chunk=0x100):
    """(equal, first differing offset or None) for `img` against a device.

    `read(off, n)` returns n bytes from the deck at `off`. Pure, so
    --selftest proves the comparison itself without hardware."""
    off = 0
    while off < len(img):
        n = min(chunk, len(img) - off)
        got = bytes(read(off, n))
        if len(got) < n:                   # short read: cannot vouch for the rest
            return False, off + len(got)
        for i in range(n):
            if got[i] != img[off + i]:
                return False, off + i
        off += n
    return True, None


def local_build():
    """(image, symbols, short hash) of the module this tree built."""
    if not (os.path.exists(IMG) and os.path.exists(SYMS)):
        return None, None, None
    img = open(IMG, "rb").read()
    syms, data = {}, []
    for line in open(SYMS):
        a, _, n = line.split()
        syms[n.lstrip("_")] = int(a, 16)
        if _ in "Dd":                      # writable data: chain pointers the
            data.append(int(a, 16))        # installer fills in, runtime state
    base = syms.get("hc_table", 0)
    if data and base:                      # compare the code + constants only
        img = img[:max(0, min(data) - base)]
    return img, syms, hashlib.sha256(img).hexdigest()[:16]


def selftest():
    """The comparison must pass on equal bytes, and point at the first bad one."""
    img = bytes(range(256)) * 8
    ok, off = compare_image(img, lambda o, n: img[o:o + n])
    def corrupt(o, n):                 # flip one byte, whatever the chunk offset
        b = bytearray(img[o:o + n])
        if o <= 0x120 < o + n:
            b[0x120 - o] ^= 0xFF
        return bytes(b)
    bad, off2 = compare_image(img, corrupt)
    short, off3 = compare_image(img, lambda o, n: img[o:o + n][:-1])
    good = (ok and off is None and bad is False and off2 == 0x120
            and short is False and off3 == 0xFF)
    print(f"selftest: {'PASS' if good else 'FAIL'} "
          f"(equal={ok} off={off} differ={bad}@{off2} short={short}@{off3})")
    return 0 if good else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()

    import hid                                        # only when a deck is used
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import read_block, send

    check_only = "--check" in sys.argv
    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if not c:
        print("no deck found on USB")
        return 1
    img, syms, digest = local_build()
    if img is None:
        print(f"cannot check the build: {IMG} or {SYMS} missing "
              "(build it: sh tools/hotcue8/build.sh)")
        return 1
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        base = syms["hc_table"]
        same, off = compare_image(img, lambda o, n: read_block(h, base + o, n))
        print(f"hot-cue module on the deck vs {os.path.basename(IMG)} "
              f"({len(img)} B, sha256 {digest}):")
        if same:
            print(f"  MATCH at {base:#010x}: the symbol map is the right one, "
                  "the counters are valid")
        else:
            print(f"  DIFFER at +{off:#x} of {len(img)} B: the deck runs ANOTHER "
                  "build (1.0.9 predates the D-H fix) -- reinstall from this "
                  "folder; the counters are NOT printed")
        if check_only:
            return 0 if same else 2

        b = bytes(read_block(h, SLOTS, 0xC0))
        print("live slots (pad A, B, C):")
        for k in range(3):
            s = b[k * 0x40:(k + 1) * 0x40]
            st, num, ms, loop, pos = struct.unpack_from("<HHIII", s, 8)
            print(f"  {'ABC'[k]}: state {st:#06x} number {num:#06x} ms {ms} "
                  f"loop {loop:#x} pos150 {pos}   raw +0..+0x20: {s[:0x20].hex()}")
        for name, mbase in MODELS:
            b = bytes(read_block(h, mbase, 8 * MS))
            print(f"pad model {name}:")
            for k in range(8):
                st, num, ms, loop, pos = struct.unpack_from("<HHIII", b, k * MS)
                print(f"  {'ABCDEFGH'[k]}: state {st:#06x} number {num:#06x} "
                      f"ms {ms} loop {loop:#x} pos150 {pos}")

        if not same:
            print("module counters not read: the offsets belong to another build")
            return 0
        w = lambda n: struct.unpack("<I", bytes(read_block(h, syms[n], 4)))[0]  # noqa: E731
        print("hot-cue module counters:",
              {n: w(n) for n in COUNTERS if n in syms})
    finally:
        h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
