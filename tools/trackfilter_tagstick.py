#!/usr/bin/env python3
"""trackfilter_tagstick.py -- build the emulator snapshot for the TRACK
FILTER My Tag test (tools/trackfilter_mytag_emu.py): a fresh boot with
cache/x1k_ratestick (an APFS clone of the user's library whose exportExt.pdb
carries test My Tag links, build/review_1110/x1k_tags.json), browse open on
the category list with the cursor on TRACK.

    .venv/bin/python tools/trackfilter_tagstick.py   -> build/review_1110/tl_tags.pkl

A remount of an older snapshot does not work here (the deck keeps the old
stick's tables), so this boots (~1 min, C core). Rebuild after changing the
stick's exportExt.pdb.
"""
import os
import subprocess
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]
from emu.machine import Machine
from emu import panl

STICK = os.environ.get("STICK", os.path.join(ROOT, "cache", "x1k_ratestick"))
BOOT = os.path.join(ROOT, "build", "review_1110", "tags_boot.pkl")
OUT = os.path.join(ROOT, "build", "review_1110", "tl_tags.pkl")
subprocess.run([sys.executable, os.path.join(HERE, "boot_realusb.py"), STICK, BOOT], check=True)
m = Machine.load(BOOT)
mem, dev, c = m.mem, m.mem.dev, m.cpu


def run(n, keys=()):
    dev.panel_frame = panl.build_frame(keys=list(keys))
    c.run(limit=n)


run(20_000_000)
run(12_000_000, ["DevUSB"]); run(400_000_000)
for _ in range(2):
    mem.w16(panl.ROTARY_COUNTER, (mem.r16(panl.ROTARY_COUNTER) + 1) & 0xFFFF)
    run(8_000_000)
run(80_000_000)
m.save(OUT)
print("saved", OUT)
