#!/usr/bin/env python3
"""qemu_fps_probe.py -- how many screen updates per second reach the app from
QEMU, idle and while a track plays, plus QEMU's CPU use. Read-only.

    .venv/bin/python tools/qemu_fps_probe.py TRACK
"""
import os, sys, tempfile, threading, time, subprocess
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_QEMU_AUDIO", "none")
from emu import qemu_app   # noqa: E402

SRC = sys.argv[1]
tmp = tempfile.mkdtemp(prefix="fps_")
stick = os.path.join(tmp, "stick"); os.makedirs(stick)
os.symlink(os.path.abspath(SRC), os.path.join(stick, os.path.basename(SRC)))
app = qemu_app.QemuApp(headless=True); app.view = "screen"; app.usb_path = stick
alive = True
def ticker():
    t = time.time()
    while alive:
        now = time.time()
        try: app.tick(now - t)
        except Exception as e: print("tick:", e)
        t = now; time.sleep(1 / 60)
threading.Thread(target=ticker, daemon=True).start()

def key(n, hold=0.15, wait=1.0):
    app.st.held.add(n); time.sleep(hold); app.st.held.discard(n); time.sleep(wait)
def tap(x, y, wait=1.0):
    app._touch_px = (x, y); time.sleep(0.15); app._touch_px = None; time.sleep(wait)
def mode():
    try: return app.read32(0x0B6C4264)
    except Exception: return None
def qcpu():
    p = getattr(app, "proc", None)
    if not p: return None
    o = subprocess.run(["top", "-l", "2", "-s", "1", "-pid", str(p.pid), "-stats", "cpu,th"],
                       capture_output=True, text=True).stdout.strip().splitlines()
    try:
        c, th = o[-1].split()[:2]
        return [float(c), th]
    except (ValueError, IndexError):
        return None
def rate(label, secs=10):
    s0, t0 = app.rfb.seq, time.time(); cpus = []
    while time.time() - t0 < secs:
        c = qcpu(); cpus.append(c) if c else time.sleep(1)
    n = app.rfb.seq - s0
    cpu = [c[0] for c in cpus]
    print(f"{label}: {n / (time.time() - t0):.1f} screen updates/s, qemu cpu % {round(sum(cpu)/max(1,len(cpu)))} (threads {cpus[-1][1] if cpus else '?'})", flush=True)

app._boot("Original", "qemu", None)
t0 = time.time()
while (app.rfb is None or app.rfb.seq < 3) and time.time() - t0 < 120: time.sleep(1)
time.sleep(30)
rate("idle home screen")
tap(162, 38, wait=2.5)
for i in range(3):
    key("RotaryPush", wait=2.0)
    if mode() in (2, 3, 5): break
t1 = time.time()
while mode() not in (3, 5) and time.time() - t1 < 40: time.sleep(0.5)
print("deck mode after load", mode(), flush=True)
time.sleep(5)
rate("track playing", 15)
alive = False; app.close()
