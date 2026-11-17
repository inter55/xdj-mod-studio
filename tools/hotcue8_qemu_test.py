#!/usr/bin/env python3
"""hotcue8_qemu_test.py -- do hot cue pads D-H really JUMP to D-H?

On the real-time emulator (QEMU, real audio chip), not ours: our emulator
never performs a hot cue jump at all (a stock pad A press does nothing
there either), so it can only check which cue sits in which slot, not what
the deck plays. Report on 1.0.9 (Reddit, 2026-11-11): "D-H are shown, but
D,E,F,G,H always play A,B,C, with the gate on or off".

Steps: boot "With mods" with the hot cue pages, stick = the mini stick from
tools/hotcue8_emu.py (one track, cues A-H at 5, 13 .. 61 s, written the way
rekordbox writes them), load it, then for each case press a pad and read
the deck's own position word (0x0C0E6D18, 1/150 s) right after:

  page 1 pad 1 -> A (5 s)   page 1 pad 2 -> B (13 s)
  page 2 pad 1 -> D (29 s)  page 2 pad 2 -> E (37 s)
  8-pad view pad 4 -> D (29 s), pad 8 -> H (61 s)

  .venv/bin/python tools/hotcue8_qemu_test.py [STICK]
  env SHOTS=dir (screenshots)
"""
import os
import sys
import tempfile
import threading
import time

sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_QEMU_EXTRAS", "hotcue8")
os.environ.setdefault("XDJ_DECK_SOUND", "1")
tmp = tempfile.mkdtemp(prefix="hcq_")
os.environ.setdefault("XDJ_QEMU_AUDIO", f"wav,path={os.path.join(tmp, 'out.wav')},timer-period=5000")

from emu import qemu_app                     # noqa: E402
from PIL import Image                        # noqa: E402
import perform_pads as PP                    # noqa: E402

STICK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "hc_s0", "hotcue8stick")
SHOTS = os.environ.get("SHOTS", "build/hotcue8_qemu")
os.makedirs(SHOTS, exist_ok=True)
TIMES = [5 + 8 * i for i in range(8)]
POS = 0x0C0E6D18
R8_X0, R8_P, R8_W = 116, 63, 58

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
    app.st.held.add(name)
    time.sleep(hold)
    app.st.held.discard(name)
    time.sleep(wait)


def tap(x, y, hold=0.15, wait=1.0):
    app._touch_px = (x, y)
    time.sleep(hold)
    app._touch_px = None
    time.sleep(wait)


def rd(a):
    try:
        return app.read32(a)
    except Exception:                          # noqa: BLE001
        return None


def pos():
    v = rd(POS)
    return None if v is None else v / 150.0


def mode():
    return rd(0x0B6C4264)


def main():
    global alive
    threading.Thread(target=ticker, daemon=True).start()
    app._boot("With mods", "qemu-mods", None)
    t0 = time.time()
    while (app.rfb is None or app.rfb.seq < 3) and time.time() - t0 < 120:
        time.sleep(1)
    print(f"booted in {time.time() - t0:.0f} s; status '{app.status}'", flush=True)
    time.sleep(40)
    shot("01_home")
    tap(162, 38, wait=2.5)                     # BROWSE
    shot("02_browse")
    # folder view: [FOLDER] -> Contents -> artist -> album -> the track
    for i in range(6):
        if mode() in (2, 3, 5):
            break
        key("RotaryPush", wait=2.0)
        shot(f"03_push{i}")
    t1 = time.time()
    while mode() not in (3, 5) and time.time() - t1 < 40:
        time.sleep(0.5)
    print("deck mode after load", mode(), "pos", pos(), flush=True)
    shot("04_loaded")
    def moving():
        a = pos(); time.sleep(1.5); b = pos()
        print(f"  mode {mode()} pos {a} -> {b}", flush=True)
        return a is not None and b is not None and b - a > 0.5
    # QEMU: the deck may auto-play after a load or wait cued; make it PLAY
    for _ in range(4):
        if moving():
            break
        key("PlayPause", wait=1.5)
    print("playing:", moving(), flush=True)
    tap(718, 38, wait=2.0)                     # PERFORM
    shot("05_perform")
    hcs = PP.HC_BASE                           # hc.syms not needed: page byte is PP's
    ok = True
    res = []

    HOLD = float(os.environ.get("HOLD", "0.5"))

    def press(label, xy, want, hold=None):
        hold = hold or HOLD
        nonlocal ok
        before = pos()
        app._touch_px = xy
        time.sleep(hold)
        during = pos()
        kb = b"".join((rd(0x0B6C7A28 + 84 + 4 * i) or 0).to_bytes(4, "little") for i in range(6)).hex()
        print(f"   held: zone {rd(0x0C0E75BC)} keys {kb} PST {rd(PP.PST):#x}", flush=True)
        slots = []
        for k in range(3):
            s = 0x0B6C4118 + k * 0x40
            w = rd(s + 8) or 0
            slots.append((w & 0xFFFF, w >> 16, rd(s + 0x0C)))
        app._touch_px = None
        time.sleep(0.6)
        after = pos()
        good = during is not None and -0.2 < during - want < hold + 0.6
        print(f"{label:28s} want {want:5.1f}s  before {before:7.2f}  held {during:7.2f}  "
              f"after {after:7.2f}  page {rd(PP.HCPAGE) & 0xFF}  slots {slots}  "
              f"{'ok' if good else 'WRONG'}", flush=True)
        res.append((label, good))
        ok &= good
        time.sleep(1.0)

    if os.environ.get("QOFF"):
        tap(675, 259, wait=1.0)                # on-screen QUANTIZE: off
        shot("05b_qoff")
    row_y = 264
    if os.environ.get("CALL"):                 # stock CALL: load the stored cues
        tap(576, row_y, hold=0.3, wait=1.5)
        shot("05c_call")
        print("after CALL: slot states",
              [hex((rd(0x0B6C4118 + k * 0x40 + 8) or 0) & 0xFFFF) for k in range(3)], flush=True)
    press("page 1 pad 1 (A)", (159, row_y), TIMES[0])
    press("page 1 pad 2 (B)", (263, row_y), TIMES[1])
    shot("06_page1")
    tab = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2)
    for x in (tab[0] + 30, tab[0], tab[0] - 30):        # swipe left = next page
        app._touch_px = (x, tab[1])
        time.sleep(0.12)
    app._touch_px = None
    time.sleep(1.5)
    print("page byte after swipe:", (rd(PP.HCPAGE) or 0) & 0xFF, flush=True)
    shot("07_page2")
    press("page 2 pad 1 (D)", (159, row_y), TIMES[3])
    press("page 2 pad 2 (E)", (263, row_y), TIMES[4])
    for x in (tab[0] - 30, tab[0], tab[0] + 30):        # back to page 1
        app._touch_px = (x, tab[1])
        time.sleep(0.12)
    app._touch_px = None
    time.sleep(1.5)
    tap(*tab, wait=1.5)                                  # 8-pad view
    shot("08_grid")
    press("8-pad view pad D", (R8_X0 + 3 * R8_P + R8_W // 2, row_y), TIMES[3])
    press("8-pad view pad H", (R8_X0 + 7 * R8_P + R8_W // 2, row_y), TIMES[7])
    press("8-pad view pad A", (R8_X0 + 0 * R8_P + R8_W // 2, row_y), TIMES[0])
    shot("09_after")
    print("RESULT:", "PASS" if ok else "FAIL", flush=True)
    alive = False
    app.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
