#!/usr/bin/env python3
"""fx_dsp_emu.py -- EMULATOR ONLY: TOUCH FX inside the real DSP program
(tools/fx_dsp/fx.c), measured on the DSP's audio output.

    EFF=filter X=40 Y=200 DEPTH=255 .venv/bin/python tools/fx_dsp_emu.py [SNAP]

Starts from a snapshot with the real DSP playing (default
cache/emu_dsp_playing7.pkl), turns MASTER TEMPO on, then
  CONTROL run : MT on, FX off               -> build/fx_ctl.wav
  FX run      : fx.bin in DSP DDR at 0xC005F4C0, call at 0xC0008D64
                re-pointed, the stretcher's staging loads re-pointed at
                FX.pbuf, SH tempo-copy store 0x08D36B3A NOPed and
                0x09D24DD8 = the tagged FX param word (-> mailbox BD8)
                                              -> build/fx_<eff>.wav
and prints a per-effect measurement + PASS/FAIL.
EFF = filter | crush | gate | echo. X/Y/DEPTH are 0..255 (default 40/200/255).
"""
import os
import struct
import subprocess
import sys
import time
sys.path[:0] = [".", "tools"]
import numpy as np                       # noqa: E402
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import dsp_pitch_probe as P              # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("FX_BIN") or os.path.join(HERE, "fx_dsp", "fx.bin")
ELF = os.path.join(HERE, "fx_dsp", "fx.elf")
FX_BASE = 0xC005F4C0
CALL = 0xC0008D64
CALL_FP = 0xC0008D60
PULLER = 0xC001CBF4
SH_STORE = 0x08D36B3A
SH_SLOT = 0x09D24DD8
EFF = {"filter": 0, "crush": 1, "gate": 2, "echo": 3, "flanger": 4,
       "pitch": 5}
# Same numbers as fx.c. Overridable so a short-ring build (FX_RING_BASE /
# FX_RING_FRAMES in build.sh) can be gated without editing this file.
ECHO_FRAMES = int(os.environ.get("FX_FRAMES", "6500"))
ECHO_MINF = int(os.environ.get("FX_MINFRAMES", "882"))
ECHO_BASE = int(os.environ.get("FX_RING", "0xC0073400"), 0)


def branch_word(old, fp, target):
    return (old & ~(0x1FFFFF << 7)) | ((((target - fp) >> 2) & 0x1FFFFF) << 7)


def fx_symbol():
    nm = os.path.join(HERE, "..", "work", "tic6x-tools", "bin", "tic6x-elf-nm")
    out = subprocess.run([nm, ELF], capture_output=True, text=True).stdout
    return {l.split()[2]: int(l.split()[0], 16) for l in out.splitlines() if len(l.split()) == 3}


def fx_word(eff, x, y, depth, on=1):
    return (0x80000000 | (on << 29) | ((eff & 7) << 26) |
            ((x & 0xFF) << 18) | ((y & 0xFF) << 10) | ((depth & 0xFF) << 2))


def install_dsp(d, blob):
    fx = fx_symbol()["FX"]
    cur = bytes(d.bus.read(FX_BASE + i, 1) for i in range(len(blob)))
    if cur == blob and d.bus.read(CALL, 4) == branch_word(d.bus.read(CALL, 4), CALL_FP, FX_BASE):
        print("  DSP already runs the FX engine (hot-installed) - not patching", flush=True)
        return fx
    for i in range(0, len(blob), 4):
        d._store(FX_BASE + i, int.from_bytes(blob[i:i + 4].ljust(4, b"\0"), "little"))
    for a in range(fx, fx + 0x180, 4):
        d._store(a, 0)
    for a, o in ((0xC001CD40, 0x3356B828), (0xC001CD98, 0x0308C168),
                 (0xC001CE34, 0x01D6B828), (0xC001CE48, 0x0188C168)):
        cur = d.bus.read(a, 4)
        c = (fx >> 16) if a in (0xC001CD98, 0xC001CE48) else (fx & 0xFFFF)
        new = (o & ~(0xFFFF << 7)) | (c << 7)
        assert cur in (o, new), (hex(a), hex(cur))
        d._store(a, new)
    old = d.bus.read(CALL, 4)
    assert branch_word(old, CALL_FP, PULLER) == old, hex(old)
    d._store(CALL, branch_word(old, CALL_FP, FX_BASE))
    return fx


