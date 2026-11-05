#!/usr/bin/env python3
"""verify_install.py MACHINE.pkl -- after a mock install (mock_install.py),
check the emulated deck's RAM holds exactly what every installer meant to
put there: each mod's code/data bytes, and each hook pointing at our code.
"""
import os
import struct
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools"), os.path.join(ROOT, "mods")]
os.chdir(ROOT)
from emu.machine import Machine          # noqa: E402


def main():
    m = Machine.load(sys.argv[1])
    mem = m.mem
    ok = True

    def region(name, base, want, skip=()):
        nonlocal ok
        got = bytes(mem.r8(base + i) for i in range(len(want)))
        bad = [i for i in range(len(want)) if got[i] != want[i] and (base + i) not in skip]
        print(f"  {'ok  ' if not bad else 'FAIL'} {name:22s} {len(want):6d} B @ {base:#010x}"
              + (f"  first diff +{bad[0]:#x}" if bad else ""))
        ok &= not bad

    def hook(name, pool, want):
        nonlocal ok
        v = mem.r32(pool)
        good = v == want if not callable(want) else want(v)
        print(f"  {'ok  ' if good else 'FAIL'} hook {name:17s} [{pool:#010x}] = {v:#010x}")
        ok &= good

    import rgb_overview as RO
    import rgb_detail as RD
    import vredirect as VR
    import mysettings as MS
    import perform_pads as PP
    import theme_switch as TS

    for a, b in RO.plan(RO.MODES["rgb"])[1]:
        if a == RO.MODE_ADDR:                 # any installed style is fine
            if mem.r8(a) not in RO.MODES.values():
                print(f"  FAIL waveform mode byte {mem.r8(a):#x}")
                ok = False
            continue
        if mem.r8(a) != b and not (RO.SNAP <= a < RO.SNAP + 0x44000):
            print(f"  FAIL overview byte {a:#x}")
            ok = False
            break
    else:
        print("  ok   overview cave + snapshot routine")
    region("centre (DCAVE)", RD.DCAVE, RD.dcave_bytes())
    cwb, _cws = RD.cw_build()
    region("centre colour module", RD.CW_BASE, cwb)
    region("PERFORM strip adapter", RD.DCAVE2, RD.dcave2_bytes())
    for name_, a_, v_ in (("centre insn", RD.INSN, RD.INSN_NEW),
                          ("centre row patch", RD.ROWINC[0], RD.ROWINC[2]),
                          ("PERFORM strip insn", RD.INSN2, RD.INSN2_NEW),
                          ("PERFORM strip row patch", RD.ROWINC2[0], RD.ROWINC2[2])):
        g_ = mem.r16(a_) == v_
        print(f"  {'ok  ' if g_ else 'FAIL'} {name_} [{a_:#x}] = {mem.r16(a_):#06x}")
        ok &= g_
    for a, b in VR.plan():
        if mem.r8(a) != b and a != VR.HOOK and not (VR.HOOK <= a < VR.HOOK + 4):
            print(f"  FAIL versions byte {a:#x}")
            ok = False
            break
    else:
        print("  ok   versions code")
    fb = mem.r8(PP.FEATB)                 # installer-chosen hidden-tab bits
    print(f"  ok   PERFORM hidden-tab bits (FEATB) = {fb:#x}")
    # PDKW (PST+56): the stub, or the light add-on's ink flip (5l re-arms it)
    pdk = mem.r32(PP.PDKW)
    lt_pdk = None
    try:
        for ln in open("build/lighttext/lt.syms"):
            a_, _, n_ = ln.split()
            if n_ == "_lt_pdk":
                lt_pdk = int(a_, 16)
    except OSError:
        pass
    g_ = pdk in (PP.PDKSTUB, lt_pdk)
    print(f"  {'ok  ' if g_ else 'FAIL'} PERFORM ink hook (PDKW) = {pdk:#x}")
    ok &= g_
    for base, blob in PP.blobs(fb):
        # PBUSY (PST+63) = the pads' runtime re-entrancy latch (1 while a
        # flush is in progress, so a snapshot can catch it set)
        region("PERFORM pads", base, blob,
               skip=set(range(PP.PDKW, PP.PDKW + 4)) | {PP.PBUSY}
               # the pads' exit literal: TOUCH FX chains in there (checked below)
               | set(range(PP.pbo_lit(), PP.pbo_lit() + 4)))
    import utility as UT                  # UTILITY rows (5u)
    _, us = UT.build()
    for p_ in UT.POOLS:
        hook("UTILITY table", p_, us["ut_tab"])
    hook("UTILITY count", UT.COUNT, UT.STOCK_N + UT.NOURS)
    ub = UT.image(bytes(mem.r8(us["ut_tab"] + i) for i in range(UT.STOCK_N * UT.REC)))[1]
    region("UTILITY module", us["ut_head"], ub,
           skip=range(us["ut_tab"], us["ut_tab"] + (UT.STOCK_N + UT.NOURS) * UT.REC))
    _tfx_touch = None
    try:
        for ln in open("build/touchfx/tfx.syms"):
            a_, _, n_ = ln.split()
            if n_ == "_tfx_touch_entry":
                _tfx_touch = int(a_, 16)
    except OSError:
        pass
    for pool, old, new in PP.hooks():
        if pool == 0x08C92E20:            # flush hook: hot cues -> phase? -> pads
            continue
        if pool == 0x08D5278C and _tfx_touch and mem.r32(pool) == _tfx_touch:
            continue                      # TOUCH FX in front: checked below
        hook("PERFORM pads", pool, new)
    # theme switching: code (entry word = on-switch) + tables
    (cb, code), (db, data) = TS.plan(TS.default_names())
    region("theme switch code", cb, code)
    region("theme tables", db, data, skip=range(db + 32, db + 52))
    # hot cue pages: hooked into the flush chain
    syms = {}
    for ln in open("build/hotcue8/hc.syms"):
        a, _, n = ln.split()
        syms[n.lstrip("_")] = int(a, 16)
    flush = mem.r32(0x08C92E20)
    # optional heads in front of the hot cues (each chains to what was live
    # when it was installed): light add-on (lt_prev), TOUCH FX
    # (tfx_prev_flush), phase meter (pm_prev). Walk them to the hot cues.
    def _syms(path):
        d = {}
        try:
            for ln in open(path):
                a, _, n = ln.split()
                d[n.lstrip("_")] = int(a, 16)
        except OSError:
            pass
        return d
    pm = _syms("build/phasemeter/pm.syms")
    lt = _syms("build/lighttext/lt.syms")
    tf = _syms("build/touchfx/tfx.syms")
    heads = {pm.get("pm_entry"): ("phase meter", pm.get("pm_prev")),
             lt.get("lt_flush"): ("light add-on", lt.get("lt_prev"))}
    heads.pop(None, None)
    for _ in range(4):
        if flush not in heads:
            break
        name, prevp = heads[flush]
        print(f"  ok   {name} in the flush chain")
        flush = mem.r32(prevp)
    print(f"  {'ok  ' if flush == syms['hc_entry'] else 'FAIL'} flush chain reaches the hot cues ({flush:#x})")
    ok &= flush == syms["hc_entry"]
    if tf and mem.r32(tf["tfx_flush_entry"]) == int.from_bytes(
            open("build/touchfx/tfx.bin", "rb").read()[:4], "little"):
        tb = open("build/touchfx/tfx.bin", "rb").read()
        skip = set()
        for k in ("tfx_prev_flush", "tfx_prev_touch"):
            skip |= set(range(tf[k], tf[k] + 4))
        region("TOUCH FX screen", tf["tfx_flush_entry"], tb, skip=skip)
        b = mem.r32(PP.pbo_lit())
        g = b == tf["tfx_flush_entry"] and mem.r32(tf["tfx_prev_flush"]) == PP.ORIG_BLIT
        print(f"  {'ok  ' if g else 'FAIL'} TOUCH FX flush entry behind the pads ({b:#x})")
        ok &= g
        t = mem.r32(0x08D5278C)
        pads_touch = PP.hooks()[0][2]
        g = t == tf["tfx_touch_entry"] and mem.r32(tf["tfx_prev_touch"]) == pads_touch
        print(f"  {'ok  ' if g else 'FAIL'} TOUCH FX touch hook -> pads ({t:#x})")
        ok &= g
        e = mem.r32(tf["tfx_engine"])
        d = getattr(mem.dev, "dspemu", None)
        call = d.bus.read(0xC0008D64, 4) if d is not None else None
        g = e == 1 and (call is None or call == 0x00ACEC10) and mem.r16(0x08D36B3A) == 0x0009
        print(f"  {'ok  ' if g else 'FAIL'} TOUCH FX engine flag {e}, DSP call word "
              f"{call if call is None else hex(call)}, BD8 store NOP {mem.r16(0x08D36B3A):#06x}")
        ok &= g
        if d is not None:
            fx = open("build/fx/fx.bin", "rb").read()
            got = bytes(d.bus.read(0xC005F4C0 + i, 1) for i in range(len(fx)))
            print(f"  {'ok  ' if got == fx else 'FAIL'} DSP FX engine code in DDR ({len(fx)} B)")
            ok &= got == fx
    prev = mem.r32(syms["hc_prev"])
    pads_flush = [n for p, o, n in PP.hooks() if p == 0x08C92E20][0]
    print(f"  {'ok  ' if prev == pads_flush else 'FAIL'} chain: hot cues -> {prev:#x} (pads)")
    ok &= prev == pads_flush
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
