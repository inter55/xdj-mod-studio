#!/usr/bin/env python3
"""keyshift_hot_emu.py -- EMULATOR ONLY: the NO-RESET delivery of real-time
key shift (tools/keyshift_dsp/ksh.c, build/keyshift/ksh.bin).

Installs the cave exactly as the deck installer does (cave bytes, ksh_prev =
live flush pool, pool -> cave, flag), on a snapshot with the real DSP
running and no track loaded, then:
  1. flag 'KSHP' (read-only probe)       -> status 6, DSP memory untouched
  2. flag 'KSH1' (install)               -> status 1, call word + ks code
                                             in DDR, HPIA restored,
                                             DSP keeps running
  3. flag 'KSH0' (revert) on a copy      -> status 5, stock call word
Saves OUT (default cache/emu_ksh_installed.pkl). Then, for the ear-less
pitch proof:
  MOUNTED= STICK=cache/stick7.img PUSHES=7 NOPLAY=1 PLAY_STEPS=1.5e9 \
    SAVE_PLAYING=cache/emu_ksh_playing.pkl \
    .venv/bin/python tools/dsp_play_test.py cache/emu_ksh_installed.pkl
  SEMIS=2 .venv/bin/python tools/keyshift_dsp_emu.py cache/emu_ksh_playing.pkl
"""
import os
import sys
import time
sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402

B = os.environ.get("KSH_DIR", os.path.join("build", "keyshift"))
# KSH_DIR=build/fx KSH_PAYLOAD=fx.bin: the same installer carrying the TOUCH FX
# engine (tools/fx_dsp/build_fxh.sh) instead of key shift
PAYLOAD = os.environ.get("KSH_PAYLOAD", "ks.bin")
POOL_FLUSH = 0x08C92E20
CALL, CALL_OLD, CALL_NEW = 0xC0008D64, 0x0027D290, 0x00ACEC10
LAND = 0xC005F4C0
FLAGS = {"probe": 0x4B534850, "install": 0x4B534831, "revert": 0x4B534830}


def syms():
    s = {}
    for l in open(os.path.join(B, "ksh.syms")):
        a, _, n = l.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def arm(m, S, blob, what):
    mem = m.mem
    base = S["ksh_entry"]
    for i, b in enumerate(blob):
        mem.w8(base + i, b)
    prev = mem.r32(POOL_FLUSH)
    if prev == base:
        prev = mem.r32(S["ksh_prev"])
    mem.w32(S["ksh_prev"], prev)
    mem.w32(S["ksh_status"], 0)
    mem.w32(S["ksh_tries"], 0)
    mem.w32(POOL_FLUSH, base)
    mem.w32(S["ksh_flag"], FLAGS[what])
    st = 0
    for i in range(200):
        m.cpu.run(limit=5_000_000)
        st = mem.r32(S["ksh_status"])
        if st and st != 8:
            break
    dbg = [mem.r32(S["ksh_dbg"] + 4 * k) for k in range(16)]
    print(f"{what}: status {st} after ~{(i+1)*5}M steps; pool back "
          f"{mem.r32(POOL_FLUSH) == prev}; dbg {[hex(x) for x in dbg]}", flush=True)
    return st, prev


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_dsp_mounted6.pkl"
    out = os.environ.get("OUT", "cache/emu_ksh_installed.pkl")
    t0 = time.time()
    S = syms()
    blob = open(os.path.join(B, "ksh.bin"), "rb").read()
    ks = open(os.path.join(B, PAYLOAD), "rb").read()
    m = Machine.load(snap)
    mem, d = m.mem, m.mem.dev.dspemu
    assert d is not None, "snapshot has no real DSP"
    m.cpu.run(limit=10_000_000)
    land0 = bytes(d.bus.read(LAND + i, 1) for i in range(len(ks)))
    print("landing area before:", "all FF" if land0 == b"\xff" * len(ks) else land0[:16].hex())

    st_p, _ = arm(m, S, blob, "probe")
    untouched = (d.bus.read(CALL, 4) == CALL_OLD and
                 bytes(d.bus.read(LAND + i, 1) for i in range(len(ks))) == land0)
    hpia0 = d.hpia
    st_i, prev = arm(m, S, blob, "install")
    call = d.bus.read(CALL, 4)
    ddr = bytes(d.bus.read(LAND + i, 1) for i in range(len(ks)))
    hpia_ok = mem.r32(S["ksh_dbg"] + 4 * 11) == mem.r32(S["ksh_dbg"])
    print(f"call word {call:#x}, ks in DDR {'==' if ddr == ks else '!='} ks.bin, "
          f"HPIA restored {hpia_ok} (now {d.hpia:#x}, before probe {hpia0:#x}) "
          f"[{time.time()-t0:.0f}s]", flush=True)
    for k in range(3):
        m.cpu.run(limit=100_000_000)
        print(f"  +{(k+1)*100}M: DSP err={d.error} running={d.running} pc={d.cpu.pc:#x}",
              flush=True)
    ok = (st_p == 6 and untouched and st_i == 1 and call == CALL_NEW and ddr == ks
          and hpia_ok and mem.r32(POOL_FLUSH) == prev and d.error is None and d.running)
    m.save(out)
    print("saved", out)
    # revert on a fresh copy of the installed state
    m2 = Machine.load(out)
    st_r, _ = arm(m2, S, blob, "revert")
    rv = m2.mem.dev.dspemu.bus.read(CALL, 4)
    print(f"revert: call word {rv:#x}")
    ok = ok and st_r == 5 and rv == CALL_OLD
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
