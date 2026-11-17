#!/usr/bin/env python3
"""stems_shortahead_emu.py -- EMULATOR ONLY, real audio chip: the SHORT
READ-AHEAD stem mute (2026-11-12).

    .venv/bin/python tools/stems_shortahead_emu.py [SECONDS_TO_SETTLE]

Swaps build/stems/sm.bin (built with -DSM_NODR, SM_AHEAD = read-ahead
limit) into cache/emu_flush_playing.pkl (a STEMS-4 track playing), leaves the
poll pool stock (no ring rewrite at all), plays until the chip's buffer has
shrunk to the limit, then two otherwise identical runs: control and one
stem muted. Reports:
  * the chip's buffer (0x11837CD0, blocks of 1/75 s) during the settle;
  * how long after the tap the mute is heard;
  * that the muted output really is the new mix (it converges to it);
  * that playback never ran dry: the play counter keeps moving at real
    speed and the DAC output has no long silent/frozen stretch.
"""
import os
import sys
import time
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
import numpy as np                       # noqa: E402
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
from stems_flush_emu import mono, first_change   # noqa: E402
from dsp_pitch_probe import SH_STEPS_PER_S       # noqa: E402
from stems_fastmute_emu import syms, POLL_POOL   # noqa: E402

SNAP = os.environ.get("SNAP", "cache/emu_flush_playing.pkl")
# SM_DIR: a variant built with SM_OUT=... tools/stems/build.sh (default: the deck build)
SM_DIR = os.environ.get("SM_DIR", "build/stems")
TAG = os.environ.get("TAG", "")          # keeps parallel runs' files apart
FS = 44100
LEVEL, PLAYC = 0x11837CD0, 0x11837CCC


def swap(m):
    mem, s = m.mem, syms()
    img = open(SM_DIR + "/sm.bin", "rb").read()
    old_entry = mem.r32(s["sm_table"])
    vc = [a for a in range(s["sm_table"], s["sm_table"] + 0x2000, 4) if mem.r32(a) == 0x081DB300]
    prev = 0x081DB300 if vc else 0x08C61A6C
    for a in range(s["sm_table"], s["end"], 4):
        mem.w32(a, 0)
    for i, b in enumerate(img):
        mem.w8(s["sm_table"] + i, b)
    mem.w32(s["sm_prev"], prev)
    for slot in [0x08D5FFA8, 0x08DDFFD4] + list(range(0x09970000, 0x09A00000, 4)):
        if mem.r32(slot) == old_entry:
            mem.w32(slot, s["sm_fread"])
    if os.environ.get("DR") == "1":              # + the ring rewrite (fast mute)
        mem.w32(POLL_POOL, mem.r32(s["sm_table"] + 4))
    elif mem.r32(POLL_POOL) != 0x08BF2BCC:       # no ring rewrite in this design
        mem.w32(POLL_POOL, 0x08BF2BCC)
    return s


def main():
    settle = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    t0 = time.time()
    if os.environ.get("BASE"):                   # reuse a settled state
        return measure(os.environ["BASE"], t0)
    m = Machine.load(SNAP)
    m.mem.dev.panel_frame = panl.build_frame(tempo=250)
    s = swap(m)
    d = m.mem.dev.dspemu
    v = lambda n: m.mem.r32(s[n])            # noqa: E731
    sl = int(SH_STEPS_PER_S * 0.5)
    print("settle (emulated s, buffer blocks, play counter, reads held, ms held):", flush=True)
    for k in range(int(settle / 0.5)):
        m.cpu.run(limit=sl)
        if k % 4 == 3:
            print(f"  {(k + 1) * 0.5:5.1f}  {d.bus.read(LEVEL, 4):5d}  {d.bus.read(PLAYC, 4):6d}  "
                  f"{v('sm_thr'):5d}  {v('sm_thrms'):6d}  paused-exits {v('sm_thrpause')}", flush=True)
    path = f"build/shortahead_base{TAG}.pkl"
    m.save(path)
    return measure(path, t0)


