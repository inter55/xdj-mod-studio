#!/usr/bin/env python3
"""light_ev_probe.py -- isolate what breaks the UTILITY open under the C
event loop. _install_themesw (the base light flow's first install) is
suspect: phase-2 hold above left the SH at the 0x8d6a68x poll loop.
This version installs themesw UP FRONT (exactly like the base flow,
before any run), optionally lighttext (WITH_LT=1), then warms up on the
main screen and holds MenuUtility for 200M: on the deck that must open
UTILITY (mode 6), which is what the harness measures.
"""
import os
import sys
sys.path[:0] = [".", "tools", "mods"]
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
from emu.ui import EmuApp                # noqa: E402
import utility as U                      # noqa: E402

MODE = 0x0BA371D4


def hold_and_report(m, c, label, n=200_000_000):
    mem, dev = m.mem, m.mem.dev
    t0 = __import__("time").time()
    dev.panel_frame = panl.build_frame(keys=["MenuUtility"])
    c.run(limit=n)
    dt = __import__("time").time() - t0
    print(f"{label}: dt={dt:.1f}s mode={mem.r32(MODE):#x} pc={c.pc:#010x} "
          f"frames={getattr(dev, 'panel_rx_frames', 0)}", flush=True)


def main():
    m = Machine.load("cache/emu_dsp_playing7.pkl")
    mem, dev, c = m.mem, m.mem.dev, m.cpu
    if os.environ.get("SKIP_TS") != "1":
        EmuApp._install_themesw(None, m)
        print("themesw installed first (base-flow order)", flush=True)
    U.install(mem)
    if os.environ.get("WITH_LT"):
        from light_emu_test import install_lt
        install_lt(mem, __import__("theme_switch").default_names()
                   .index("light") + 1)
        print("lighttext installed too", flush=True)
    dev.panel_frame = panl.build_frame()
    c.run(limit=30_000_000)
    print(f"warmup mode={mem.r32(MODE):#x} pc={c.pc:#010x}", flush=True)
    hold_and_report(m, c, "hold1")
    # a short exit press, then a second hold from wherever we are
    dev.panel_frame = panl.build_frame(keys=["MenuUtility"])
    c.run(limit=12_000_000)
    dev.panel_frame = panl.build_frame()
    c.run(limit=30_000_000)
    print(f"after exit-press mode={mem.r32(MODE):#x}", flush=True)
    hold_and_report(m, c, "hold2")
    evc = getattr(dev, "_evc", None)
    if evc is not None:
        print(f"C ev: scif_rx={tuple(int(evc.scif_rx[i]) for i in range(3))}",
              flush=True)


if __name__ == "__main__":
    main()
