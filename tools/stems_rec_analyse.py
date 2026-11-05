#!/usr/bin/env python3
"""stems_rec_analyse.py -- read a deck recording (tools/stems_qemu_test.py)
and say WHEN the muted stem left the sound, and whether playback stuttered.

Per 0.5 s window of the recording: where in the file it plays (cross-
correlation, followed through jumps), and whether it matches the plain mix
or the muted mix better. Stutter = file position not advancing 0.5 s per
window (repeats / skips) outside the one deliberate jump.

  .venv/bin/python tools/stems_rec_analyse.py [STEMSFILE] [DIR] [mask]
  -> prints "MUTE HEARD AFTER x.xx s" and "DISCONTINUITIES n"
"""
import os
import sys

import numpy as np

sys.path[:0] = [".", "tools"]
import stems4                                   # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "dist/deck-tests/Get Lucky ESQUIRE (STEMS).wav"
D = sys.argv[2] if len(sys.argv) > 2 else "build/stems_qemu"
MASK = int(sys.argv[3], 0) if len(sys.argv) > 3 else 1

rec = np.load(os.path.join(D, "rec.npy")).astype(np.float64)
tap_s, sr = open(os.path.join(D, "tap.txt")).read().split()
tap_s, sr = float(tap_s), int(sr)
_, _, _, st, mix = stems4.read(SRC)
mono = lambda a: a.reshape(-1, 2).astype(np.float64).mean(1)
M = mono(mix)
E = mono(stems4.expected(st, MASK))
W = sr // 2


def best(ref, seg, lo, hi):
    lo, hi = max(0, lo), min(len(ref) - len(seg), hi)
    if hi <= lo:
        return None, 0.0
    r = ref[lo:hi + len(seg)]
    n = 1 << int(np.ceil(np.log2(len(r) + len(seg))))
    c = np.fft.irfft(np.fft.rfft(r, n) * np.conj(np.fft.rfft(seg, n)), n)[:hi - lo + 1]
    # normalise by the local energy so loud passages don't win by size
    e2 = np.convolve(r * r, np.ones(len(seg)), "valid")[:hi - lo + 1]
    q = c / np.sqrt(e2 * (seg ** 2).sum() + 1e-9)
    k = int(np.argmax(q))
    return lo + k, float(q[k])


rows = []
pos = None
for w0 in range(0, len(rec) - W, W):
    seg = rec[w0:w0 + W]
    if np.abs(seg).max() < 100:
        continue
    if pos is None:
        lo, hi = 0, len(M)
    else:
        lo, hi = pos + W - sr // 4, pos + W + sr // 4
    pm, qm = best(M, seg, lo, hi)
    pe, qe = best(E, seg, lo, hi)
    if max(qm, qe) < 0.8 and pos is not None:      # a jump: search everywhere
        pm, qm = best(M, seg, 0, len(M))
        pe, qe = best(E, seg, 0, len(M))
    p = pm if qm >= qe else pe
    rows.append((w0 / sr, p / sr if p is not None else None, qm, qe))
    pos = p

heard = None
for t, p, qm, qe in rows:
    if t >= tap_s and heard is None and qe > qm + 0.05:
        heard = t
    elif heard is not None and t < heard + 1.5 and qm > qe + 0.05:
        heard = None                                 # not stable yet
jumps = []
for (t0, p0, *_), (t1, p1, *_) in zip(rows, rows[1:]):
    if p0 is None or p1 is None:
        continue
    d = (p1 - p0) - (t1 - t0)
    if abs(d) > 0.03:
        jumps.append(f"{t1:.1f}s:{d:+.2f}")
for t, p, qm, qe in rows:
    if tap_s - 1.5 <= t <= tap_s + 3 or (heard and heard - 1 <= t <= heard + 1):
        print(f"  rec {t:6.1f}s  file {p:7.2f}s  mix {qm:.2f}  muted {qe:.2f}"
              f"{'  <- tap' if abs(t - tap_s) < 0.26 else ''}")
print(f"tap at rec {tap_s:.2f} s")
print(f"MUTE HEARD AFTER {heard - tap_s:.2f} s" if heard else "MUTE NEVER HEARD")
print(f"DISCONTINUITIES {len(jumps)}", jumps[:12])
