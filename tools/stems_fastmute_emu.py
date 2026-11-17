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
mute = one stem off; unmute = that stem muted first, then back on. The stem
is the first one unmuted in the snapshot's own PMASK, so the change is a
single bit -- the case the one-plane ring rewrite handles.
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
# SM_DIR: a variant built with SM_OUT=... tools/stems/build.sh (default: the deck build)
SM_DIR = os.environ.get("SM_DIR", "build/stems")
# PSLOT_SKEW=N: make the SH see the chip's play-slot word N slots AHEAD, as
# the real deck reported on 2026-11-09 (label at PSLOT = PLAYBLK + 968). The
# chip itself is untouched -- only the host-port read our module does is
# skewed. Needs the Python host-port path (XDJ_DSP_CS6C=0).
SKEW = int(os.environ.get("PSLOT_SKEW", "0"))
# PLAYBLK_SKEW=N: make the SH see the chip's PLAYBLK word N blocks OFF (the
# real deck mid-track, 2026-11-09 ring map: PLAYBLK lags thousands of blocks
# while PSLOT stays right). Negative = lagging.
PSKEW = int(os.environ.get("PLAYBLK_SKEW", "0"))
if SKEW or PSKEW:
    os.environ["XDJ_DSP_CS6C"] = "0"


def skew(m):
    if not (SKEW or PSKEW):
        return
    d = m.mem.dev.dspemu
    orig = d.read

    def read(a, size, _o=orig, _d=d):
        v = _o(a, size)
        if a - 0x18000000 == 0xC0000 and size == 4 and _d.hpia == 0x1182E81C:
            v = (v + SKEW) % 13846
        if a - 0x18000000 == 0xC0000 and size == 4 and _d.hpia == 0x11837CCC and PSKEW:
            v = max(0, v + PSKEW)
        return v
    d.read = read
FS = 44100
POLL_POOL = 0x08C1D56C


def syms():
    s = {}
    for line in open(SM_DIR + "/sm.syms"):
        a, _, nm = line.split()
        s[nm.lstrip("_")] = int(a, 16)
    return s


def swap_new(m):
    mem, s = m.mem, syms()
    img = open(SM_DIR + "/sm.bin", "rb").read()
    # the snapshot's own sm build: its entry is the table's first word; what
    # it chained to is the version switcher's cave (or the stock read)
    old_entry = mem.r32(s["sm_table"])
    old_poll = mem.r32(s["sm_table"] + 4)      # snapshot may already carry the poll hook
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
    assert mem.r32(POLL_POOL) in (0x08BF2BCC, old_poll), hex(mem.r32(POLL_POOL))
    mem.w32(POLL_POOL, mem.r32(s["sm_table"] + 4))
    return s


def to_measure_point(which):
    m = Machine.load(SNAP)
    skew(m)
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
        skew(m)
        mem, d = m.mem, m.mem.dev.dspemu
        # Toggle ONE stem from the snapshot's own mask: the one-plane path
        # only applies when exactly one bit changes, and the ring must
        # already hold that old mask (the module checks one block first).
        base = mem.r8(PP.PMASK) & 15
        stem = 0
        while stem < 4 and (base & (1 << stem)):
            stem += 1
        if stem == 4:                        # all muted: unmute something
            stem = 0
        if os.environ.get("FM_STEM"):        # 0 vocals 1 drums 2 bass 3 other
            stem = int(os.environ["FM_STEM"])
        one = 1 << stem
        if change == "unmute":               # both runs: that stem muted first
            mem.w8(PP.PMASK, base | one)
            m.cpu.run(limit=int(SH_STEPS_PER_S * 1.5))
        if mode == "chg":
            mem.w8(PP.PMASK, base if change == "unmute" else base | one)
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
                  f"noslot {mem.r32(s['sm_drnoslot'])} "
                  f"sub {mem.r32(s['sm_drsub']) if 'sm_drsub' in s else '-'} "
                  f"chk {mem.r32(s['sm_drchk']) if 'sm_drchk' in s else '-'} "
                  f"fix {mem.r32(s['sm_drfix']) if 'sm_drfix' in s else '-'} "
                  f"reads {mem.r32(s['sm_calls'])} err {d.error}", flush=True)
    i = first_change(out["ctl"], out["chg"])
    print(f"RESULT {which} {change}:",
          "no change within 3 s" if i is None else f"change heard after {i / FS:.3f} s",
          f"[{time.time()-t0:.0f}s]", flush=True)
    return out


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "new", sys.argv[2] if len(sys.argv) > 2 else "mute")
