#!/usr/bin/env python3
"""dsp_map.py -- EMULATOR ONLY: measure the DSP's DDR occupancy.

The 1.1 question this answers: Echo, Flanger and Reverb each need a delay
line of 9-90 KB, and our own DSP home (0xC005F4C0, shared with key shift)
has only 2,880 B. docs/TOUCHFX_MEMORY_SURVEY.md guessed ~103 KB free at
0xC0045900..0xC005F4C0 from the shape of a pointer table in blk1 -- a
guess it explicitly could not prove without a deck.

A snapshot of the real DSP is just as good for this purpose and needs no
deck: load it, let it play, then read DDR straight out of the chip's
memory (DspBus.dump) and report what is actually non-zero.

    .venv/bin/python tools/dsp_map.py [SNAP] [--secs N] [--quiet]

Prints every non-zero page, the zero runs of 1 KB or more, and a verdict
on the claimed gap. Nothing is written to disk and no firmware changes.

MASTER TEMPO is switched on before the measurement, on purpose: the first
version of this tool did not, so the audio path that fills DDR above our
window was idle and it reported 0xC0062000 as free. With MT on and the FX
engine installed, that block is full of audio samples. A "free" block here
means free on the path TOUCH FX actually runs on (fx.c needs MT on).
"""
import os
import sys

sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402

DDR = 0xC0000000
TOP = 0xC0080000                  # 512 KB is plenty: the image ends < 0xC0070000
OURS = 0xC005F4C0                 # our DSP window (key shift / TOUCH FX)
CLAIM_LO = 0xC0045900             # the survey's claimed free-gap start
PAGE = 0x1000


def dump(d, lo, n):
    return d.bus.dump(lo, n)


def runs(buf, lo, val_zero=True):
    """yield (start, end) of runs of all-zero (or all-non-zero) bytes"""
    out, i, n = [], 0, len(buf)
    cur = None
    for i, b in enumerate(buf):
        z = b == 0
        if (z if val_zero else not z):
            if cur is None:
                cur = i
        elif cur is not None:
            out.append((lo + cur, lo + i))
            cur = None
    if cur is not None:
        out.append((lo + cur, lo + n))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    snap = args[0] if args else "cache/emu_dsp_playing7.pkl"
    secs = 1.0
    for a in sys.argv[1:]:
        if a.startswith("--secs="):
            secs = float(a.split("=", 1)[1])
    quiet = "--quiet" in sys.argv

    m = Machine.load(snap)
    d = m.mem.dev.dspemu
    dev = m.mem.dev
    dev.panel_frame = panl.build_frame(keys=["MasterTempo"])
    m.cpu.run(limit=5_000_000)
    dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=30_000_000)
    steps = int(secs * 100_000_000)
    while steps > 0:
        k = min(steps, 50_000_000)
        m.cpu.run(limit=k)
        steps -= k
    print(f"snapshot {snap}: ran {secs:g}s of DSP time, error={d.error}", flush=True)

    n = TOP - DDR
    buf = dump(d, DDR, n)

    pages = []
    for p in range(0, n, PAGE):
        nz = sum(1 for b in buf[p:p + PAGE] if b)
        pages.append(nz)
    used = [(DDR + i * PAGE, pages[i]) for i in range(len(pages)) if pages[i]]
    print(f"non-zero pages: {len(used)} of {len(pages)} "
          f"({sum(pages)} of {n} bytes non-zero)", flush=True)
    if not quiet:
        print("  page        non-zero bytes")
        for a, c in used:
            print(f"  0x{a:08X}  {c:5d}")

    gaps = [r for r in runs(buf, DDR) if r[1] - r[0] >= 1024]
    gaps.sort(key=lambda r: r[0] - r[1])
    print(f"zero runs >= 1 KB: {len(gaps)}; largest:")
    for lo, hi in gaps[:8]:
        print(f"  0x{lo:08X} .. 0x{hi:08X}  {hi - lo:7d} B")
    print(f"first non-zero above 0x{DDR:08X}: ", end="")
    for i, b in enumerate(buf):
        if b:
            print(f"0x{DDR + i:08X}")
            break
    else:
        print("(none)")

    # the specific claim
    seg = buf[CLAIM_LO - DDR:OURS - DDR]
    nz = sum(1 for b in seg if b)
    print(flush=True)
    print(f"CLAIMED GAP 0x{CLAIM_LO:08X} .. 0x{OURS:08X} "
          f"({len(seg)} B): {nz} bytes non-zero")
    if nz == 0:
        biggest = max(runs(seg, CLAIM_LO), key=lambda r: r[1] - r[0],
                      default=None)
        free = biggest[1] - biggest[0] if biggest else 0
        print(f"VERDICT: gap is EMPTY -> {free} B free "
              f"(0x{biggest[0]:08X} .. 0x{biggest[1]:08X})" if biggest
              else "VERDICT: gap is EMPTY")
        ok = free >= 46080
        print(f"ECHO (needs ~46 KB for 250 ms stereo float): "
              f"{'FITS' if ok else 'TOO SMALL'}")
    else:
        first, last = None, None
        for i, b in enumerate(seg):
            if b:
                if first is None:
                    first = CLAIM_LO + i
                last = CLAIM_LO + i
        print(f"VERDICT: gap is USED (0x{first:08X} .. 0x{last:08X}) -> "
              f"no room for a delay line there")
    # what sits at the claim's start, if anything
    head = dump(d, CLAIM_LO, 64)
    print(f"0x{CLAIM_LO:08X}: " + " ".join(
        f"{int.from_bytes(head[i:i + 4], 'little'):08x}" for i in range(0, 64, 4)))
    return 0 if nz == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