def measure(path, t0):
    out, ctr = {}, {}
    for mode in ("ctl", "chg"):
        m = Machine.load(path)
        mem, d = m.mem, m.mem.dev.dspemu
        if os.environ.get("SWAP") == "1":       # this build into the settled state
            swap(m)
        base = mem.r8(PP.PMASK) & 15
        stem = int(os.environ.get("FM_STEM", "1"))
        if mode == "chg":
            mem.w8(PP.PMASK, base ^ (1 << stem))
        n0 = len(d.asp1.out)
        c0 = d.bus.read(PLAYC, 4)
        words, levels = [], []
        for _ in range(int(8.0 / 0.25)):
            m.cpu.run(limit=int(SH_STEPS_PER_S * 0.25))
            words += d.asp1.out[n0:]
            n0 = len(d.asp1.out)
            levels.append(d.bus.read(LEVEL, 4))
            if os.environ.get("DR") == "1" and mode == "chg" and len(levels) <= 8:
                sy = syms()
                print(f"    t={len(levels) * 0.25:.2f}s polls {mem.r32(sy['sm_polls'])} "
                      f"rewritten {mem.r32(sy['sm_drblk'])} play {d.bus.read(PLAYC, 4)} "
                      f"next {mem.r32(sy['sm_drlastb']) if 'sm_drlastb' in sy else '-'}", flush=True)
        ctr[mode] = (d.bus.read(PLAYC, 4) - c0, levels, d.error)
        if os.environ.get("DR") == "1":
            sy = syms()
            print(f"  {mode}: rewrite blocks {mem.r32(sy['sm_drblk'])} sub {mem.r32(sy['sm_drsub'])} "
                  f"bump {mem.r32(sy['sm_drbump'])} wfail {mem.r32(sy['sm_drwfail'])} "
                  f"batches {mem.r32(sy['sm_drbat'])}", flush=True)
        out[mode] = mono(words)
        np.save(f"build/shortahead_{mode}{TAG}.npy", out[mode])
    ok = True
    # 2026-11-13: first_change() waits for a difference of 25 % of the
    # music's level, so a quiet stem (vocals at rms ~150) "took" seconds while
    # the chip already played it muted. Both runs are deterministic: any real
    # difference (mean |diff| > 8 LSB over 512 samples) is the change.
    a, b = out["ctl"], out["chg"]
    n = min(len(a), len(b))
    i = next((k for k in range(0, n - 512, 512)
              if np.abs(a[k:k + 512] - b[k:k + 512]).mean() > 8), None)
    if i is not None:
        w = slice(i, min(n, i + FS))
        print(f"  first second after the change: control rms {np.sqrt(np.mean(a[w] ** 2)):.0f}, "
              f"muted rms {np.sqrt(np.mean(b[w] ** 2)):.0f}, difference rms "
              f"{np.sqrt(np.mean((a[w] - b[w]) ** 2)):.0f}")
    lat = None if i is None else i / FS
    print(f"mute heard after: {'never (8 s)' if lat is None else f'{lat:.2f} s'}")
    ok &= lat is not None and lat < (int(os.environ.get('AHEAD_S', '4')) + 1.5)
    for mode in ("ctl", "chg"):
        n, levels, err = ctr[mode]
        a = out[mode]
        # longest run of (near) silence or a frozen sample = an underrun
        flat = np.abs(np.diff(a.astype(np.int32))) < 2
        run = mx = 0
        for f in flat:
            run = run + 1 if f else 0
            mx = max(mx, run)
        print(f"  {mode}: play counter +{n} in 8 s (real speed = {8 * 75}), "
              f"buffer {min(levels)}..{max(levels)} blocks, longest flat {mx / FS * 1000:.0f} ms, err {err}")
        ok &= n > 8 * 75 * 0.95 and mx < FS * 0.05 and err is None and min(levels) > 0
    print("RESULT:", "PASS" if ok else "FAIL", f"[{time.time() - t0:.0f}s wall]")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
