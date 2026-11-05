#!/usr/bin/env python3
"""app_speed_bench.py -- how fast does the STUDIO's own emulation run?

Boots emu.ui.EmuApp(headless=True) exactly like studio/emuhost.py does,
lets the app's default mod set arm itself (themesw, lighttext, stems,
phasemeter, hotcue8, ...), then measures the app's _emu_loop for a fixed
block of emulated LCDC frames: wall seconds, SH4 steps, frames/s,
steps/s, and how long one MenuUtility press takes to open/close UTILITY
(the user-visible "the app is slow" symptom). XDJ_EV_C selects the event
loop: unset/1 = C event loop (the app default in play mode), 0 = the old
Python event loop. XDJ_DECK_SOUND=0 keeps it silent.
"""
import os
import sys
import time
sys.path[:0] = [".", "tools", "mods"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")

import emu.ui as emu_ui                      # noqa: E402
from emu.machine import Machine             # noqa: E402

MODE = 0x0BA371D4


def main():
    ev = os.environ.get("XDJ_EV_C", "1(default)")
    m = Machine.load(os.environ.get(
        "SNAP", "cache/emu_dsp_playing7.pkl"))
    app = emu_ui.EmuApp(headless=True)
    app._start(m, "bench")                    # same emu thread the studio gets
    cpu, dev = m.cpu, m.mem.dev
    print(f"boot ok wave_mode={app.wave_mode} ev_c={ev}", flush=True)
    time.sleep(1.0)

    # -- steady-state pace: time a fixed block of emulated frames
    f0, s0 = getattr(dev, "frames", 0), cpu.steps
    t0 = time.time()
    while getattr(dev, "frames", 0) - f0 < 300 and time.time() - t0 < 240:
        time.sleep(0.2)
    dt = time.time() - t0
    df = getattr(dev, "frames", 0) - f0
    ds = cpu.steps - s0
    fps = df / dt if dt > 0 else 0
    print(f"pace: {df} frames in {dt:.1f}s -> {fps:.1f} fps (deck=60); "
          f"{ds:,} steps -> {ds / dt / 1e6:.2f}M steps/s", flush=True)

    # -- the user symptom: one MENU press, how long until UTILITY opens
    t0 = time.time()
    app.st.held.add("MenuUtility")          # deck key press (momentary)
    opened = None
    while time.time() - t0 < 120:
        app.tick(0.05)                       # studio render-loop duty
        if m.mem.r32(MODE) == 6:
            opened = time.time() - t0
            break
        time.sleep(0.05)
    app.st.held.discard("MenuUtility")
    print(f"menu open: {'%.1f s' % opened if opened else 'NEVER (120s timeout)'}",
          flush=True)
    # close it again so the bench ends clean
    try:
        app.st.held.add("MenuUtility")
        for _ in range(60):
            app.tick(0.05)
            time.sleep(0.05)
        app.st.held.discard("MenuUtility")
    except Exception:
        pass
    try:
        app.close()
    except Exception:
        pass
    print("BENCH DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
