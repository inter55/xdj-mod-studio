#!/usr/bin/env python3
"""trackfilter_mytag_emu.py -- MY TAG tab and MASTER PLAYER of the TRACK
FILTER screen, native touch only, in the emulator.

    .venv/bin/python tools/trackfilter_mytag_emu.py

MY TAG: build/review_1110/tl_tags.pkl = fresh boot with cache/x1k_ratestick
(an APFS clone of the user's library whose exportExt.pdb carries test My Tag
links, build/review_1110/x1k_tags.json), browse on the category list, cursor
on TRACK (tools/trackfilter_tagstick.py). The same stick in the XDJ-1000MK2's
firmware gave: Genre Acid House 3, + Deep House AND 1 / OR 4, + Components
Vocal (AND across columns) 1. Each list here must equal those numbers.

MASTER PLAYER: build/review_1110/tl_loaded.pkl = the user's library with
"Adventure" (124.76 BPM, Abm) loaded; the button must set BPM 125 and key Abm
(1000MK2 number 1) and tick both.
"""
import json
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
ok = True


def chk(label, good):
    global ok
    print(("ok   " if good else "FAIL ") + label, flush=True)
    ok &= bool(good)


class Deck:
    def __init__(self, snap):
        self.m = Machine.load(snap)
        self.mem, self.dev, self.c = self.m.mem, self.m.mem.dev, self.m.cpu
        self.s = TF.install(self.mem)

    def v(self, n):
        return self.mem.r32(self.s[n])

    def run(self, n, frame=None):
        self.dev.panel_frame = frame or panl.build_frame(keys=[])
        self.c.run(limit=n)

    def key(self, k):
        self.run(12_000_000, panl.build_frame(keys=[k]))
        self.run(25_000_000)

    def press(self, xy, until=None, n=6, most=400):
        t0 = self.v("tfs_touches")
        for _ in range(most):
            self.run(250_000, panl.build_frame(touch=xy))
            if until() if until else self.v("tfs_touches") - t0 >= n:
                return True
        print("  press did not land:", xy)
        return False

    def tap(self, xy):
        r = self.press(xy)
        self.run(3_000_000)
        self.run(4_000_000)
        return r

    def count(self):
        return self.mem.r16(TF.LEFTBUF + 2)

    def open_screen(self):
        self.press((710, 90), until=lambda: self.v("tfs_mode") == 1, most=2000)
        self.run(3_000_000)
        self.run(8_000_000)

    def close_and_list(self):
        self.tap((680, 27))
        for _ in range(80):
            self.run(10_000_000)
            if self.v("tfs_relist") == 0 and self.v("tfs_mode") == 0:
                break
        self.run(150_000_000)
        return self.count()

    def mcond(self, c):
        b = self.s["tf_mcond"] + c * 36
        n = self.mem.r8(b + 2)
        return (self.mem.r8(b), self.mem.r8(b + 1), [self.mem.r32(b + 4 + 4 * i) for i in range(n)])


# ---- MY TAG --------------------------------------------------------------
tags = json.load(open(os.path.join(ROOT, "build", "review_1110", "x1k_tags.json")))
AH, DH, VOC = 2360286408, 3226815695, 2611334893
d = Deck(os.path.join(ROOT, "build", "review_1110", "tl_tags.pkl"))
exc0 = d.m.exc_total
d.run(20_000_000)
d.key("RotaryPush")
d.run(150_000_000)
chk(f"TRACK list open ({d.count()} rows)", d.count() == 263)
names = [bytes(d.mem.r8(d.s["tf_mc"] + i * 32 + 8 + k) for k in range(24)).split(b"\0")[0].decode()
         for i in range(d.v("tf_mcn"))]
chk(f"My Tag columns read from the stick: {names}",
    names == ["Genre", "Components", "Situation", "Untitled Column"])
nt = d.v("tf_mtn")
used = sorted({g for _, g in tags["links"]})
got = sorted(d.mem.r32(d.s["tf_mt"] + 32 * i) for i in range(nt))
chk(f"used tags = the stick's links ({nt})", got == used)

