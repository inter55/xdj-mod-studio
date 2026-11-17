#!/usr/bin/env python3
"""keydetect_test.py -- all 24 keys, synthetic harmonic music, plus
transposition tracking on real audio (resample by 2^(n/12))."""
import os
import sys
import numpy as np
sys.path.insert(0, "tools")
import keydetect as K

SR = 44100
MAJ_SC = [0, 2, 4, 5, 7, 9, 11]
MIN_SC = [0, 2, 3, 5, 7, 8, 10]      # natural minor


def note(f, dur, rng):
    t = np.arange(int(dur * SR)) / SR
    y = sum((0.6 ** h) * np.sin(2 * np.pi * f * (h + 1) * t + rng.uniform(0, 6)) for h in range(5))
    return y * np.minimum(1, np.minimum(t * 40, (dur - t) * 20))


def piece(tonic, minor, rng, bars=24):
    sc = MIN_SC if minor else MAJ_SC
    out = []
    for b in range(bars):
        deg = [0, 0, 4, 2, 0, 3, 4, 0][b % 8] if not minor else [0, 0, 5, 3, 0, 6, 4, 0][b % 8]
        for k in range(4):                      # 4 melody notes per bar over a triad
            d = (deg + [0, 2, 4, 2][k] + rng.integers(0, 2) * 2) % 7
            f = 220 * 2 ** (((tonic + sc[d]) % 12 - 9) / 12.0)
            y = note(f, 0.4, rng)
            root = 110 * 2 ** (((tonic + sc[deg % 7]) % 12 - 9) / 12.0)
            y[:len(y)] += 0.8 * note(root, 0.4, rng)
            out.append(y)
    return np.concatenate(out)


def main():
    rng = np.random.default_rng(3)
    ok = 0
    for minor in (False, True):
        for tonic in range(12):
            x = piece(tonic, minor, rng)
            t, mn, conf = K.detect(x, SR)
            good = (t, mn) == (tonic, minor)
            # relative major/minor counts as "close" but is reported
            ok += good
            if not good:
                print(f"  miss: true {K.name(tonic, minor)} got {K.name(t, mn)} conf {conf:.3f}")
    print(f"synthetic: {ok}/24 exact")
    # Informational leg only: the pass/fail above is the synthetic 24-key
    # check. The clip came from an older key-shift session and no tool in the
    # repo regenerates it, so a fresh checkout must not die here.
    sample = "work/shift_samples/keyshift_+0.wav"
    if not os.path.exists(sample):
        print(f"  real-audio leg skipped: {sample} not present (optional)")
        return 0 if ok >= 22 else 1
    x, sr = K.read_wav(sample)
    t0, m0, _ = K.detect(x, sr)
    for n in (1, 2, 3, 5, 7):
        r = 2 ** (n / 12.0)
        idx = np.arange(int(len(x) / r)) * r
        y = np.interp(idx, np.arange(len(x)), x)
        t, m, c = K.detect(y, sr)
        print(f"  real clip {K.name(t0, m0)} resampled +{n}: got {K.name(t, m)} "
              f"(want tonic {K.NOTES[(t0 + n) % 12]}) conf {c:.3f}")
    return 0 if ok >= 22 else 1


if __name__ == "__main__":
    sys.exit(main())
