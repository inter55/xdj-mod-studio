#!/usr/bin/env python3
"""touchfx_stress.py -- EMULATOR ONLY: the gesture from the 2026-11-10 report
("fast swipes or switching effects fast: first the sound goes, then the
screen freezes") against the real DSP with the CURRENT engine + panel.

    .venv/bin/python tools/touchfx_stress.py [SNAP]

SNAP (default cache/emu_dsp_playing7.pkl) = real DSP playing a track. The
engine is patched into the DSP, PERFORM pads + the TOUCH FX panel into RAM,
then through the native touch path only:
  A  1.5 s, panel open, no finger                      (reference)
  B  3 s of abuse: the finger jumps across the whole pad every touch frame,
     lifts, taps another effect, DEPTH drags, FX ON twice, and again
  C  1 s, finger lifted
PASS = the audio chip never errors, its play position keeps the reference
rate in B and C, the output never leaves 3x the reference level (and is
not silent), the word is off after the lift, the panel repainted at most
once per 60 ms per layer during B, and the screen is stable afterwards
(the last change did get drawn).

What the emulator CANNOT show: how long a repaint takes on the real CPU
(uncached pixel writes) -- that is what the repaint limit is for; this test
only proves the limit works and nothing else broke.
"""
import os
import random
import sys
import time
sys.path[:0] = [".", "tools"]
import numpy as np                       # noqa: E402
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
import touchfx as TF                     # noqa: E402
import fx_dsp_emu as F                   # noqa: E402
import dsp_pitch_probe as P              # noqa: E402

