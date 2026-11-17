#!/usr/bin/env python3
"""hotcue8_recall_probe.py -- which cue time does each pad actually stage?

Claude's 2026-11-11 probes measured POS150 while the track played, so a pad
that did nothing still looked like a "jump". This reads the deck's STAGED
seek target instead: a stock CALL writes the resolved cue position into
0x0B6C0F10 + 0x14*k (1/150 s; cue A at 5.000 s = 0x2EE), so the value there
right after a press is what the deck decided to play.

Boots the WITH-MODS image in QEMU (real audio chip, stock firmware + the
8-pad view), loads the mini stick's track (cues A-H at 5, 13 .. 61 s),
taps HOT CUE so the 8-pad view is up, and presses every pad in turn.

  .venv/bin/python tools/hotcue8_recall_probe.py [STICK]

Read-only, emulator only. Exit 0 = every pad staged its own cue,
1 = at least one pad staged another cue (the hardware report).
"""
import os
import sys
import tempfile
import threading
import time

sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "1")
tmp = tempfile.mkdtemp(prefix="hcrp_")
os.environ.setdefault("XDJ_QEMU_AUDIO",
                      f"wav,path={os.path.join(tmp, 'out.wav')},timer-period=5000")
from emu import qemu_app                      # noqa: E402
import perform_pads as PP                     # noqa: E402

STICK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "hc_s0", "hotcue8stick")
TIMES = [5000 + 8000 * i for i in range(8)]
STAGE, STRIDE, NSTAGE = 0x0B6C0F10, 0x14, 4
HCPAGE = PP.HCPAGE           # page byte the pads module keeps
PAD_Y = 263
PAD_X0, PAD_P, PAD_W = 116, 63, 58
TAB = (16 + 92 // 2, 232 + 15 // 2)


def main():
    if not os.path.isdir(STICK):
        print(f"SKIP: {STICK} missing -- build it with tools/hotcue8_emu.py")
        return 0
    app = qemu_app.QemuApp(headless=True)
    app.view = "screen"
    app.usb_path = STICK
    alive = [True]

    def ticker():
        t = time.time()
        while alive[0]:
            now = time.time()
            try:
                app.tick(now - t)
            except Exception:                  # noqa: BLE001
                pass
            t = now
            time.sleep(1 / 60)

    threading.Thread(target=ticker, daemon=True).start()

    def key(name, hold=0.15, wait=1.0):
        app.st.held.add(name)
        time.sleep(hold)
        app.st.held.discard(name)
        time.sleep(wait)

    def tap(x, y, hold=0.15, wait=1.0):
        app._touch_px = (x, y)
        time.sleep(hold)
        app._touch_px = None
        time.sleep(wait)

    def mode():
        try:
            return app.read32(0x0B6C4264)
        except Exception:                       # noqa: BLE001
            return None

    def stage():
        return [app.read32(STAGE + STRIDE * k) for k in range(NSTAGE)]

    ok = True
    try:
        app._boot("Stock v1.15 + mods", "qemu-mods", None)
        t0 = time.time()
        while (app.rfb is None or app.rfb.seq < 3) and time.time() - t0 < 180:
            time.sleep(1)
        time.sleep(45)                          # mods image: a little longer
        tap(162, 38, wait=2.5)                  # BROWSE
        for _ in range(6):
            if mode() in (2, 3, 5):
                break
            key("RotaryPush", wait=2.0)
        t1 = time.time()
        while mode() not in (3, 5) and time.time() - t1 < 40:
            time.sleep(0.5)
        print("loaded, mode", mode(), flush=True)
        tap(718, 38, wait=2.0)                  # PERFORM
        tap(*TAB, wait=1.5)                     # HOT CUE tab -> 8-pad view
        page = app.read32(HCPAGE)
        print(f"8-pad view: page byte {page} (0 = page 1, the view)", flush=True)
        # Park the deck: while it plays the words below just follow the
        # playhead and no pad can be told apart. Paused, a hot cue lands on
        # the cue and stays there, so they are an exact per-pad target.
        a0 = app.read32(STAGE)
        time.sleep(0.6)
        if app.read32(STAGE) != a0:             # playing: pause (panel key)
            key("PlayPause", wait=1.2)
        a1 = app.read32(STAGE)
        time.sleep(0.6)
        if app.read32(STAGE) != a1:
            key("PlayPause", wait=1.2)
        parked = app.read32(STAGE)
        time.sleep(0.6)
        print(f"parked at {parked} (playing: {app.read32(STAGE) != parked})", flush=True)

        for i in range(8):
            x = PAD_X0 + PAD_P * i + PAD_W // 2
            before = stage()
            app._touch_px = (x, PAD_Y)
            time.sleep(0.45)
            held = stage()
            app._touch_px = None
            time.sleep(0.8)
            want = TIMES[i] * 3 // 20           # ms -> 1/150 s
            print(f"    held {held}  released {stage()}", flush=True)
            got = [v for v in held if v not in (None, 0)]
            match = [v for v in got if v == want]
            def near(v):
                j = min(range(8), key=lambda q: abs(TIMES[q] * 3 // 20 - v))
                return 'ABCDEFGH'[j] if abs(TIMES[j] * 3 // 20 - v) < 40 else '?'
            who = ",".join(f"{v}->{near(v)}" for v in sorted(set(got)))
            good = bool(match)
            ok &= good
            print(f"pad {'ABCDEFGH'[i]} want {want:5d} ({TIMES[i]/1000:5.2f}s)  "
                  f"staged {held}  [{who}]  {'ok' if good else 'WRONG'}", flush=True)
    finally:
        alive[0] = False
        app.close()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
