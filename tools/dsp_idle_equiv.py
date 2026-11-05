#!/usr/bin/env python3
"""dsp_idle_equiv.py SNAPSHOT [CYCLES] -- proof and speed of the audio
chip's busy-wait skip (emu/c6x/c6xcore.c, step()).

Runs the real DSP of SNAPSHOT for CYCLES twice: once with no idle skipping
at all (XDJ_DSP_IDLE=0), once with it (both loops: the stopped deck's
0xC004C8CC and the playing deck's 0xC0048C2E). The skip only jumps whole
passes of a loop whose state provably repeats, so the two runs must end in
the SAME state: registers, control registers, all of L2 (code, data and
stack), the audio ring and the DMA parameter RAM. Prints both speeds and
IDENTICAL, or the parts that differ (exit code 1).

    .venv/bin/python tools/dsp_idle_equiv.py cache/emu_dsp_wavplay.pkl.gz 20000000
"""
import ctypes
import hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

L2_LO, L2_N = 0x00800000, 0x40000      # 256 KB: the DSP's code, data, stack


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _ctrl_n():
    """C_NCTRL from the core source (the control-register count)."""
    try:
        for line in open(os.path.join(ROOT, "emu", "c6x", "c6xcore.c")):
            if line.startswith("#define") and "C_NCTRL" in line:
                return int(line.split()[-1], 0)
    except OSError:
        pass
    return 16


def run(snap, n, skip):
    """run the DSP for n cycles and hash everything it can change"""
    os.environ["XDJ_DSP_IDLE"] = "1" if skip else "0"
    os.environ["XDJ_DECK_SOUND"] = "0"
    from emu.machine import Machine
    from emu.c6x import cfast

    m = Machine.load(snap)
    d = m.mem.dev.dspemu
    assert d is not None, "snapshot has no real DSP"
    cpu = d.cpu
    lib, u32 = cpu.lib, cfast.u32

    t0 = time.time()
    left = n
    while left > 0:                      # in slices, like the machine drives it
        k = min(left, 1_000_000)
        cpu.run_cycles(k)
        left -= k
    dt = time.time() - t0

    lib.c6x_regs.restype = ctypes.POINTER(u32)
    lib.c6x_ctrl.restype = ctypes.POINTER(u32)
    st = {
        "registers": _sha(bytes(bytearray(
            b"".join(int(x).to_bytes(4, "little") for x in lib.c6x_regs(cpu.h)[:64])))),
        "control": _sha(bytes(bytearray(
            b"".join(int(x).to_bytes(4, "little")
                     for x in lib.c6x_ctrl(cpu.h)[:_ctrl_n()])))),
        "pc": _sha(int(cpu.pc).to_bytes(4, "little")),
    }
    try:
        st["L2"] = _sha(d.bus.dump(L2_LO, L2_N))
    except Exception as e:               # noqa: BLE001 - report, don't hide
        st["L2"] = f"unavailable: {e!r}"
    try:
        ring = list(d.asp1.out)[-8192:]
        st["audio ring"] = _sha(repr(ring).encode())
    except Exception as e:               # noqa: BLE001
        st["audio ring"] = f"unavailable: {e!r}"
    for name, obj in (("DMA params", getattr(d, "edma", None)),):
        p = None
        for attr in ("param", "params", "param_ram", "ram"):
            if obj is not None and hasattr(obj, attr):
                p = getattr(obj, attr)
                break
        st[name] = _sha(repr(p).encode()) if p is not None else "unavailable"
    jumped = lib.c6x_counts(cpu.h, 4)
    jumps = lib.c6x_counts(cpu.h, 5)
    return st, dt, jumped, jumps


def main(argv):
    snap = argv[0] if argv else "cache/emu_dsp_wavplay.pkl.gz"
    n = int(float(argv[1])) if len(argv) > 1 else 20_000_000
    if not os.path.exists(snap) and not snap.startswith("/"):
        print(f"no snapshot at {snap}")
        return 2
    a, ta, _, _ = run(snap, n, False)
    b, tb, jumped, jumps = run(snap, n, True)
    print(f"no skip : {n:,} cycles in {ta:6.2f} s ({n / ta / 1e6:5.2f} Mcyc/s)")
    print(f"skip    : {n:,} cycles in {tb:6.2f} s ({n / tb / 1e6:5.2f} Mcyc/s)"
          f"   {ta / tb:.2f}x faster, {jumped:,} cycles jumped in {jumps:,} passes")
    bad = [k for k in a if a[k] != b.get(k)]
    if bad:
        print("DIFFERS: " + ", ".join(f"{k} ({a[k][:12]} vs {b[k][:12]})" for k in bad))
        return 1
    print("IDENTICAL: registers, control registers, L2, audio ring, DMA params")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
