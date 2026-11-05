#!/usr/bin/env python3
"""stems_fastmute_emu.py -- EMULATOR ONLY, real DSP: how long after a STEMS
pad tap is the change heard, when the tap lands BETWEEN the deck's read
bursts (the deck reads the stick ~every 5 s; the old rewrite waited for the
next burst, the fast trigger runs from the player task's ~340/s poll).

    .venv/bin/python tools/stems_fastmute_emu.py [old|new] [mute|unmute]

From cache/emu_flush_playing.pkl ("Don't Come Back" as a full-length
STEMS-4 file, playing; tools/stems_flush_emu.py prep): `new` swaps in
build/stems/sm.bin and hooks the poll pool, `old` keeps the snapshot's sm.
Runs to the end of the next read burst + 1 s, then two otherwise identical
runs (control, change) and reports the first audible difference.
mute = VOCALS off; unmute = muted first, all back on at the measure point.
"""
import os
import sys
import time
sys.path[:0] = [".", "tools"]
import numpy as np                       # noqa: E402
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
from stems_flush_emu import mono, first_change   # noqa: E402
from dsp_pitch_probe import SH_STEPS_PER_S       # noqa: E402

SNAP = os.environ.get("SNAP", "cache/emu_flush_playing.pkl")
FS = 44100
POLL_POOL = 0x08C1D56C


def syms():
    s = {}
    for line in open("build/stems/sm.syms"):
        a, _, nm = line.split()
        s[nm.lstrip("_")] = int(a, 16)
    return s


def swap_new(m):
    mem, s = m.mem, syms()
    img = open("build/stems/sm.bin", "rb").read()
    # the snapshot's own sm build: its entry is the table's first word; what
    # it chained to is the version switcher's cave (or the stock read)
    old_entry = mem.r32(s["sm_table"])
    vc = [a for a in range(s["sm_table"], s["sm_table"] + 0x2000, 4) if mem.r32(a) == 0x081DB300]
    prev = 0x081DB300 if vc else 0x08C61A6C
    print(f"old sm entry {old_entry:#x}, chains {prev:#x}", flush=True)
    for a in range(s["sm_table"], s["end"], 4):
        mem.w32(a, 0)
    for i, b in enumerate(img):
        mem.w8(s["sm_table"] + i, b)
    mem.w32(s["sm_prev"], prev)
    for slot in [0x08D5FFA8, 0x08DDFFD4] + list(range(0x09970000, 0x09A00000, 4)):
        if mem.r32(slot) == old_entry:
            mem.w32(slot, s["sm_fread"])
    assert mem.r32(POLL_POOL) == 0x08BF2BCC
    mem.w32(POLL_POOL, mem.r32(s["sm_table"] + 4))
    return s


def to_measure_point(which):
    m = Machine.load(SNAP)
    m.mem.dev.panel_frame = panl.build_frame(tempo=250)
    s = swap_new(m) if which == "new" else None
    d = m.mem.dev.dspemu
    sl = int(SH_STEPS_PER_S * 0.02)
    seen = False
    quiet = 0
    lvl = d.bus.read(0x11837CD0, 4)
    for _ in range(int(12 / 0.02)):          # find a burst end
        m.cpu.run(limit=sl)
        v = d.bus.read(0x11837CD0, 4)
        if v > lvl + 2:
            seen, quiet = True, 0
        elif seen:
            quiet += 1
        lvl = v
        if seen and quiet >= 50:             # 1 s after the burst
            break
    return m, s


def run(which, change):
    t0 = time.time()
    base, s = to_measure_point(which)
    print(f"[{which}] at measure point, buffer {base.mem.dev.dspemu.bus.read(0x11837CD0, 4)} "
          f"blocks [{time.time()-t0:.0f}s]", flush=True)
    path = "build/fastmute_base.pkl"
    base.save(path)          # (never mid-rewrite: a snapshot drops in-flight
                             # USB reads and the waiting task would never wake)
    out = {}
    for mode in ("ctl", "chg"):
        m = Machine.load(path)
        mem, d = m.mem, m.mem.dev.dspemu
        if change == "unmute":               # both runs: muted for 1.5 s first
            mem.w8(PP.PMASK, 0x01)
            m.cpu.run(limit=int(SH_STEPS_PER_S * 1.5))
        if mode == "chg":
            mem.w8(PP.PMASK, 0x01 if change == "mute" else 0x00)
        n0 = len(d.asp1.out)
        words = []
        steps = int(3.0 * SH_STEPS_PER_S)
        while steps > 0:
            k = min(steps, 100_000_000)
            m.cpu.run(limit=k)
            steps -= k
            words += d.asp1.out[n0:]
            n0 = len(d.asp1.out)
        out[mode] = mono(words)
        np.save(f"build/fastmute_{which}_{change}_{mode}.npy", out[mode])
        if s:
            print(f"  {mode}: polls {mem.r32(s['sm_polls'])} armed {mem.r32(s['sm_parm'])} "
                  f"blocks {mem.r32(s['sm_drblk'])} skips {mem.r32(s['sm_drskip'])} "
                  f"reads {mem.r32(s['sm_calls'])} err {d.error}", flush=True)
    i = first_change(out["ctl"], out["chg"])
    print(f"RESULT {which} {change}:",
          "no change within 3 s" if i is None else f"change heard after {i / FS:.3f} s",
          f"[{time.time()-t0:.0f}s]", flush=True)
    return out


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "new", sys.argv[2] if len(sys.argv) > 2 else "mute")
