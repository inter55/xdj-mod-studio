#!/usr/bin/env python3
"""fxmem_wide.py -- the proven survey cave at four map bases, 8 MB in total.

Why four passes and not one bigger map: the cave is exactly at its 1,792 B cap
(a 64 KB-page build lands on 1,792 B and never completed in the emulator), and
nothing outside the chip can read chip memory -- host reads of 0xC0xxxxxx come
back as stable junk while RAM reads work. So the map base is a build-time
constant and each pass covers 2 MB.

Each pass: hook must be stock, cave written, armed, read, hook put back. Only
the cave's own RAM words are written; no engine is installed, and a power cycle
always returns the deck to stock.

    .venv/bin/python tools/fxmem_wide.py --build   # the four variants, once
    .venv/bin/python tools/fxmem_wide.py           # the wide run on the deck

Exit: 0 for four completed passes plus sound landmarks, 1 if anything stopped.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fxmem_probe as P                                      # noqa: E402

SPAN = 0x200000
BASES = [0xC0000000 + i * SPAN for i in range(4)]
ROOT = os.path.normpath(os.path.join(HERE, ".."))


def vdir(base):
    return os.path.join(ROOT, "build", f"fxmem_base_{base:08x}")


def variant(base):
    d = vdir(base)
    binp, symp = os.path.join(d, "fxmem.bin"), os.path.join(d, "fxmem.syms")
    if not (os.path.exists(binp) and os.path.exists(symp)):
        sys.exit(f"no build for {base:#010x}: "
                 ".venv/bin/python tools/fxmem_wide.py --build")
    syms = {}
    for line in open(symp):
        a, _, n = line.split()
        syms[n.lstrip("_")] = int(a, 16)
    return syms, open(binp, "rb").read()


def build_all():
    ok = True
    for base in BASES:
        env = dict(os.environ, FXMEM_BASE=f"0x{base:X}", FXMEM_OUT=vdir(base))
        r = subprocess.run(["sh", os.path.join(HERE, "fx_dsp", "build_fxmem.sh")],
                           env=env, capture_output=True, text=True)
        hit = [ln for ln in r.stdout.splitlines() if ln.startswith("fxmem.bin")]
        print(f"  {base:#010x}: {'ok   ' if r.returncode == 0 else 'FAILED'} "
              f"{hit[0] if hit else ''}")
        if r.returncode:
            print(r.stdout[-600:] + r.stderr[-600:])
            ok = False
    return 0 if ok else 1


def pass_map(h, base, S, blob):
    """One 2 MB map at `base`. Returns (res, done); never leaves a live hook."""
    cave = S["sv_entry"]
    hook = P.r32(h, P.POOL)
    if hook != P.STOCK:
        sys.exit(f"STOP: the display-flush hook holds {hook:#010x}, not stock "
                 f"{P.STOCK:#010x}.\n  Power-cycle the deck, load a track and "
                 "press play, then run this again. Nothing was written.")
    if bytes(P.read_block(h, cave, len(blob))) != blob:
        P.write_code(h, cave, blob)
        if bytes(P.read_block(h, cave, len(blob))) != blob:
            sys.exit("FAIL: cave read-back mismatch; nothing was armed")
    P.w32_verify(h, S["sv_prev"], P.STOCK, "sv_prev")
    P.w32_verify(h, S["sv_page"], base, "page address")
    P.w32_verify(h, S["sv_done"], 0, "done flag")
    P.w32_verify(h, S["sv_flag"], P.MAGIC, "arm word")
    P.w32(h, P.POOL, cave)
    if P.r32(h, P.POOL) != cave:
        P.w32(h, P.POOL, P.STOCK)
        sys.exit("FAIL: the hook word did not take; stock put back")
    done, end = 0, time.time() + P.WAIT_S
    while time.time() < end:
        done = P.r32(h, S["sv_done"])
        if done:
            break
        time.sleep(0.05)
    res = [P.r32(h, S["sv_res"] + 4 * i) for i in range(P.N_RES)]
    if P.r32(h, P.POOL) != P.STOCK:
        P.w32(h, P.POOL, P.STOCK)
    return res, done


def summarise(maps):
    """Pure: stitch per-pass counts into one picture. Returns exit code."""
    pages = sum(len(c) for _, _, c in maps)
    busy = sum(1 for _, _, c in maps for x in c if x)
    page = maps[0][1]
    runs, cur = [], None
    for base, pg, counts in maps:
        for i, c in enumerate(counts):
            if c:
                if cur:
                    runs.append(cur)
                    cur = None
            else:
                a = base + i * pg
                cur = (cur[0], a + pg) if cur else (a, a + pg)
    if cur:
        runs.append(cur)
    print(f"whole span {BASES[0]:#010x}..{BASES[-1] + SPAN:#010x}: {busy}/{pages} "
          f"pages hold data, {page // 1024} KB pages")
    if not runs:
        print("  no empty page anywhere in 8 MB")
        return 0
    for a, b in runs[:12]:
        print(f"  empty 0x{a:010X}-0x{b:010X}  ({b - a:#x} B)")
    if len(runs) > 12:
        print(f"  ... and {len(runs) - 12} more")
    a, b = max(runs, key=lambda r: r[1] - r[0])
    print(f"biggest empty stretch 0x{a:010X}-0x{b:010X} ({b - a:#x} B)")
    if b - a >= 0xD000:
        print("  big enough for a 52 KB delay line -- but one sample per page is\n"
              "  a screen, not a proof. Prove it word by word on a stock boot:\n"
              f"    tools/fxmem_probe.py --page 0x{a:08X}")
    return 0


def main():
    if "--build" in sys.argv:
        return build_all()
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import send

    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"STOP: found {len(cands)} deck interface(s). Check the cable, "
                 "switch the deck on, quit rekordbox / DJ apps.")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    maps, first = [], None
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        for base in BASES:
            S, blob = variant(base)
            t = time.time()
            res, done = pass_map(h, base, S, blob)
            if first is None:
                first = res
            if not done:
                print(f"{base:#010x}: the survey did not finish "
                      f"[{time.time()-t:.1f}s] - stopping, nothing armed")
                return 1
            page, n = res[14], res[13]
            counts = res[16:16 + n]
            maps.append((base, page, counts))
            print(f"{base:#010x}..+{n * page // 0x100000} MB: "
                  f"{sum(1 for c in counts if c)}/{n} pages hold data "
                  f"[{time.time()-t:.1f}s]", flush=True)
    finally:
        h.close()
    prog = " ".join(f"{w:08X}" for w in first[4:8])
    cos = " ".join(f"{w:08X}" for w in first[8:12])
    ok = first[8] == 0x3F800000
    print(f"landmarks: program {prog}  cos {cos} "
          f"({'1.0f as expected' if ok else 'NOT 1.0f - do not believe these maps'})")
    if not ok:
        return 1
    return summarise(maps)


if __name__ == "__main__":
    sys.exit(main())
