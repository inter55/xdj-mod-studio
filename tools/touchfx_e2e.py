#!/usr/bin/env python3
"""touchfx_e2e.py -- EMULATOR ONLY, end to end: a finger on the TOUCH FX pad
changes the real DSP's audio output.

    .venv/bin/python tools/touchfx_e2e.py [SNAP]

SNAP (default cache/emu_fx_playing.pkl) = the real DSP playing a track with
the TOUCH FX engine hot-installed the deck way (keyshift_hot_emu.py with
KSH_DIR=build/fx, then dsp_play_test.py). This test then does what a DJ
does, through the native touch path only:
  MASTER TEMPO on, PERFORM button, (PERFORM pads + TOUCH FX panel in RAM,
  the engine's 2-byte store NOP), record A = panel closed,
  tap TOUCH FX, hold a finger low-left on the pad (FILTER: low cutoff),
  record B, lift, record C.
PASS = B's energy above 4 kHz < 30 % of A's, C back above 60 % of A,
mailbox BD8 == the panel's word during B and 0x00100000 after the lift,
both channels alive, play position advancing at the same rate, no DSP error.
"""
import os
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

BD8 = 0x11837BD8
POSW = 0x11837CCC


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_fx_playing.pkl"
    secs = float(os.environ.get("SECS", "2.5"))
    t0 = time.time()
    m = Machine.load(snap)
    mem, c, d = m.mem, m.cpu, m.mem.dev.dspemu
    assert d is not None, "snapshot has no real DSP"
    ok = True
    os.makedirs("screenshots/touchfx", exist_ok=True)
    from ghosttap_emu import fb_png

    def chk(label, good):
        nonlocal ok
        print(("  ok   " if good else "CHECK FAIL: ") + label, flush=True)
        ok &= bool(good)

    def run(n, frame=None, keys=()):
        mem.dev.panel_frame = frame or panl.build_frame(keys=list(keys))
        c.run(limit=n)

    chk("engine call word live", d.bus.read(0xC0008D64, 4) == 0x00ACEC10)
    F.mt_on(m)
    chk("MASTER TEMPO on", mem.r8(TF.MTW) == 1)
    PP.install(mem)
    s = TF.install(mem)
    mem.w16(TF.NOPSITE, 0x0009)

    def v(name):
        return mem.r32(s[name])

    def last():
        n = v("tfx_logn")
        return mem.r32(s["tfx_log"] + 4 * ((n - 1) & 31)) if n else 0

    def press(xy, n=6):
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

    # PERFORM button (top row, native touch)
    for _ in range(3):
        if mem.r32(PP.MODEW) == 7:
            break
        run(3_000_000, panl.build_frame(touch=(705, 37)))
        run(8_000_000)
    chk("PERFORM screen", mem.r32(PP.MODEW) == 7)

    def record(tag, frame_fn=None):
        n0 = len(d.asp1.out)
        p0 = d.bus.read(POSW, 4)
        steps = int(secs * P.SH_STEPS_PER_S)
        bd = set()
        while steps > 0:
            k = min(steps, 50_000_000)
            run(k, frame_fn() if frame_fn else None)
            bd.add(d.bus.read(BD8, 4))
            steps -= k
        a = np.array(d.asp1.out[n0:], dtype=np.uint32).view(np.int32)
        a = (a[: len(a) // 2 * 2].reshape(-1, 2) >> 16).astype(float)
        p = (d.bus.read(POSW, 4) - p0) & 0xFFFFFFFF
        hi = F.hi_ratio(a.mean(1))
        rl, rr = (float(np.sqrt(np.mean(a[:, k] ** 2))) for k in (0, 1))
        print(f"[{tag}] {len(a) / P.FS:.2f}s out, >4k {hi:.4f}, rms L/R {rl:.0f}/{rr:.0f}, "
              f"play position +{p} blocks, BD8 {sorted(hex(x) for x in bd)} "
              f"[{time.time() - t0:.0f}s]", flush=True)
        return hi, rl, rr, p, bd

    hA, lA, rA, pA, _ = record("A closed")
    # open the panel, hold a finger low-left on the pad (FILTER, low cutoff)
    press(TF.TAB)
    lift()
    chk("panel open", v("tfx_mode") == 1)
    xy = (TF.PAD[0] + 20, TF.PAD[3] - 30)
    chk("finger reached the hook", press(xy))
    w = TF.word(0, v("tfx_x"), v("tfx_y"), v("tfx_depth"))
    chk(f"panel word {w:#010x} active", v("tfx_active") == 1 and mem.r32(TF.KEYWORD) == w)
    run(20_000_000, panl.build_frame(touch=xy))      # word -> mailbox -> DSP
    hB, lB, rB, pB, bdB = record("B finger on pad (FILTER low)",
                                 lambda: panl.build_frame(touch=xy))
    fb_png(m, "screenshots/touchfx/e2e_finger.png")
    lift()
    run(20_000_000)
    hC, lC, rC, pC, bdC = record("C lifted")
    chk("mailbox carried the panel word", w in bdB)
    chk("mailbox off after the lift", bdC == {TF.WORD_OFF})
    chk(f"filter audible: >4k {hB / hA * 100:.0f}% of closed (< 30%)", hB < 0.30 * hA)
    chk(f"back after lift: >4k {hC / hA * 100:.0f}% of closed (> 60%)", hC > 0.60 * hA)
    chk("both channels alive", min(lA, rA, lB, rB, lC, rC) > 50)
    chk("L/R balanced under FX", min(lB, rB) / max(lB, rB) > 0.8 * min(lA, rA) / max(lA, rA))
    chk(f"play position rate kept ({pA}/{pB}/{pC})",
        all(abs(p - pA) <= max(2, pA * 0.03) for p in (pB, pC)))
    chk("DSP no error", d.error is None)
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