d.open_screen()
d.tap((51, 154))                                  # MY TAG tab
d.run(20_000_000); fb_png(d.m, f"{OUT}/mytag_1_tab.png")
d.tap((108, 77))                                  # tick Genre
d.tap((393, 142))                                 # Acid House
d.run(20_000_000); fb_png(d.m, f"{OUT}/mytag_2_ah.png")
chk("Genre ticked, Acid House chosen", d.mcond(0) == (1, 0, [AH]))
d.tap((648, 142))                                 # Deep House (AND)
chk("Deep House added (AND)", d.mcond(0) == (1, 0, [AH, DH]))
n_and = d.close_and_list()
chk(f"Acid House AND Deep House -> {n_and} (1000MK2: 1)", n_and == 1)

d.open_screen()
d.tap((51, 154))
d.tap((208, 253))                                 # OR
n_or = d.close_and_list()
chk(f"Acid House OR Deep House -> {n_or} (1000MK2: 4)", n_or == 4)

d.open_screen()
d.tap((51, 154))
d.tap((350, 77))                                  # Components tab (label)
d.tap((280, 77))                                  # tick Components
# Components holds 8 used tags in position order: Synth Vocal / Beat SubBass
# / Percussion Piano / Dark Upper -> Vocal is the second cell
d.tap((648, 142))
d.run(20_000_000); fb_png(d.m, f"{OUT}/mytag_3_components.png")
chk("Components: ticked, Vocal chosen, AND", d.mcond(1) == (1, 0, [VOC]))
rot = d.mem.r16(panl.ROTARY_COUNTER)
d.mem.w16(panl.ROTARY_COUNTER, (rot + 1) & 0xFFFF)
d.run(20_000_000)
d.run(20_000_000); fb_png(d.m, f"{OUT}/mytag_4_scrolled.png")
chk("knob scrolls the tag grid one row", d.v("tfs_scroll") == 1)
d.tap((52, 218))                                  # i: overview
d.run(20_000_000); fb_png(d.m, f"{OUT}/mytag_5_info.png")
chk("i opens the overview", d.v("tfs_info") == 1)
d.tap((52, 218))
n_x = d.close_and_list()
chk(f"Genre OR + Components Vocal -> {n_x} (1000MK2: 1)", n_x == 1)

d.open_screen()
d.tap((51, 154))
d.press((52, 275), until=lambda: d.mcond(0)[0] == 0 and d.mcond(1)[0] == 0, most=2000)
d.run(8_000_000)
chk("hold RESET on MY TAG clears My Tag", all(d.mcond(c) == (0, 0, []) for c in range(4)))
n_r = d.close_and_list()
chk(f"list back to 263 ({n_r})", n_r == 263)
chk("no CPU exceptions (my tag)", d.m.exc_total == exc0)

# ---- MASTER PLAYER -------------------------------------------------------
d = Deck(os.path.join(ROOT, "build", "review_1110", "tl_loaded.pkl"))
exc0 = d.m.exc_total
d.run(20_000_000)
d.key("Browse")
d.run(200_000_000)
d.key("Back")                                     # re-open the list once the
d.run(60_000_000)                                 # module is in (a list built
d.key("RotaryPush")                               # before it has no button)
d.run(200_000_000)
d.run(20_000_000); fb_png(d.m, f"{OUT}/master_0_browse.png")
chk(f"browse shows a track list ({d.count()} rows)", d.count() > 0)
d.open_screen()
chk("screen open", d.v("tfs_mode") == 1)
d.tap((747, 116))
d.run(10_000_000)
d.run(20_000_000); fb_png(d.m, f"{OUT}/master_1_set.png")
chk(f"MASTER PLAYER: BPM {TF.nat_get(d.mem, 1)} (want 125), key {TF.nat_get(d.mem, 3)} "
    "(want 1 = Abm), both ticked", TF.nat_get(d.mem, 1) == 125 and TF.nat_get(d.mem, 3) == 1)
chk("no CPU exceptions (master)", d.m.exc_total == exc0)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
