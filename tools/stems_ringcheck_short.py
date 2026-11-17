#!/usr/bin/env python3
"""stems_ringcheck_short.py SNAP [STEM] [SECS] -- EMULATOR ONLY: right after
a tap, what do the buffered blocks ahead of the playhead hold?

SNAP = a settled short-read-ahead state (tools/stems_shortahead_emu.py
leaves build/shortahead_base.pkl) with the stems module installed. Toggles
STEM (default 1 drums) from the snapshot's own mask, runs SECS (default
0.3), then classifies each labelled block from the playhead on:
  new = mix for the new mask, old = mix for the old mask, other = neither.
"""
import os
import struct
import sys
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
import numpy as np                       # noqa: E402
from emu.machine import Machine          # noqa: E402
import perform_pads as PP                # noqa: E402
from dsp_pitch_probe import SH_STEPS_PER_S   # noqa: E402

RING, SLOT = 0xC0090000, 4804


def main():
    snap = sys.argv[1]
    stem = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3
    m = Machine.load(snap)
    mem, d = m.mem, m.mem.dev.dspemu
    lab = lambda sl: d.bus.read(RING + sl * SLOT + 4704, 4)          # noqa: E731
    old = mem.r8(PP.PMASK) & 15
    new = old ^ (1 << stem)
    ps0 = d.bus.read(0x1182E81C, 4)
    p0 = lab(ps0)
    mem.w8(PP.PMASK, new)
    m.cpu.run(limit=int(secs * SH_STEPS_PER_S))
    ps = d.bus.read(0x1182E81C, 4)
    p = lab(ps)
    root = mem.dev.usb_disk.root
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

    def blk(sl):
        raw = b"".join(struct.pack("<I", d.bus.read(RING + sl * SLOT + 4 * i, 4)) for i in range(1176))
        return np.frombuffer(raw, "<f4") * 32768

    best = None
    for path, tr in cands:
        doff = struct.unpack("<I", tr[4:8])[0]
        rel = struct.unpack("<4I", tr[16:32])
        dt = ">i2" if tr[12] else "<i2"
        rows = []
        with open(path, "rb") as fh:
            def planes(b):
                pl = []
                for k in range(4):
                    fh.seek(doff + b * 2352 + rel[k])
                    pl.append(np.frombuffer(fh.read(2352), dt).astype(float))
                return pl

            def mix(pl, mask):
                return np.clip(sum(pl[k] for k in range(4) if not mask & (1 << k)), -32768, 32767)
            for i in range(0, 320):
                b, sl = p + i, ps + i
                if lab(sl) != b:
                    rows.append((b, "nolabel"))
                    continue
                got = blk(sl)
                pl = planes(b)
                kn = "new" if np.allclose(got, mix(pl, new), atol=1) else \
                     "old" if np.allclose(got, mix(pl, old), atol=1) else "other"
                if kn == "other":
                    fh.seek(doff + b * 2352)
                    fm = np.frombuffer(fh.read(2352), dt).astype(float)
                    if np.allclose(got, fm, atol=1):
                        kn = "old (file mix)"
                    elif np.allclose(got, np.clip(fm - pl[stem] if new & (1 << stem)
                                                  else fm + pl[stem], -32768, 32767), atol=1):
                        kn = "new (file mix -/+ stem)"
                    else:
                        kn = f"other (rms diff vs new {np.sqrt(np.mean((got - mix(pl, new)) ** 2)):.0f}, " \
                             f"stem rms {np.sqrt(np.mean(pl[stem] ** 2)):.0f})"
                rows.append((b, kn))
        score = sum(1 for _, k in rows if k in ("new", "old"))
        if best is None or score > best[0]:
            best = (score, path, rows)
    _, path, rows = best
    runs, cur = [], None
    for b, k in rows:
        k = k.split(" (rms")[0] if k.startswith("other") else k
        if cur and cur[0] == k:
            cur[2] = b
        else:
            cur = [k, b, b]
            runs.append(cur)
    print(f"{os.path.basename(path)}: mask {old:#x} -> {new:#x}; tap at block {p0}, now {p} "
          f"(+{secs} s); chip error {d.error}")
    for k, a, b in runs:
        print(f"  blocks {a - p:+4d}..{b - p:+4d}  ({b - a + 1:3d})  {k}")


if __name__ == "__main__":
    main()