def play(m, secs, chunk=100_000_000):
    d = m.mem.dev.dspemu
    n0 = len(d.asp1.out)
    steps = int(secs * P.SH_STEPS_PER_S)
    while steps > 0:
        k = min(steps, chunk)
        m.cpu.run(limit=k)
        steps -= k
    return d.asp1.out[n0:]


def mt_on(m):
    dev = m.mem.dev
    dev.panel_frame = panl.build_frame(keys=["MasterTempo"])
    m.cpu.run(limit=5_000_000)
    dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=30_000_000)


def hi_ratio(x, split=4000.0):
    """fraction of spectral energy above `split` Hz"""
    n = 16384
    win = np.hanning(n)
    acc = np.zeros(n // 2 + 1)
    for s in range(0, len(x) - n, n // 2):
        acc += np.abs(np.fft.rfft(x[s:s + n] * win)) ** 2
    f = np.fft.rfftfreq(n, 1 / P.FS)
    tot = acc.sum()
    return acc[f > split].sum() / tot if tot else 0.0


def bands(x):
    """log energy in 1/6-octave bands, 100 Hz .. 20 kHz"""
    n = 8192
    win = np.hanning(n)
    acc = np.zeros(n // 2 + 1)
    for k in range(0, len(x) - n, n // 2):
        acc += np.abs(np.fft.rfft(x[k:k + n] * win)) ** 2
    f = np.fft.rfftfreq(n, 1 / P.FS)
    ed = 100 * 2 ** (np.arange(0, 6 * np.log2(200) + 1) / 6)
    return np.log10(np.array([acc[(f >= a) & (f < b)].sum() + 1e-9 for a, b in zip(ed[:-1], ed[1:])]))


def env_mod(x):
    """depth of amplitude modulation: (max-min)/(max+min) of a smoothed
    RMS envelope, and its dominant mod frequency"""
    h = 128
    e = np.sqrt(np.array([np.mean(x[i:i + h] ** 2) for i in range(0, len(x) - h, h)]) + 1e-9)
    e = e - e.mean()
    r = np.correlate(e, e, "full")[len(e) - 1:]
    lo, hi = int(P.FS / h / 60), int(P.FS / h / 1.5)   # 1.5..60 Hz
    k = lo + int(np.argmax(r[lo:hi])) if hi > lo else 1
    env = np.sqrt(np.array([np.mean(x[i:i + h] ** 2) for i in range(0, len(x) - h, h)]) + 1e-9)
    mx, mn = np.percentile(env, 95), np.percentile(env, 5)
    return (mx - mn) / (mx + mn + 1e-9), P.FS / h / k


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_dsp_playing7.pkl"
    eff = os.environ.get("EFF", "filter")
    xv = int(os.environ.get("X", "40"))
    yv = int(os.environ.get("Y", "200"))
    dv = int(os.environ.get("DEPTH", "255"))
    secs = float(os.environ.get("SECS", "4"))
    blob = open(BIN, "rb").read()
    res, st, pos = {}, {}, {}
    cache = f"build/fx_ctl_{os.path.basename(snap)}_{secs}.npy"
    for mode in ("ctl", "fx"):
        if mode == "ctl" and os.path.exists(cache) and not os.environ.get("NOCACHE"):
            a = np.load(cache)
            pos["ctl"] = int(open(cache + ".pos").read())
            st["ctl"] = a
            res["ctl"] = a.mean(1)
            print("ctl: cached", cache, flush=True)
            continue
        t = time.time()
        m = Machine.load(snap)
        d, mem = m.mem.dev.dspemu, m.mem
        mt_on(m)
        if mode == "fx":
            fx = install_dsp(d, blob)
            mem.w16(SH_STORE, 0x0009)
            mem.w32(SH_SLOT, fx_word(EFF[eff], xv, yv, dv))
        play(m, 0.3)
        c0 = d.bus.read(0x11837CCC, 4)
        words = play(m, secs)
        pos[mode] = (d.bus.read(0x11837CCC, 4) - c0) & 0xFFFFFFFF
        x = P.wav_write(f"build/fx_{mode}.wav", words)
        a = np.array(words, dtype=np.uint32).view(np.int32)
        a = (a[: len(a) // 2 * 2].reshape(-1, 2) >> 16).astype(float)
        res[mode] = x
        st[mode] = a
        if mode == "ctl":
            np.save(cache, a)
            open(cache + ".pos", "w").write(str(pos["ctl"]))
        extra = ""
        if mode == "fx":
            v = [d.bus.read(fx + 256 + 4 * i, 4) for i in range(4)]
            extra = f" magic {v[0]:#x} calls {v[1]} active {v[2]} w {v[3]:#x}"
            if eff == "echo":       # delay-line diagnostics
                extra += " pbuf " + " ".join(f"{d.bus.read(fx + 4 * i, 4):08x}" for i in range(4))
                extra += " buf " + " ".join(f"{d.bus.read(ECHO_BASE + 4 * i, 4):08x}" for i in range(8))
                extra += f" dpos {d.bus.read(fx + 316, 4)} pad {d.bus.read(fx + 320, 4):08x}"
        print(f"{mode}: out {len(x)/P.FS:.2f}s err={d.error}{extra} [{time.time()-t:.0f}s]", flush=True)
    c, f = res["ctl"], res["fx"]
    n = min(len(c), len(f))
    c, f = c[:n], f[:n]
    import wave
    for nm, a in (("ctl", c), ("fx", f)):
        with wave.open(f"build/fx_{nm}.wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(P.FS)
            w.writeframes(np.clip(a, -32768, 32767).astype("<i2").tobytes())
    # sanity: not silent, tempo kept, both channels alive and alike
    sc, sf = st["ctl"][:n], st["fx"][:n]
    rms = lambda v: float(np.sqrt(np.mean(v ** 2)))
    rl, rr = rms(sf[:, 0]), rms(sf[:, 1])
    cl, cr = rms(sc[:, 0]), rms(sc[:, 1])
    # tempo = the DSP's own play-position counter (0x11837CCC, ~75/s while
    # playing); the beat estimate is fooled by a strong chopper/gate
    tr = pos["fx"] / pos["ctl"] if pos.get("ctl") else 0.0
    bal = min(rl, rr) / max(rl, rr, 1e-9)
    cbal = min(cl, cr) / max(cl, cr, 1e-9)
    # an effect never makes the music 3x louder: full-scale garbage (the
    # unstable filter corner fixed in 1.0.8: rms 32768/32768) used to pass
    sane = (rl > 50 and rr > 50 and abs(tr - 1) < 0.03 and bal > cbal * 0.7
            and rl < 3 * cl and rr < 3 * cr)
    print(f"SANITY: rms L/R ctl {cl:.0f}/{cr:.0f} fx {rl:.0f}/{rr:.0f} "
          f"(balance {cbal:.2f} -> {bal:.2f}), play position {pos.get('ctl')} -> {pos['fx']} "
          f"blocks (x{tr:.3f}) -> {'ok' if sane else 'BAD'}")
    if eff == "filter":
        hc, hf = hi_ratio(c), hi_ratio(f)
        ok = hf < hc * 0.6
        print(f"FILTER X={xv} Y={yv}: hi-band(>4k) energy {hc:.4f} -> {hf:.4f} "
              f"({hf/hc*100:.0f}%) -> RESULT: {'PASS' if ok and sane else 'FAIL'}")
    elif eff == "crush":
        # model: the same crush math (fx.c) on the control audio
        nb = int(1.0 + yv / 255 * 15)
        step = 2.0 / (2 ** nb) * 32768
        hold = 1 + int((1 - xv / 255) * 31)
        mdl = np.empty_like(c)
        for k in range(0, len(c), hold):
            v = c[k]
            mdl[k:k + hold] = np.round(v / step) * step
        w = dv / 255
        mdl = (1 - w) * c + w * mdl
        bc, bf, bm = bands(c), bands(f), bands(mdl)
        r_m, r_c = np.corrcoef(bf, bm)[0, 1], np.corrcoef(bf, bc)[0, 1]
        diff = np.sqrt(np.mean((f - c) ** 2)) / (np.sqrt(np.mean(c ** 2)) + 1e-9)
        ok = r_m > 0.9 and r_m > r_c and diff > 0.05
        print(f"CRUSH X={xv} Y={yv} (bits {nb}, hold {hold}): spectrum corr vs model "
              f"{r_m:.3f}, vs dry {r_c:.3f}; rel-change {diff:.3f} -> "
              f"RESULT: {'PASS' if ok and sane else 'FAIL'}")
    elif eff == "echo":
        # same mapping as fx.c: cap at ECHO_FRAMES-1 (a full ring length would
        # read the frame being written)
        df = ECHO_MINF + int(xv / 255 * (ECHO_FRAMES - 1 - ECHO_MINF))
        fb = yv / 255 * 0.75
        # cross-correlate the FX output with the dry control run: with
        # DEPTH=255 the output is the delay line only, so its peak must sit
        # exactly `df` frames behind the control, at the level feedback sets
        N = 1 << (2 * len(c) - 1).bit_length()
        rc = np.fft.irfft(np.fft.rfft(f, N) * np.conj(np.fft.rfft(c, N)), N)[:len(c)]
        rc /= np.sqrt((c * c).sum() * (f * f).sum()) + 1e-9
        peak = int(np.argmax(np.abs(rc[:min(len(c), 3 * df)])))
        r1 = float(rc[peak])
        r2 = float(rc[2 * df]) if 2 * df < len(rc) else 0.0
        ok = abs(peak - df) <= 24 and r1 > 0.35
        print(f"ECHO X={xv} Y={yv} D={dv}: delay {df} frames ({df/P.FS*1000:.0f} ms), "
              f"best lag {peak} ({peak/P.FS*1000:.0f} ms) corr {r1:.2f}, "
              f"2nd repeat {r2:.2f} (feedback {fb:.2f}) -> "
              f"RESULT: {'PASS' if ok and sane else 'FAIL'}")
    elif eff == "flanger":
        base, mod = 132, 88            # frames, same numbers as fx.c
        # a flanger's delayed copy must sit inside the swept window and the
        # lag must MOVE (that is the sweep): look at the best lag per quarter
        seg = len(c) // 4
        lags, corrs = [], []
        for k in range(4):
            a, b = f[k*seg:(k+1)*seg], c[k*seg:(k+1)*seg]
            N = 1 << (2 * len(a) - 1).bit_length()
            r = np.fft.irfft(np.fft.rfft(a, N) * np.conj(np.fft.rfft(b, N)), N)[:len(a)]
            r /= np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9
            k0 = int(np.argmax(np.abs(r[:base + mod + 40])))
            lags.append(k0); corrs.append(float(r[k0]))
        lo, hi = min(lags), max(lags)
        ok = (lo >= base - 6 and hi <= base + mod + 24 and
              max(corrs) > 0.3 and min(corrs) > 0.08)
        print(f"FLANGER X={xv} Y={yv} D={dv}: best lag per quarter {lags} "
              f"(window {base}..{base + mod}), sweep {hi - lo} frames, "
              f"corr {min(corrs):.2f}..{max(corrs):.2f} -> "
              f"RESULT: {'PASS' if ok and sane else 'FAIL'}")
    elif eff == "pitch":
        # same mapping as fx.c: X -> semitones -11..+11, k = 2^(semi/12).
        # Test: the FX spectrum must look like the control's spectrum moved
        # by 6*log2(k) of the 1/6-octave bands (bands() is 1/6 octave), and
        # better than the same comparison without the move.
        import math
        semi = int(xv * 22 / 255) - 11
        k = 2.0 ** (semi / 12.0)
        bc, bf = bands(c), bands(f)
        idx = np.arange(len(bc))
        moved = np.interp(idx - 6 * math.log2(k), idx, bc)
        r_m = float(np.corrcoef(bf, moved)[0, 1])
        r_0 = float(np.corrcoef(bf, bc)[0, 1])
        ok = k == 1.0 or (r_m > 0.8 and r_m > r_0)
        print(f"PITCH X={xv} -> {semi:+d} semitones (k={k:.3f}): spectrum corr "
              f"shifted {r_m:.3f} vs dry {r_0:.3f} -> "
              f"RESULT: {'PASS' if ok and sane else 'FAIL'}")
    elif eff == "gate":
        rate = 2.0 + (xv / 255) ** 2 * 38.0
        h = 256
        e = lambda v: np.sqrt(np.array([np.mean(v[i:i + h] ** 2) for i in range(0, len(v) - h, h)]) + 1e-3)
        g = e(f) / e(c)
        lo, hi = np.percentile(g, 10), np.percentile(g, 90)
        gs = np.abs(np.fft.rfft(g - g.mean()))
        fr = np.fft.rfftfreq(len(g), h / P.FS)
        band = (fr > 1.0)
        pk = fr[band][np.argmax(gs[band])]
        ok = hi / lo > 2.0 and abs(pk - rate) < 0.6
        print(f"GATE X={xv} Y={yv}: gain envelope p10 {lo:.2f} p90 {hi:.2f} "
              f"({hi/lo:.1f}x), peak {pk:.2f} Hz (want {rate:.2f}) -> "
              f"RESULT: {'PASS' if ok and sane else 'FAIL'}")


if __name__ == "__main__":
    main()
