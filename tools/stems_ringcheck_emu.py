#!/usr/bin/env python3
"""stems_ringcheck_emu.py -- EMULATOR ONLY: after ONE stem is muted, is every
block the audio chip is going to play exactly the new mix?

    .venv/bin/python tools/stems_ringcheck_emu.py [STEM 0..3] [SECS]

From cache/emu_flush_playing.pkl (STEMS file playing, read-ahead full) with
the current build/stems module swapped in: mute STEM (default 1 = DRUMS),
play SECS (default 14: the rewrite pass finishes AND the deck reads more of
the file, which it mixes itself as it reads), then read every labelled block
ahead of the play position out of the chip and compare it with the file:

  new     = the three other stems summed  (blocks the deck read after the tap)
  sub     = the original mix minus the muted stem  (blocks the pass rewrote
            through the one-plane path; same thing to the ear)
  orig    = the original mix                                 (still un-muted)
  double  = new minus the muted stem AGAIN                   (2026-11-10 bug:
            the one-plane path ran on into blocks that were already muted;
            the stem comes back inverted, ~25 s after the tap)
  other   = anything else (allowed: the counted fade blocks)

PASS = no double, no orig ahead of the playhead, other <= fades, the
one-plane path was used (or the test proves nothing about it), no chip error.
"""
import os
import struct
import sys
sys.path[:0] = [".", "tools"]
import numpy as np                       # noqa: E402
import perform_pads as PP                # noqa: E402
import stems_fastmute_emu as FM          # noqa: E402
from dsp_pitch_probe import SH_STEPS_PER_S   # noqa: E402

RING, SLOT = 0xC0090000, 4804


def main():
    stem = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 14.0
    m, s = FM.to_measure_point("new")
    mem, d = m.mem, m.mem.dev.dspemu
    root = getattr(mem.dev.usb_disk, "root", None)
    lab = lambda sl: d.bus.read(RING + sl * SLOT + 4704, 4)          # noqa: E731
    base = mem.r8(PP.PMASK) & 15
    assert base == 0, f"snapshot already has a mask ({base:#x})"
    mem.w8(PP.PMASK, 1 << stem)
    left = int(secs * SH_STEPS_PER_S)
    while left > 0:
        k = min(left, 250_000_000)
        m.cpu.run(limit=k)
        left -= k
    ps = d.bus.read(0x1182E81C, 4)
    p = lab(ps)
    c = {k: mem.r32(s["sm_" + k]) for k in ("parm", "drblk", "drsub", "drsubf", "drchk", "drbump",
                                            "drlate", "drfade", "drwfail", "drnoslot", "drend",
                                            "drwait", "drms", "drbat", "drtot", "drpk", "hits")
         if "sm_" + k in s}
    if {"drms", "drbat", "drtot"} <= set(c):
        c["drms_avg"] = round(c["drtot"] / c["drbat"], 2) if c["drbat"] else None
    print(f"play block {p} (slot {ps}); counters {c}; chip error {d.error}", flush=True)
    cands = []
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.lower().endswith((".aif", ".aiff", ".wav")):
                q = os.path.join(dp, f)
                with open(q, "rb") as fh:
                    fh.seek(-128, 2)
                    t = fh.read(128)
                if t[:4] == b"XST4":
                    cands.append((q, t))
    assert cands, "no STEMS file on the test stick"

    def blk(sl):
        raw = b"".join(struct.pack("<I", d.bus.read(RING + sl * SLOT + 4 * i, 4))
                       for i in range(1176))
        return np.frombuffer(raw, "<f4") * 32768

    ring = []                               # (block, samples) ahead of the playhead
    b = p + 3
    while b < p + 3000 and lab(ps + (b - p)) == b:
        ring.append((b, blk(ps + (b - p))))
        b += 1
    best = None
    for path, tr in cands:                  # the playing file = the best match
        doff, dlen = struct.unpack("<II", tr[4:12])
        rel = struct.unpack("<4I", tr[16:32])
        dt = ">i2" if tr[12] else "<i2"
        n = {"new": 0, "sub": 0, "orig": 0, "double": 0, "other": 0}
        first = {}
        with open(path, "rb") as fh:
            for b, got in ring:
                if doff + b * 2352 + max(rel) + 2352 > os.path.getsize(path):
                    n["other"] += 1
                    continue
                fh.seek(doff + b * 2352)
                orig = np.frombuffer(fh.read(2352), dt).astype(float)
                pl = []
                for k in range(4):
                    fh.seek(doff + b * 2352 + rel[k])
                    pl.append(np.frombuffer(fh.read(2352), dt).astype(float))
                new = np.clip(sum(pl[k] for k in range(4) if k != stem), -32768, 32767)
                dbl = np.clip(new - pl[stem], -32768, 32767)
                sub = np.clip(orig - pl[stem], -32768, 32767)
                dbl2 = np.clip(sub - pl[stem], -32768, 32767)
                kind = ("new" if np.array_equal(got, new) else
                        "sub" if np.array_equal(got, sub) else
                        "double" if np.array_equal(got, dbl2) else
                        "orig" if np.array_equal(got, orig) else
                        "double" if np.array_equal(got, dbl) else "other")
                n[kind] += 1
                first.setdefault(kind, b)
        if best is None or n["other"] < best[1]["other"]:
            best = (path, n, first)
    path, n, first = best
    b = ring[-1][0] + 1 if ring else p + 3
    print(f"{os.path.basename(path)}: {b - p - 3} blocks ahead of the playhead checked: {n}; "
          f"first of each kind {first}")
    # 2026-11-16: with the short read-ahead the deck reads nothing more while
    # the buffer is over the limit, so after SECS only ~(buffer - SECS) of
    # blocks are left ahead (1016 here, was > 1500 when it kept reading 30 s
    # ahead). What matters is that EVERY block left is the new mix.
    good = n["new"] + n["sub"]
    ok = (d.error is None and c["parm"] >= 1 and good >= 300
          and good >= len(ring) - c.get("drfade", 0) - 1 and n["double"] == 0
          and n["orig"] == 0 and n["other"] <= c.get("drfade", 0) + 1 and c["drwfail"] == 0)
    if not c.get("drsub"):
        print("  note: the one-plane path was not used in this run")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
