#!/usr/bin/env python3
"""hotcue8_jump_emu.py -- do hot cue pads D-H really JUMP to D-H?

Our emulator with the real audio-chip program. Stock hot cue pads only jump
after CALL (on-screen, 576,264), so the test taps CALL first, like a DJ who
loads a track and calls its cues. Then, with the module from this tree:

  1. page 1 pad 1 -> A (5 s)               (stock behaviour, unchanged)
  2. swipe to page 2, pad 1 -> D (29 s)    (1.0.9 report: played A here,
                                            because pad 1 was used once)
  3. page 2 pad 2 -> E (37 s)              (pad 2 never used before)
  4. swipe back to page 1, pad 1 -> A      (A comes back after D was used)
  5. taps on the HOT CUE tab page forward: F, H, then A again

    .venv/bin/python tools/hotcue8_jump_emu.py [FIXTURE]

FIXTURE = a loaded-track snapshot of the 8-cue mini stick (tools/hotcue8_emu.py,
default $TMPDIR/hc_s0/hc_rb_s0_loaded.pkl: cues written the way rekordbox
writes them). A pass needs the jump within 0.6 s of the cue.
"""
import os
import sys

sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402
import hotcue8_test as T                 # noqa: E402

FIX = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "hc_s0", "hc_rb_s0_loaded.pkl")
POS, PADST = 0x0C0E6D18, 0x0B6C3C78
ROW = 264
R8_X0, R8_P, R8_W = 116, 63, 58


def main():
    code, syms = T.build()
    m = Machine.load(FIX)
    mem, dev = m.mem, m.mem.dev
    PP.install(mem)
    T.install_hc(mem, code, syms)
    mem.w8(MS.MSOFF, 0)
    run = lambda n: m.cpu.run(limit=n)          # noqa: E731

    def tap(x, y, hold=150_000_000, after=150_000_000):
        dev.panel_frame = panl.build_frame(touch=(x, y)); run(hold)
        dev.panel_frame = panl.build_frame(); run(after)

    def swipe(direction):
        tab = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2)
        xs = (tab[0] + 30, tab[0], tab[0] - 30)
        for x in (xs if direction > 0 else xs[::-1]):
            dev.panel_frame = panl.build_frame(touch=(x, tab[1])); run(8_000_000)
        dev.panel_frame = panl.build_frame(); run(60_000_000)

    run(20_000_000)
    tap(709, 37, after=30_000_000)             # PERFORM
    run(250_000_000)
    tap(576, ROW)                              # CALL
    ok, res = True, []

    def press(label, x, want):
        nonlocal ok
        dev.panel_frame = panl.build_frame(touch=(x, ROW)); run(150_000_000)
        p = mem.r32(POS) / 150.0
        dev.panel_frame = panl.build_frame(); run(150_000_000)
        good = -0.2 < p - want < 0.6
        st = [mem.r32(PADST + 4 * k) for k in range(3)]
        print(f"{label:28s} want {want:5.1f} s  got {p:6.2f} s  page {mem.r8(PP.HCPAGE)}  "
              f"pad states {st}  {'ok' if good else 'WRONG'}", flush=True)
        ok &= good
        res.append(good)

    press("page 1 pad 1 (A)", 159, 5)
    press("page 1 pad 2 (B)", 263, 13)
    swipe(+1)
    press("page 2 pad 1 (D)", 159, 29)
    press("page 2 pad 2 (E)", 263, 37)
    press("page 2 pad 1 again (D)", 159, 29)
    swipe(-1)
    press("back on page 1 pad 1 (A)", 159, 5)
    press("back on page 1 pad 2 (B)", 263, 13)
    tab = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2)
    # a TAP on the HOT CUE tab pages forward (2026-11-12; no 8-pad grid)
    tap(*tab, hold=20_000_000, after=150_000_000)     # page 1 -> 2
    press("tap -> page 2 pad 3 (F)", 367, 45)
    tap(*tab, hold=20_000_000, after=150_000_000)     # page 2 -> 3
    press("tap -> page 3 pad 2 (H)", 263, 61)
    tap(*tab, hold=20_000_000, after=150_000_000)     # page 3 -> 1
    press("tap -> page 1 pad 1 (A)", 159, 5)
    print("RESULT:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
