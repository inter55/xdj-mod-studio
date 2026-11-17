#!/usr/bin/env python3
"""hotcue_call_diff.py -- what does the deck's CALL button write?

Hot cue pads only jump after stock CALL (QEMU, real audio chip, stock
firmware); paging the slots, both pad models and the two 16-byte cue tables
still left pad 1 jumping to A. So the jump source is whatever CALL fills.
This boots the original firmware in QEMU, loads the mini stick's track
(cues A-H at 5, 13 .. 61 s), dumps all main RAM (pmemsave) before and after
CALL, and lists every changed 4-byte word holding a cue time in ms, 1/150 s
or 1/75 s, in either byte order, plus whole changed regions.

  .venv/bin/python tools/hotcue_call_diff.py [STICK]
"""
import os
import sys
import tempfile
import threading
import time

import numpy as np

sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "1")
tmp = tempfile.mkdtemp(prefix="hccall_")
os.environ.setdefault("XDJ_QEMU_AUDIO", f"wav,path={os.path.join(tmp, 'out.wav')},timer-period=5000")
from emu import qemu_app                      # noqa: E402

STICK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "hc_s0", "hotcue8stick")
OUT = os.environ.get("OUT", "build/hotcue_call")
os.makedirs(OUT, exist_ok=True)
TIMES = [5000 + 8000 * i for i in range(8)]
LO, N = 0x08000000, 0x08000000

app = qemu_app.QemuApp(headless=True)
app.view = "screen"
app.usb_path = STICK
alive = True


def ticker():
    t = time.time()
    while alive:
        now = time.time()
        try:
            app.tick(now - t)
        except Exception as e:                  # noqa: BLE001
            print("tick:", e, flush=True)
        t = now
        time.sleep(1 / 60)


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
    except Exception:                           # noqa: BLE001
        return None


def dump(name):
    p = os.path.abspath(os.path.join(OUT, name + ".bin"))
    app.monitor("stop")
    print(app.monitor(f"pmemsave {LO:#x} {N:#x} \"{p}\"", timeout=60).strip()[-80:], flush=True)
    app.monitor("cont")
    t0 = time.time()
    while (not os.path.exists(p) or os.path.getsize(p) < N) and time.time() - t0 < 60:
        time.sleep(0.5)
    return np.fromfile(p, "<u4")


def main():
    global alive
    threading.Thread(target=ticker, daemon=True).start()
    app._boot("Original", "qemu", None)
    t0 = time.time()
    while (app.rfb is None or app.rfb.seq < 3) and time.time() - t0 < 120:
        time.sleep(1)
    time.sleep(40)
    tap(162, 38, wait=2.5)                      # BROWSE
    for _ in range(6):
        if mode() in (2, 3, 5):
            break
        key("RotaryPush", wait=2.0)
    t1 = time.time()
    while mode() not in (3, 5) and time.time() - t1 < 40:
        time.sleep(0.5)
    print("loaded, mode", mode(), flush=True)
    tap(718, 38, wait=2.0)                      # PERFORM
    a = dump("before")
    tap(576, 264, hold=0.3, wait=2.0)           # CALL
    b = dump("after")
    alive = False
    app.close()
    ch = np.nonzero(a != b)[0]
    print(f"{len(ch)} words changed", flush=True)
    vals = {}
    for i, t in enumerate(TIMES):
        for unit, v in (("ms", t), ("150", t * 150 // 1000), ("75", t * 75 // 1000)):
            vals[v] = f"{'ABCDEFGH'[i]}:{unit}"
            sw = int.from_bytes(int(v).to_bytes(4, "little"), "big")
            vals[sw] = f"{'ABCDEFGH'[i]}:{unit}:BE"
    hits = [(LO + 4 * int(i), int(b[i]), vals[int(b[i])]) for i in ch if int(b[i]) in vals]
    print(f"changed words now holding a cue time: {len(hits)}", flush=True)
    for addr, v, what in hits[:200]:
        print(f"  {addr:#010x} = {v:#x}  ({what})  was {int(a[(addr - LO) // 4]):#x}")
    # changed regions (runs), for context
    runs, s0, prev = [], None, None
    for i in ch:
        if prev is None or i > prev + 8:
            if s0 is not None:
                runs.append((s0, prev))
            s0 = i
        prev = i
    if s0 is not None:
        runs.append((s0, prev))
    print(f"{len(runs)} changed regions; the 60 largest:")
    for s, e in sorted(runs, key=lambda r: r[0] - r[1])[:60]:
        print(f"  {LO + 4 * s:#010x} .. {LO + 4 * e + 4:#010x}  ({(e - s + 1) * 4} B)")


if __name__ == "__main__":
    main()
