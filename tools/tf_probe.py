#!/usr/bin/env python3
"""tf_probe.py -- READ-ONLY: the TRACK FILTER screen's counters on the deck
(mode, draws, full repaints, stripped/cropped stock commands, damaged
lines, relist state, hook pools). Uses build/trackfilter/tf.syms."""
import os, struct, sys, time
sys.path[:0] = [".", "tools"]
import hid
from xdj700_usb_shell import PIONEER_VID, open_deck, drain
from rgb_overview_apply import br, send
import trackfilter as TF

s = {}
for line in open("build/trackfilter/tf.syms"):
    a, _, n = line.split()
    s[n.lstrip("_")] = int(a, 16)
c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
os.makedirs("docs/deck_logs", exist_ok=True)
log = open(time.strftime("docs/deck_logs/tf_probe_%Y-%m-%d.log"), "a")
def out(*a):
    print(*a); print(*a, file=log)
try:
    drain(h, 1.0)
    send(h, "$CDJ", 1.5)
    u32 = lambda a: struct.unpack("<I", bytes(br(h, a, 4)))[0]
    out(time.strftime("==== %H:%M:%S"))
    for n in ("tf_dead", "tfs_mode", "tfs_edit", "tfs_tab", "tfs_draws", "tfs_full", "tfs_stripped",
              "tfs_cropped", "tfs_dmg", "tfs_lay", "tfs_tlo", "tfs_thi", "tfs_relist",
              "tfs_relists", "tfs_taps", "tfs_holds", "tfs_touches", "tf_left", "tf_calls"):
        if n in s:
            v = u32(s[n])
            out(f"  {n:14s} {v:#010x} ({v})")
    out("  cfg.on", bytes(br(h, s["tf_cfg"], 1)).hex())
    for p in (0x08C92E20, 0x08D5278C) + TF.TEXT_POOLS + tuple(TF.POOLS):
        out(f"  pool {p:#x} = {u32(p):#x}")
    out("  MODEW", u32(0x0BA371D4))
finally:
    h.close()
