#!/usr/bin/env python3
"""stems_overtake_emu.py -- EMULATOR ONLY: the stem rewrite when it is SLOWER
than playback (a slow or busy stick on a real deck). The emulator's stick is
too fast to show that, so this builds the stems module with its pacing set to
one batch per 300 ms of the deck clock (well under the 75 blocks/s played; the
emulated 1 ms clock runs ~2.7x fast), mutes VOCALS for 4 s and
then checks every block in the audio chip's store around the play position:

    each block must be EXACTLY the original mix or EXACTLY the muted mix
    (or one of the counted fade blocks) -- never a half-written block.

    .venv/bin/python tools/stems_overtake_emu.py

Rebuilds the normal module afterwards (bytes compared with the one it found).
"""
import os
import struct
import subprocess
import sys
sys.path[:0] = [".", "tools"]
import numpy as np                       # noqa: E402
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
import stems_fastmute_emu as FM          # noqa: E402
from dsp_pitch_probe import SH_STEPS_PER_S   # noqa: E402

RING, SLOT = 0xC0090000, 4804


def build(flags):
    env = dict(os.environ, PMASK=hex(PP.PMASK), SM_CFLAGS=flags)
    subprocess.run(["sh", "tools/stems/build.sh"], env=env, check=True,
                   stdout=subprocess.DEVNULL)


def main():
    keep = open("build/stems/sm.bin", "rb").read()
    build("-DDR_PACE_MS=300u")
    try:
        ok = run()
    finally:
        build("")
        same = open("build/stems/sm.bin", "rb").read() == keep
        print("normal module rebuilt:", "same bytes" if same else "DIFFERENT BYTES")
    print("RESULT:", "PASS" if ok and same else "FAIL")
    sys.exit(0 if ok and same else 1)


def run():
    m, s = FM.to_measure_point("new")
    mem, d = m.mem, m.mem.dev.dspemu
    disk = mem.dev.usb_disk
    root = getattr(disk, "root", None) or getattr(disk, "path", None)
    lab = lambda sl: d.bus.read(RING + sl * SLOT + 4704, 4)
    ps0 = d.bus.read(0x1182E81C, 4)
    p0 = lab(ps0)
    mem.w8(PP.PMASK, 0x01)
    m.cpu.run(limit=int(SH_STEPS_PER_S * 4.0))
    ps1 = d.bus.read(0x1182E81C, 4)
    p1 = lab(ps1)
    c = {k: mem.r32(s["sm_" + k]) for k in ("drblk", "drbump", "drlate", "drfade",
                                             "drwfail", "drms", "drnoslot", "drsub")}
    print(f"played {p0} -> {p1}; counters {c}; dsp err {d.error}", flush=True)
    # the playing file = the STEMS file whose data matches an untouched block
    cands = []
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.lower().endswith((".aif", ".aiff", ".wav")):
                p = os.path.join(dp, f)
                with open(p, "rb") as fh:
                    fh.seek(-128, 2)
                    t = fh.read(128)
                if t[:4] == b"XST4":
                    cands.append((p, t))

    def blk(sl):
        raw = b"".join(struct.pack("<I", d.bus.read(RING + sl * SLOT + 4 * i, 4))
                       for i in range(1176))
        return np.frombuffer(raw, "<f4") * 32768

    best = None
    for path, t in cands:
        doff, dlen = struct.unpack("<II", t[4:12])
        big = t[12]
        rel = struct.unpack("<4I", t[16:32])
        dt = ">i2" if big else "<i2"
        with open(path, "rb") as fh:
            n = {"orig": 0, "new": 0, "other": 0}
            flips = 0
            last = None
            for b in range(p0, p1 + 400):
                sl = ps0 + (b - p0)
                if lab(sl) != b:
                    continue
                got = blk(sl)
                fh.seek(doff + b * 2352)
                orig = np.frombuffer(fh.read(2352), dt).astype(float)
                acc = np.zeros(1176)
                for k in (1, 2, 3):
                    fh.seek(doff + b * 2352 + rel[k])
                    acc = acc + np.frombuffer(fh.read(2352), dt)
                new = np.clip(acc, -32768, 32767)
                fh.seek(doff + b * 2352 + rel[0])
                # one-plane path: the original mix minus the muted stem
                sub = np.clip(orig - np.frombuffer(fh.read(2352), dt), -32768, 32767)
                kind = ("new" if np.array_equal(got, new) or np.array_equal(got, sub) else
                        "orig" if np.array_equal(got, orig) else "other")
                n[kind] += 1
                if kind != "other":
                    flips += last is not None and kind != last
                    last = kind
        if best is None or n["other"] < best[1]["other"]:
            best = (path, n, flips)
    path, n, flips = best
    print(f"{os.path.basename(path)}: blocks {n}, old<->new switches {flips}")
    ok = (d.error is None and c["drbump"] > 0 and n["new"] > 0
          and n["other"] <= c["drfade"] and c["drwfail"] == 0)
    if c["drbump"] == 0:
        print("  (the pass was never overtaken: the test did not test anything)")
    if n["other"] > c["drfade"]:
        print(f"  {n['other']} blocks are neither mix but only {c['drfade']} fades were made")
    return ok


if __name__ == "__main__":
    main()
