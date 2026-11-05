#!/usr/bin/env python3
"""c6x_pgo.py -- refresh emu/c6x/c6xcore.profdata, the clang profile the
DSP core is built with (emu/c6x/cfast.build_lib).

Builds an instrumented core, plays cache/emu_dsp_wavplay.pkl.gz in play
mode (XDJ_PLAY, DSP on its own thread) for SECS wall seconds, merges the
counters. Re-run after larger c6xcore.c changes (a stale profile still
builds -- clang uses what still matches -- just less well).

    .venv/bin/python tools/c6x_pgo.py [SECS=30]
"""
import glob
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0


def main():
    import emu.c6x.cfast as cf
    tmp = tempfile.mkdtemp(prefix="c6xpgo")
    lib = os.path.join(tmp, "c6x_gen.so")
    subprocess.check_call(["cc", "-O2", "-fprofile-instr-generate", "-shared", "-fPIC",
                           "-o", lib, cf.SRC, "-lm"])
    code = f"""
import os, sys, time
sys.path.insert(0, {ROOT!r})
import emu.c6x.cfast as cf
cf.LIB = {lib!r}; cf.SRC = "/nonexistent"
from emu.machine import Machine
m = Machine.load("cache/emu_dsp_wavplay.pkl.gz")
de = m.mem.dev.dspemu
t0 = time.time()
while time.time() - t0 < {SECS}:
    m.cpu.run(limit=1_000_000)
    de.sync()
de.stop_thread()
assert not de.error, de.error
"""
    env = dict(os.environ, XDJ_PLAY="1", XDJ_DECK_SOUND="0",
               LLVM_PROFILE_FILE=os.path.join(tmp, "c6-%p.profraw"))
    subprocess.check_call([sys.executable, "-c", code], env=env)
    raws = glob.glob(os.path.join(tmp, "*.profraw"))
    prof = cf.PROF + ".tmp"
    subprocess.check_call(["xcrun", "llvm-profdata", "merge", "-o", prof] + raws)
    os.replace(prof, cf.PROF)
    print("wrote", cf.PROF)


if __name__ == "__main__":
    main()
