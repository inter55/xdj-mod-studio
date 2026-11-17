#!/usr/bin/env python3
"""stems_ringmap_emu.py -- EMULATOR ONLY: check the ring-map build of the
stems module (build with SM_CFLAGS=-DSM_MAP). Mutes VOCALS, runs until the
map is done, then compares the module's runs {slot, label, length} with the
labels read straight out of the emulated chip's memory.

    PMASK=... SM_CFLAGS=-DSM_MAP sh tools/stems/build.sh
    .venv/bin/python tools/stems_ringmap_emu.py
"""
import sys
sys.path[:0] = [".", "tools"]
from stems_fastmute_emu import to_measure_point, skew   # noqa: E402,F401
from dsp_pitch_probe import SH_STEPS_PER_S              # noqa: E402
import perform_pads as PP                               # noqa: E402

RING, SLOT, NSLOT = 0xC0090000, 4804, 13846


def truth_runs(d):
    runs, junk, s0, l0, n = [], 0, 0, 0, 0
    for sl in range(NSLOT):
        lab = d.bus.read(RING + sl * SLOT + 4704, 4)
        if n and lab == l0 + n:
            n += 1
            continue
        if n >= 2:
            runs.append((s0, l0, n))
        else:
            junk += n
        s0, l0, n = sl, lab, 1
    if n >= 2:
        runs.append((s0, l0, n))
    else:
        junk += n
    return runs, junk


def main():
    m, s = to_measure_point("new")
    mem, d = m.mem, m.mem.dev.dspemu
    assert "sm_map" in s, "build/stems is not the -DSM_MAP build"
    mem.w8(PP.PMASK, 0x01)
    for _ in range(60):
        m.cpu.run(limit=int(SH_STEPS_PER_S * 0.1))
        if mem.r32(s["sm_mapdone"]):
            break
    done = mem.r32(s["sm_mapdone"])
    n = mem.r32(s["sm_mapn"])
    got = [tuple(mem.r32(s["sm_map"] + 12 * i + 4 * k) for k in range(3)) for i in range(n)]
    print(f"map done {done}  runs {n}  junk {mem.r32(s['sm_mapjunk'])}  lost {mem.r32(s['sm_maplost'])}")
    print(f"PLAYBLK {mem.r32(s['sm_mapp0'])} -> {mem.r32(s['sm_mapp1'])}  "
          f"PSLOT {mem.r32(s['sm_mapps0'])} -> {mem.r32(s['sm_mapps1'])}  err {d.error}")
    for r in got[:20]:
        print(f"  slot {r[0]:5d}  blocks {r[1]:6d}..{r[1] + r[2] - 1:6d}  ({r[2]})")
    tr, junk = truth_runs(d)
    print(f"truth now: runs {len(tr)} junk {junk}")
    same = sum(1 for r in got if r in tr)
    # a run can grow at its front while the scan is under way: same start
    starts = {(a, b) for a, b, _ in tr}
    near = sum(1 for r in got if (r[0], r[1]) in starts)
    print(f"runs identical {same}/{n}, same start {near}/{n}")
    ok = done and n and near >= max(1, n - 2) and not d.error
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
