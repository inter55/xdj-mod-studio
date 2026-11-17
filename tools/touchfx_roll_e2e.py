#!/usr/bin/env python3
"""touchfx_roll_e2e.py -- EMULATOR ONLY: TOUCH FX ROLL drives the deck's own
beat loop, end to end, through the native touch path.

    .venv/bin/python tools/touchfx_roll_e2e.py [SNAP]

SNAP (default cache/emu_fx_playing.pkl) = the real audio chip playing a
track. Steps, like a DJ: PERFORM, TOUCH FX tab, ROLL button, hold a finger
on the left of the pad (1/2 beat), then further right (1 beat), lift.
The play position word (0x0C0E6D18, 1/150 s) is sampled every ~40 ms.

PASS =
  * while held the position jumps BACK at least twice and stays inside a
    window no longer than the loop (+ one sample);
  * moving the finger right gives a longer loop;
  * after the lift it only moves forward again (RELOOP/EXIT ended the loop)
    and the RELOOP/EXIT key bit is released;
  * the effect word sent to the audio chip stays OFF (ROLL needs no FX
    engine), tfx_rolls == 1 per hold, no DSP error.
"""
import os
import sys
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
import touchfx as TF                     # noqa: E402

POS = 0x0C0E6D18
KEYB = 0x0B6C7A28
SAMPLE = 20_000_000                      # SH steps per sample (~40 ms)


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_fx_playing.pkl"
    m = Machine.load(snap)
    mem, c, d = m.mem, m.cpu, m.mem.dev.dspemu
    assert d is not None, "snapshot has no real audio chip"
    ok = True

    def chk(label, good):
        nonlocal ok
        print(("  ok   " if good else "CHECK FAIL: ") + label, flush=True)
        ok &= bool(good)

    def run(n, xy=None):
        mem.dev.panel_frame = panl.build_frame(touch=xy) if xy else panl.build_frame()
        c.run(limit=n)

    PP.install(mem)
    s = TF.install(mem)
    v = lambda n: mem.r32(s[n])             # noqa: E731

    def tap(xy, hold=8_000_000, after=12_000_000):
        run(hold, xy)
        run(after)

    for _ in range(3):
        if mem.r32(PP.MODEW) == 7:
            break
        tap((705, 37), 3_000_000, 8_000_000)
    chk("PERFORM screen", mem.r32(PP.MODEW) == 7)
    tap(TF.TAB)
    chk("panel open", v("tfx_mode") == 1)
    roll = (530 + 62, 86 + 3 * 28 + 12)     # grid button 6 (left column, row 4)
    tap(roll)
    chk("ROLL selected", v("tfx_eff") == 6)

    def trace(n, xy=None):
        out = []
        for _ in range(n):
            run(SAMPLE, xy)
            out.append(mem.r32(POS) / 150.0)
        print("   ", " ".join(f"{p:.2f}" for p in out), flush=True)
        return out

    def loops(tr):
        back = sum(1 for a, b in zip(tr, tr[1:]) if b < a - 0.01)
        return back, max(tr) - min(tr)

    word0 = mem.r32(TF.KEYWORD)
    print("before:")
    pre = trace(6)
    print("held, 1/2 beat:")
    left = (TF.PAD[0] + 20, (TF.PAD[1] + TF.PAD[3]) // 2)
    h1 = trace(18, left)[3:]
    chk(f"roll engaged (rolls {v('tfx_rolls')}, roll {v('tfx_roll')})",
        v("tfx_roll") == 1 and v("tfx_rolls") == 1)
    b1, w1 = loops(h1)
    print("held, 1 beat:")
    right = (TF.PAD[0] + 150, left[1])
    h2 = trace(24, right)[6:]
    b2, w2 = loops(h2)
    print("lifted:")
    post = trace(20)
    b3, _ = loops(post[4:])
    chk(f"1/2 beat loops while held (back jumps {b1}, window {w1:.2f} s)", b1 >= 2 and w1 < 0.45)
    chk(f"1 beat loop is longer ({w2:.2f} s > {w1:.2f} s, back jumps {b2})", b2 >= 1 and w2 > w1 + 0.05)
    chk(f"plays on after the lift (back jumps {b3}, {post[4]:.2f} -> {post[-1]:.2f})",
        b3 == 0 and post[-1] - post[4] > 0.4)
    chk("RELOOP/EXIT released", v("tfx_rexit") == 0 and not mem.r8(KEYB + 86) & 0x10)
    chk(f"no FX word to the audio chip ({mem.r32(TF.KEYWORD):#x})",
        mem.r32(TF.KEYWORD) in (word0, TF.WORD_OFF) and v("tfx_active") == 0)
    chk("one roll per hold", v("tfx_rolls") == 1)
    chk("audio chip no error", d.error is None)
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
