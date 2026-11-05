#!/usr/bin/env python3
"""dsp_audio_diag.py -- headless DspSound bridge diagnostic (no CoreAudio).

Drives DspSound._callback via a fake AudioQueue buffer (an np arena), the
same way the pain report's diagnostic plan describes: record len(asp.out)
vs _drained, look for alternating L/R gap patterns, count gaps/partial
takes/stalls driven by the actual consumer.

Run: .venv/bin/python tools/dsp_audio_diag.py [seconds]
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, ".")
import numpy as np

from emu.machine import Machine
from emu import dspsound as D

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
# PACE=1: hold emulated time to the wall clock like emu/ui.py (play mode
# runs faster than real time since 2026-11-01; unpaced, the producer just
# outruns the consumer and `lag` grows)
PACE = os.environ.get("PACE", "0") == "1"
LEAD = float(os.environ.get("XDJ_PACE_LEAD", "0.1"))     # = EmuApp.PACE_LEAD
# A snapshot that is really PLAYING (DSP status 0x11837BF8 == 2): stick7's
# "Control clip.wav" auto-playing after load. emu_dsp_sound.pkl.gz is a
# PAUSED deck (the DSP loops its output ring there, now muted by the model).
SNAP = os.environ.get("SNAP", "cache/emu_dsp_wavplay.pkl.gz")


class FakeBuf(ctypes.Structure):
    # mirror macaudio._AQB (the _callback casts bufref to that struct)
    _fields_ = [("mAudioDataBytesCapacity", ctypes.c_uint32),
                ("mAudioData", ctypes.c_void_p),
                ("mAudioDataByteSize", ctypes.c_uint32),
                ("mUserData", ctypes.c_void_p),
                ("mPacketDescriptionCapacity", ctypes.c_uint32),
                ("mPacketDescriptions", ctypes.c_void_p),
                ("mPacketDescriptionCount", ctypes.c_uint32)]


def main():
    m = Machine.load(SNAP)
    s = D.DspSound(m)
    s.playing = s._dsp_playing()
    print(f"{SNAP}: DSP playing = {s.playing}")
    de = m.mem.dev.dspemu
    de.sync()                                     # warm the time anchor
    arena = np.zeros((4096, 2), "<i2")            # 16 KB stereo L/R
    keep = []                                     # keep the ctypes alive
    fb = FakeBuf()
    fb.mAudioDataBytesCapacity = 4096 * 4
    fb.mAudioData = arena.ctypes.data
    fb.mAudioDataByteSize = 4096 * 4
    bref = ctypes.cast(ctypes.byref(fb), ctypes.c_void_p)
    keep.extend((arena, fb, bref))
    hist = []                                     # (drained, len(out))
    t0 = time.time()
    n_calls = 0
    next_call = time.time()
    dev = m.mem.dev
    f0 = getattr(dev, "frames", 0)
    t0p = t0
    behind = 0
    while time.time() - t0 < SECS:
        if PACE:
            m.run(limit=1_000_000, max_cycles=4_000_000)   # as emu/ui.py
        else:
            m.cpu.run(limit=200_000)
        de.sync()
        if PACE:      # the app's _emu_loop rule: LCDC frames held to the wall clock
            ahead = (getattr(dev, "frames", 0) - f0) / 60.0 - (time.time() - t0p) - LEAD
            if ahead > 0.005:
                time.sleep(min(ahead, 0.05))
            elif ahead < -0.5:
                behind += 1
                t0p, f0 = time.time(), getattr(dev, "frames", 0)
        if time.time() >= next_call:              # AudioQueue pace (10.7 Hz)
            s._callback(None, None, bref)
            next_call += 4096 / 44100.0
            n_calls += 1
            hist.append((s._drained, len(de.asp1.out)))
    rows = (arena != 0).any(axis=1).sum()
    rt = n_calls / SECS * 4096 / 44100 * 100
    print(f"consumer calls={n_calls} (= {rt:.0f}% AudioQueue rate)"
          f"; served={s.served} frames" + (f"; fell >0.5 s behind {behind}x" if PACE else ""))
    print(f"gaps={s.gaps} half={s.half} max_stall={s.max_stall} "
          f"lag={s.lag} words, r_edge={s.r_edge} l_edge={s.l_edge} "
          f"primed={s.primed} skipped={s.trimmed_total}")
    print(f"audio rows nonzero: {rows}/4096")
    # interleave-gap pattern: sampled per callback, low half vs high half
    low = arena[:, 0]
    high = arena[:, 1]
    lnz = int((low != 0).sum())
    hnz = int((high != 0).sum())
    print(f"L plane tones: {lnz}/4096   R plane tones: {hnz}/4096   "
          f"({'balanced' if abs(lnz - hnz) < 256 else 'ONE-EAR SKEW'})")
    # drain-vs-produced: does _drained ever fall behind by > nfr?
    lags = [n - d for d, n in hist]
    print(f"max lag observed: {max(lags)} words")


if __name__ == "__main__":
    main()