POSW = 0x11837CCC
BD8 = 0x11837BD8
MSCLK = 0x0B704A80


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_dsp_playing7.pkl"
    t0 = time.time()
    m = Machine.load(snap)
    mem, c, d = m.mem, m.cpu, m.mem.dev.dspemu
    assert d is not None, "snapshot has no real DSP"
    ok = True
    rnd = random.Random(7)
    os.makedirs("screenshots/touchfx", exist_ok=True)
    from ghosttap_emu import fb_png

    def chk(label, good):
        nonlocal ok
        print(("  ok   " if good else "CHECK FAIL: ") + label, flush=True)
        ok &= bool(good)

    def run(n, frame=None):
        mem.dev.panel_frame = frame or panl.build_frame()
        c.run(limit=n)

    F.install_dsp(d, open("build/fx/fx.bin", "rb").read())
    F.mt_on(m)
    PP.install(mem)
    s = TF.install(mem)
    mem.w16(TF.NOPSITE, 0x0009)
    v = lambda n: mem.r32(s[n])                        # noqa: E731

    def last():
        n = v("tfx_logn")
        return mem.r32(s["tfx_log"] + 4 * ((n - 1) & 31)) if n else 0

    def press(xy, n=3):
        want = 1 << 31 | (xy[0] & 0x7FFF) << 16 | xy[1]
        k = 0
        for _ in range(300):
            run(250_000, panl.build_frame(touch=xy))
            if last() == want:
                k += 1
                if k >= n:
                    return True
        return False

    def lift():
        for _ in range(300):
            run(250_000)
            if v("tfx_logn") and not last() >> 31:
                break
        run(500_000)

    for _ in range(3):
        if mem.r32(PP.MODEW) == 7:
            break
        run(3_000_000, panl.build_frame(touch=(705, 37)))
        run(8_000_000)
    chk("PERFORM screen", mem.r32(PP.MODEW) == 7)
    press(TF.TAB)
    lift()
    chk("panel open", v("tfx_mode") == 1)

    def audio(n0):
        a = np.array(d.asp1.out[n0:], dtype=np.uint32).view(np.int32)
        a = (a[: len(a) // 2 * 2].reshape(-1, 2) >> 16).astype(float)
        return a

    def quiet(tag, secs):
        n0, p0 = len(d.asp1.out), d.bus.read(POSW, 4)
        steps = int(secs * P.SH_STEPS_PER_S)
        while steps > 0:
            k = min(steps, 50_000_000)
            run(k)
            steps -= k
        a = audio(n0)
        p = (d.bus.read(POSW, 4) - p0) & 0xFFFFFFFF
        r = float(np.sqrt(np.mean(a ** 2)))
        print(f"[{tag}] {len(a) / P.FS:.2f}s out, rms {r:.0f}, peak {np.abs(a).max():.0f}, "
              f"play +{p} blocks [{time.time() - t0:.0f}s]", flush=True)
        return r, p / secs

    rA, pA = quiet("A reference", 1.5)

    def cell(i):
        return (TF.RX + (i & 1) * TF.GB_DX + TF.GB_W // 2, TF.GB_Y0 + (i >> 1) * TF.GB_DY + 12)

    # ---- B: abuse ----
    # NOTE: this snapshot loses its stick ~9 s in (EMERGENCY LOOP by itself,
    # with no mods and no touches): keep A + B + C under ~6.5 s of audio.
    secs = float(os.environ.get("SECS", "3"))
    n0, p0 = len(d.asp1.out), d.bus.read(POSW, 4)
    f0, dr0, df0, ms0 = v("tfx_full"), v("tfx_draws"), v("tfx_defer"), mem.r32(MSCLK)
    end = c.steps + int(secs * P.SH_STEPS_PER_S) if hasattr(c, "steps") else None
    budget = int(secs * P.SH_STEPS_PER_S)
    used = 0
    words = set()
    seen = set()
    effs = [int(x) for x in os.environ.get("EFFS", "1,2,0,5,2,1,0").split(",")]
    k = 0
    while (len(d.asp1.out) - n0) / 2 / P.FS < secs:
        # fast swipes: a new corner-to-corner position every touch frame
        for _ in range(40):
            xy = (rnd.choice((TF.PAD[0] + 3, TF.PAD[2] - 4, rnd.randint(TF.PAD[0] + 3, TF.PAD[2] - 4))),
                  rnd.choice((TF.PAD[1] + 3, TF.PAD[3] - 4, rnd.randint(TF.PAD[1] + 3, TF.PAD[3] - 4))))
            run(250_000, panl.build_frame(touch=xy))
            used += 250_000
            words.add(mem.r32(TF.KEYWORD))
        run(500_000); used += 500_000                       # noqa: E702
        # fast effect switching: three taps in a row, barely lifted between
        s0 = c.steps if hasattr(c, "steps") else None
        for _ in range(3):
            press(cell(effs[k % len(effs)]), n=1)
            lift()
            seen.add(v("tfx_eff"))
            k += 1
        # DEPTH drag (one change per frame) and FX ON twice
        for j in range(12):
            press((TF.RX + 10 + 20 * j, TF.DEP_Y + 5), n=1)
        lift()
        for _ in range(2):
            press((TF.RX + 30, TF.BTN_Y + 10), n=1)
            lift()
        used += 12_000_000                                  # rough: the taps above
    lift()
    aB = audio(n0)
    pB = ((d.bus.read(POSW, 4) - p0) & 0xFFFFFFFF) / max(0.1, len(aB) / P.FS)
    full, draws, defer = v("tfx_full") - f0, v("tfx_draws") - dr0, v("tfx_defer") - df0
    ms = (mem.r32(MSCLK) - ms0) & 0xFFFFFFFF
    rB = float(np.sqrt(np.mean(aB ** 2)))
    print(f"[B abuse] {len(aB) / P.FS:.2f}s out, rms {rB:.0f}, peak {np.abs(aB).max():.0f}, "
          f"play {pB:.1f} blocks/s (ref {pA:.1f}); {len(words)} different words; "
          f"presents {draws}, full repaints {full}, put off {defer}, deck clock {ms} ms "
          f"[{time.time() - t0:.0f}s]", flush=True)
    chk(f"effects were switched {sorted(seen)} and the word followed the finger", len(words) > 20 and len(seen) >= min(3, len(set(effs))))
    chk(f"repaints limited: {full} in {ms} ms (max {2 * (ms // 60 + 2)})", full <= 2 * (ms // 60 + 2))
    chk("output bounded under abuse (< 3x reference)", rB < 3 * rA and np.isfinite(rB))
    chk("output not dead under abuse", rB > 0.05 * rA)
    chk(f"play rate kept in B ({pB:.1f} vs {pA:.1f})", abs(pB - pA) <= 0.05 * pA + 1)

    run(20_000_000)
    rC, pC = quiet("C lifted", 1.0)
    chk("word off after the lift", mem.r32(TF.KEYWORD) == TF.WORD_OFF and d.bus.read(BD8, 4) == TF.WORD_OFF)
    chk("audio back to the reference level", 0.5 * rA < rC < 2 * rA)
    chk(f"play rate kept in C ({pC:.1f} vs {pA:.1f})", abs(pC - pA) <= 0.05 * pA + 1)
    f1 = v("tfx_full")
    fb_png(m, "screenshots/touchfx/stress_after.png")
    v1 = v("tfx_ver")
    run(60_000_000)
    print(f"  after settle: full +{v('tfx_full') - f1}, ver {v1} -> {v('tfx_ver')}, why {v('tfx_why'):#x}, "
          f"hold {v('tfx_hold')} on {v('tfx_on')} touching {v('tfx_touching')} active {v('tfx_active')} "
          f"eff {v('tfx_eff')} x {v('tfx_x')} y {v('tfx_y')} depth {v('tfx_depth')}")
    chk("screen settled: nothing left to repaint", v("tfx_full") == f1)
    chk("panel still open and alive", v("tfx_mode") == 1 and mem.r32(PP.MODEW) == 7)
    chk("DSP no error", d.error is None)
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
