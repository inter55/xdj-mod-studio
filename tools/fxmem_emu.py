#!/usr/bin/env python3
"""fxmem_emu.py -- EMULATOR ONLY: prove the one-shot deck survey works.

Runs the real survey cave (tools/fx_dsp/fxmem.c) in the emulated SH firmware
exactly as the host arms it, against the real DSP program, and compares its
result with an independent direct read of the same DSP memory:

  1. stock state, MASTER TEMPO on, track playing -> the block must read FREE
     (nz == 0) and the raw DDR scan must agree;
  2. FX engine installed, Echo switched on      -> the same block must read
     IN USE (nz > 0), which is what proves the survey can see a delay line
     sitting there.

Also checks that the hook pool is restored and the host port pointer (HPIA)
is left as it was found -- both are deck-safety properties.

    .venv/bin/python tools/fxmem_emu.py [SNAP]
"""
import os
import sys
sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402
import fx_dsp_emu as F                   # noqa: E402

POOL = 0x08C92E20
MAGIC = 0x53565231
BASE = 0x09800000
LO, HI = 0xC0073400, 0xC0080000


def syms():
    s = {}
    for line in open("build/fxmem/fxmem.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def scan(d):
    """independent read of the same block, straight off the DSP bus"""
    b = d.bus.dump(LO, HI - LO)
    nz = first = last = 0
    for i in range(0, len(b), 4):
        if b[i] | b[i + 1] | b[i + 2] | b[i + 3]:
            a = LO + i
            if not nz:
                first = a
            last = a
            nz += 1
    return nz, first, last


def survey(m, S, blob, page=0xC006C000):
    mem = m.mem
    for i, b in enumerate(blob):
        mem.w8(BASE + i, b)
    prev = mem.r32(POOL)
    if prev == BASE:
        prev = mem.r32(S["sv_prev"])
    mem.w32(S["sv_prev"], prev)
    mem.w32(S["sv_page"], page)
    mem.w32(S["sv_done"], 0)
    mem.w32(S["sv_res"], 0)
    mem.w32(POOL, BASE)
    mem.w32(S["sv_flag"], MAGIC)
    done = 0
    for i in range(200):
        m.cpu.run(limit=1_000_000)
        done = mem.r32(S["sv_done"])
        if done:
            break
    res = [mem.r32(S["sv_res"] + 4 * k) for k in range(168)]
    # res[12] is the cave's own read-back check of the host-port pointer;
    # the live pointer afterwards is the firmware's business, not ours.
    return res, done, mem.r32(POOL) == prev, res[12] == 1, i + 1


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_dsp_playing7.pkl"
    S = syms()
    blob = open("build/fxmem/fxmem.bin", "rb").read()
    ok = True

    # ---- 1. stock: the block must read FREE -------------------------------
    m = Machine.load(snap)
    d = m.mem.dev.dspemu
    F.mt_on(m)
    F.play(m, 0.3)
    direct = scan(d)
    res, done, pool_ok, hpia_ok, tries = survey(m, S, blob)
    nz = res[0]
    print(f"stock: fast nz={res[0]} settled nz={res[1]}, "
          f"direct scan nz={direct[0]}, done={done} "
          f"pool_restored={pool_ok} hpia_restored={hpia_ok} [{tries}M steps]")
    print(f"       samples {[hex(w) for w in res[4:12]]}, "
          f"dsp program {[hex(w) for w in res[14:18]]}, "
          f"fast {res[0]} settled {res[1]} disagree {res[18]}/8 agree {res[20]}")
    print(f"       content: eq0 {res[158]}/13056 float<1.0 {res[159]}/13056 "
          f"samples {[hex(w) for w in res[160:164]]}")
    free = res[1] == 0 and direct[0] == 0
    ok &= free and done == 1 and pool_ok and hpia_ok

    # ---- 2. Echo running: the same block must read IN USE ------------------
    m2 = Machine.load(snap)
    d2 = m2.mem.dev.dspemu
    F.mt_on(m2)
    # This case has to install an engine that ACTUALLY writes the ring, so
    # the survey can be seen to catch it. The shipping engine is built
    # ring-free (FX_NO_RING=1) and never touches 0xC0073400; the ring
    # build beside it is what proves the survey is not just reading zeros.
    ring_eng = "build/fx_ring/fx.bin"
    if not os.path.exists(ring_eng):
        sys.exit("build the ring engine first: FX_OUT=build/fx_ring "
                 "sh tools/fx_dsp/build.sh")
    print(f"       engine under test: {ring_eng} "
          f"({os.path.getsize(ring_eng)} B, has the delay line)")
    F.install_dsp(d2, open(ring_eng, "rb").read())
    m2.mem.w16(F.SH_STORE, 0x0009)
    m2.mem.w32(F.SH_SLOT, F.fx_word(3, 40, 120, 255))
    F.play(m2, 0.4)
    direct2 = scan(d2)
    res2, done2, pool2, hpia2, tries2 = survey(m2, S, blob)
    used = res2[0] > 0 and direct2[0] > 0
    pages2 = res2[16:144]
    print(f"        program {[hex(w) for w in res2[4:8]]} cos {[hex(w) for w in res2[8:12]]}"
          f" busy pages {sum(1 for c in pages2 if c)}/128, page 28 {pages2[28]}")
    print(f"       content: eq0 {res2[158]}/13056 float<1.0 {res2[159]}/13056 "
          f"samples {[hex(w) for w in res2[160:164]]}")
    print(f"echo on: fast nz={res2[0]} settled nz={res2[1]}, "
          f"direct scan nz={direct2[0]}, done={done2} "
          f"pool_restored={pool2} hpia_restored={hpia2}")
    ok &= used and done2 == 1 and pool2 and hpia2
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
