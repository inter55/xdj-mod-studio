#!/usr/bin/env python3
"""stems_qemu_test.py -- live stem mute on the REAL-TIME emulator (QEMU,
real audio chip at its true speed), with the deck's sound recorded to a WAV.

Why: our own emulator runs below real time and treats host-port writes to
the audio chip as free; on the deck (2026-11-03) the fast mute rewrote ~35 s
of audio yet the change was heard ~30 s late, and playback stuttered.

Steps: boot "With mods" (stems + pads), the stick = a folder with one STEMS
file, BROWSE > load it, it plays, open PERFORM > STEMS, tap VOCALS, record.
Then: when did the vocals go (vocal gain per 0.25 s, fitted against the
file's own planes), and did the music stutter (alignment drift / repeats).

  .venv/bin/python tools/stems_qemu_test.py [STEMSFILE]
  env SHOTS=dir (step screenshots), MUTE_AT=secs after play (default 12),
      AFTER=secs recorded after the tap (default 20)
"""
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import wave

import numpy as np

sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_QEMU_EXTRAS", "stems")

SRC = sys.argv[1] if len(sys.argv) > 1 else "dist/deck-tests/Get Lucky ESQUIRE (STEMS).wav"
SHOTS = os.environ.get("SHOTS", "build/stems_qemu")
MUTE_AT = float(os.environ.get("MUTE_AT", "12"))
AFTER = float(os.environ.get("AFTER", "20"))
os.makedirs(SHOTS, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="stemsq_")
REC = os.path.join(tmp, "out.wav")
os.environ["XDJ_QEMU_AUDIO"] = f"wav,path={REC},timer-period=5000"
os.environ["XDJ_DECK_SOUND"] = "1"          # the deck's audio path stays on

from emu import qemu_app                     # noqa: E402
from emu import panl                         # noqa: E402
from PIL import Image                        # noqa: E402

stick = os.path.join(tmp, "stick")
os.makedirs(stick)
os.symlink(os.path.abspath(SRC), os.path.join(stick, os.path.basename(SRC)))

app = qemu_app.QemuApp(headless=True)
app.view = "screen"
app.usb_path = stick
alive = True


def ticker():
    t = time.time()
    while alive:
        now = time.time()
        try:
            app.tick(now - t)
        except Exception as e:                 # noqa: BLE001
            print("tick:", e, flush=True)
        t = now
        time.sleep(1 / 60)


def shot(name):
    r = app.rfb
    if r is None:
        return
    with r.lock:
        img = r.fb[:480, :800].copy()
    Image.fromarray(img).save(os.path.join(SHOTS, name + ".png"))


def key(name, hold=0.15, wait=1.0):
    app.st.held.add(name)                     # panl key name (RotaryPush ...)
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
    except Exception:                          # noqa: BLE001
        return None


def syms(p):
    d = {}
    for line in open(p):
        a, _, n = line.split()
        d[n.lstrip("_")] = int(a, 16)
    return d


def main():
    global alive
    threading.Thread(target=ticker, daemon=True).start()
    app._boot("With mods", "qemu-mods", None)
    t0 = time.time()
    while (app.rfb is None or app.rfb.seq < 3) and time.time() - t0 < 120:
        time.sleep(1)
    print(f"booted in {time.time() - t0:.0f} s; status '{app.status}'", flush=True)
    time.sleep(40)                             # home screen + stick mount
    shot("01_home")
    tap(162, 38, wait=2.5)                     # BROWSE (on-screen button)
    shot("02_browse")
    for i in range(3):                          # into the folder / onto the file
        key("RotaryPush", wait=2.0)
        shot(f"03_push{i}")
        if mode() in (2, 3, 5):
            break
    t1 = time.time()
    while mode() not in (3, 5) and time.time() - t1 < 40:
        time.sleep(0.5)
    print("deck mode after load", mode(), flush=True)
    shot("04_loaded")
    s = syms("build/stems/sm.syms")
    import perform_pads as PP
    time.sleep(3)
    print("stems file valid:", app.read32(s["sm_valid"]), flush=True)
    tap(718, 38, wait=2.0)                      # PERFORM
    shot("05_perform")
    # the STEMS tab: the tab stack's last row (KEY SHIFT hidden -> shifted up)
    for y in PP.TAB_Y[::-1]:
        tap(PP.TAB_X + PP.TAB_W // 2, y + PP.TAB_H // 2, wait=1.0)
        if app.read32(PP.PST) & 0xFF == 3 or (app.read32(PP.PST) >> 16) & 0xFF == 3:
            break
    shot("06_stems")
    play0 = time.time()
    if os.environ.get("STALE"):
        # the deck case: the track already ran a while, then jumped back --
        # the chip now holds an OLD decoded copy of the same moments too
        time.sleep(float(os.environ.get("STALE")))
        key("Cue", wait=1.0)                    # back to the cue point (start)
        key("PlayPause", wait=0.5)
        print("jumped back to the cue point and played on", flush=True)
    time.sleep(MUTE_AT)
    rec_bytes = os.path.getsize(REC)
    tap_t = time.time()
    tap(115 + 44, 232 + 32, hold=0.15, wait=0.1)   # VOCALS pad
    mask = app.read32(PP.PMASK) & 0xFF
    print(f"VOCALS tapped: mask {mask:#x}; wav bytes at tap {rec_bytes}", flush=True)
    shot("07_muted")
    c = {}
    t2 = time.time()
    while time.time() - t2 < AFTER:
        time.sleep(1)
    for k in ("polls", "parm", "drblk", "drscan", "drskip", "drbusy", "drnoslot",
              "drwfail", "drlastp", "drlastb", "remix", "remixb", "hits"):
        try:
            c[k] = app.read32(s["sm_" + k])
        except Exception:                      # noqa: BLE001
            pass
    print("counters", c, flush=True)
    alive = False
    app.close()
    time.sleep(1)
    analyse(rec_bytes)


def planes():
    import stems4
    return stems4.read(SRC) if hasattr(stems4, "read") else None


def analyse(tap_bytes):
    w = wave.open(REC)
    sr, ch = w.getframerate(), w.getnchannels()
    x = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, ch).astype(np.float32)
    mono = x.mean(1)
    tap_s = (tap_bytes - 44) / (2 * ch * sr)
    print(f"recording {len(mono) / sr:.1f} s at {sr} Hz; tap at {tap_s:.2f} s", flush=True)
    np.save(os.path.join(SHOTS, "rec.npy"), mono)
    shutil.copy(REC, os.path.join(SHOTS, "rec.wav"))
    open(os.path.join(SHOTS, "tap.txt"), "w").write(f"{tap_s} {sr}\n")


if __name__ == "__main__":
    try:
        main()
    finally:
        alive = False
        try:
            app.close()
        except Exception:                      # noqa: BLE001
            pass
