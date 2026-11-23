#!/usr/bin/env python3
"""trackfilter_screen_emu.py -- the TRACK FILTER screen + header button in
the emulator, driven only by native touch on the user's real library.

    .venv/bin/python tools/trackfilter_screen_emu.py

Repeats on the 700 what was done on the XDJ-1000MK2's firmware in the
reference emulator (docs/TRACK_FILTER_1.1.md): hold the button -> the screen;
tick BPM, type 128, +-3 %; tick KEY, pick Am (MINOR row); the live [count]
must be 48 (the 1000MK2 showed 48 for exactly this on the same library);
close -> the TRACK list redoes itself with those 48; tap the button -> all
263; hold RESET -> nothing ticked, 130 / 6 / C / 3 stars. Screenshots in
screenshots/trackfilter/screen_*.png.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

import trackfilter as TF
from emu.machine import Machine
from emu import panl
from ghosttap_emu import fb_png

OUT = os.path.join(ROOT, "screenshots", "trackfilter")
os.makedirs(OUT, exist_ok=True)
m = Machine.load(os.path.join(ROOT, "build", "review_1110", "tl_before.pkl"))
mem, dev, c = m.mem, m.mem.dev, m.cpu
s = TF.install(mem)
v = lambda n: mem.r32(s[n])
ok = True


def chk(label, good):
    global ok
    print(("ok   " if good else "FAIL ") + label, flush=True)
    ok &= bool(good)


def run(n, frame=None):
    dev.panel_frame = frame or panl.build_frame(keys=[])
    c.run(limit=n)


def key(k):
    run(12_000_000, panl.build_frame(keys=[k]))
    run(25_000_000)


def press(xy, until=None, n=6, most=400):
    t0 = v("tfs_touches")
    for _ in range(most):
        run(250_000, panl.build_frame(touch=xy))
        if (until() if until else v("tfs_touches") - t0 >= n):
            return True
    print("  press did not land:", xy)
    return False


def lift():
    run(3_000_000)


def tap(xy):
    r = press(xy)
    lift()
    run(4_000_000)
    return r


def count():
    return mem.r16(TF.LEFTBUF + 2)


def store():
    return [TF.nat_get(mem, k) for k in range(1, 6)]


exc0 = m.exc_total
run(20_000_000)
key("RotaryPush")                          # open TRACK
run(150_000_000)
chk("TRACK list open, 263 rows", count() == 263)
fb_png(m, f"{OUT}/screen_0_list.png")

# hold the header button -> the screen
press((710, 90), until=lambda: v("tfs_mode") == 1, most=2000)
lift()
run(8_000_000)
chk("hold -> TRACK FILTER screen open", v("tfs_mode") == 1)
fb_png(m, f"{OUT}/screen_1_open.png")

tap((150, 85))                             # tick BPM
chk("BPM ticked", TF.nat_get(mem, 1) == 130)
tap((317, 85))                             # BPM value -> keypad
chk("BPM keypad open", v("tfs_edit") == 1)
for _ in range(3):
    tap((749, 348))                        # backspace
for d in (1, 2, 8):
    tap((40 + 66 * (d - 1) + 30, 348))
fb_png(m, f"{OUT}/screen_2_keypad.png")
tap((728, 440))                            # CLOSE
tap((580, 85))                             # +- % keypad
tap((40 + 66 * 2 + 30, 348))               # 3
tap((40 + 66 * 7 + 30, 348))               # 8 (greyed: nothing)
tap((728, 440))
chk("BPM 128 +-3 %", TF.nat_get(mem, 1) == 128 and TF.nat_get(mem, 2) == 3)
tap((150, 149))                            # tick KEY
tap((317, 149))                            # key picker
chk("key picker open", v("tfs_edit") == 3)
tap((70, 440))                             # MINOR
tap((18 + 64 * 7 + 30, 360))               # Am
fb_png(m, f"{OUT}/screen_3_picker.png")
tap((728, 440))
chk("KEY Am ticked", TF.nat_get(mem, 3) == 8)
run(20_000_000)
fb_png(m, f"{OUT}/screen_4_set.png")
lib = TF.library(os.path.expanduser("~/Downloads/Rekordbox stuff/PIONEER/rekordbox/export.pdb"))
want = sum(TF.matches(r, bpm=128, rng=3, key=8) for r in lib.values())
print("  live count (Python reference):", want, " screen store:", store())
chk("reference count is the 1000MK2's 48", want == 48)

# leave with the title funnel -> the list is redone
tap((680, 27))
for _ in range(60):
    run(10_000_000)
    if v("tfs_relist") == 0 and count() not in (263,):
        break
run(150_000_000)
fb_png(m, f"{OUT}/screen_5_filtered.png")
chk(f"closed; TRACK list redone with the filter ({count()} rows)",
    v("tfs_mode") == 0 and count() == 48)

if os.environ.get("SAVE5"):
    m.save(os.environ["SAVE5"])
# tap the header -> filter off, all back
print("  before tap: on", mem.r8(s["tf_cfg"]), "mode", v("tfs_mode"), "relist", v("tfs_relist"),
      "taps", v("tfs_taps"), "holds", v("tfs_holds"))
tap((710, 90))
print("  after tap: on", mem.r8(s["tf_cfg"]), "mode", v("tfs_mode"), "relist", v("tfs_relist"),
      "taps", v("tfs_taps"), "holds", v("tfs_holds"))
for _ in range(60):
    run(10_000_000)
    if count() == 263:
        break
run(100_000_000)
fb_png(m, f"{OUT}/screen_6_off.png")
chk("tap -> filter off, 263 rows", count() == 263 and mem.r8(s["tf_cfg"]) == 0)

# hold RESET
press((710, 90), until=lambda: v("tfs_mode") == 1, most=2000)
lift(); run(8_000_000)
press((52, 275), until=lambda: TF.nat_get(mem, 1) == 0 and TF.nat_get(mem, 3) == 0, most=2000)
lift(); run(8_000_000)
fb_png(m, f"{OUT}/screen_7_reset.png")
chk("hold RESET -> nothing ticked, 130 / 6 %", store()[:1] == [0] and store()[2] == 0
    and TF.nat_get(mem, 2) == 6 and TF.nat_get(mem, 4) == 0 and TF.nat_get(mem, 5) == 0)
key("Back")
chk("BACK leaves the screen", v("tfs_mode") == 0)
chk("no CPU exceptions", m.exc_total == exc0)
print("counters: draws", v("tfs_draws"), "full", v("tfs_full"), "stripped", v("tfs_stripped"),
      "relists", v("tfs_relists"))
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
