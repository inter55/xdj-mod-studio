#!/usr/bin/env python3
"""dsp_idle_equiv.py SNAPSHOT [CYCLES] -- proof and speed of the audio chip's
busy-wait skip (emu/c6x/c6xcore.c, step()).

Runs the real DSP of SNAPSHOT for CYCLES (default 3e8) twice: once with no
idle skipping at all (XDJ_DSP_IDLE=0), once with it (both idle loops: the
stopped deck's 0xC004C8CC and the playing deck's 0xC0048C2E). The skip only
jumps whole passes of a loop whose state provably repeats, so the two runs
must end in the SAME state: registers, control registers, every RAM byte,
the EDMA parameter RAM and the audio sent to the DAC. Prints both speeds
and IDENTICAL, or the parts that differ (exit code 1).

  .venv/bin/python tools/dsp_idle_equiv.py \\
      "~/Library/Application Support/XDJ Mod Studio/firmware/dspsound.pkl.gz"
"""
import array
import ctypes
import hashlib
import os
import sys
import time

sys.path.insert(0, ".")


def run(snap, n, skip):
    from emu.machine import Machine
    from emu.c6x.cfast import set_idle_loop
    os.environ["XDJ_DSP_IDLE"] = "1" if skip else "0"
    m = Machine.load(snap)
    d = m.mem.dev.dspemu
    assert d is not None, "snapshot has no real DSP"
    c = d.cpu
    if skip:
        d._arm_idle()
    else:
        set_idle_loop(c, 0, 0, 0)
    d._run(20_000_000)                       # decode the code it runs once
    n0, j0 = len(d.asp1.out), c.idle_jumped
    t = time.time()
    d._run(n)
    w = time.time() - t
    lib = c.lib
    st = {"pc": c.pc, "cycle": c.cycle, "dsp_cycles": d.cycles,
          "regs": ctypes.string_at(lib.c6x_regs(c.h), 64 * 4),
          "ctrl": ctypes.string_at(lib.c6x_ctrl(c.h), 30 * 4),
          "dac": hashlib.sha1(array.array("I", d.asp1.out[n0:]).tobytes()).hexdigest(),
          "edma": hashlib.sha1(bytes(d.edma.param)).hexdigest()}
    seen = set()
    for lo, _hi, buf in d.bus.ram:
        if id(buf) not in seen:
            seen.add(id(buf))
            st[f"ram {lo:#x}"] = hashlib.sha1(bytes(buf)).hexdigest()
    print(f"  skip {'on ' if skip else 'off'}: {n / 1e6:.0f}M DSP cycles in {w:.2f}s = "
          f"{n / w / 1e6:.0f} Mcyc/s ({n / w / 3e6:.0f}% of the real 300 MHz); "
          f"jumped {c.idle_jumped - j0:,} cycles; DAC words {len(d.asp1.out) - n0}; "
          f"error {d.error}", flush=True)
    return st


def main():
    snap = os.path.expanduser(sys.argv[1])
    n = int(float(sys.argv[2])) if len(sys.argv) > 2 else 300_000_000
    a = run(snap, n, False)
    b = run(snap, n, True)
    diff = [k for k in a if a[k] != b[k]]
    print("IDENTICAL" if not diff else f"DIFFERENT: {diff}")
    sys.exit(1 if diff else 0)


if __name__ == "__main__":
    main()
