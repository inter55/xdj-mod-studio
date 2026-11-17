#!/usr/bin/env python3
"""ks_ring_test.py -- EMULATOR ONLY, offline.

2x2 A/B (mask x ring) for the key-shift boot wedge:
  masked  = the emu-proven flag 'KSR1'        (0x4B535231 in ksr_flag)
  unmask  = the deck-alike flag 'KSR1'|0x100   (0x4B535331)
  junk    = the deck's live ring words written into 0x0C8440..0x0C8470
  restore = the canonical cold-boot ring words (emu)

Quadrants:
  1 masked + junk      (A)     -- does junk alone wedge a masked attempt?
  2 masked + restore   (B)     -- control
  3 unmask  + junk     (C)     -- deck-like; expect the wedge (0x101)
  4 unmask  + restore  (D)     -- THE CANDIDATE FIX on the deck

Result lines print quadrant -> st / boot_ret so the decisive quadrant
per se is visible. Exit 0 = theory consistent with the deck.
"""
import os
import sys
import time
sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402
from keyshift_reload_emu import syms, B  # noqa: E402

SNAP = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_dsp_mounted6.pkl"
MAX_SLICES = int(os.environ.get("MAX_SLICES", "200"))
POOL_FLUSH = 0x08C92E20
RING = 0x0C8440
MASKED, UNMASKED = 0x4B535231, 0x4B535331
EMU_RING = [0x6D65733E, 0x00004449, 0x1183FFFC, 0x1183FFEC,
            0x00000002, 0x00000000, 0x08D78F0E, 0x00000007]
DECK_RING = [0x53FF7955, 0x7F797A79, 0xFF798A79, 0x79A7799D,
             0x79AA9F4B, 0xB379AEFF, 0xBA79B979, 0x79C9FF79]


def quad(tag, flag, ring_words, S, blob, base, max_slices=None):
    max_slices = max_slices or MAX_SLICES
    t0 = time.time()
    m = Machine.load(SNAP)
    mem, d = m.mem, m.mem.dev.dspemu
    m.cpu.run(limit=10_000_000)
    for i, b in enumerate(blob):
        mem.w8(base + i, b)
    prev = mem.r32(POOL_FLUSH)
    mem.w32(S["ksr_prev"], prev)
    mem.w32(POOL_FLUSH, base)
    for k, v in enumerate(ring_words):
        mem.w32(RING + 4 * k, v)
    ring_pre = [mem.r32(RING + 4 * k) for k in range(8)]
    mem.w32(S["ksr_flag"], flag)
    st = 0
    for i in range(max_slices):
        m.cpu.run(limit=10_000_000)
        st = mem.r32(S["ksr_status"])
        if st:
            break
        if i % 10 == 9:
            print(f"[{tag}] +{(i+1)*10}M st={st:#x} dsp={d.cpu.pc:#x} "
                  f"hpic={d.hpic:#x} pin={d.busy_pin()} "
                  f"F4={d.bus.read(0x1183FFF4,4):#x}", flush=True)
    dbg = [mem.r32(S["ksr_dbg"] + 4 * k) for k in range(24)]
    boot_ret, pins = dbg[10], dbg[21]
    print(f"[{tag}] ring_pre :", " ".join(f"{v:08x}" for v in ring_pre[:8]))
    print(f"[{tag}] DONE st={st:#x} boot_ret={boot_ret:#x} pins={pins:#x} "
          f"hpic post={dbg[11]:#x} hpia post={dbg[12]:#x} "
          f"[{time.time()-t0:.0f}s]", flush=True)
    m.save(f"/tmp/ks_ring_quad_{tag.replace(' ','_')}.pkl")
    return st, boot_ret


def main():
    S = syms()
    blob = open(os.path.join(B, "ksr.bin"), "rb").read()
    base = S["ksr_entry"]
    which = os.environ.get("QUADS", "A,B,C,D")
    wanted = [q.strip().upper() for q in which.split(",")]
    res = {}
    if "A" in wanted:
        res["A"] = quad("A mask+junk", MASKED, DECK_RING, S, blob, base)
    if "B" in wanted:
        res["B"] = quad("B mask+restore", MASKED, EMU_RING, S, blob, base)
    if "C" in wanted:
        res["C"] = quad("C unmask+junk", UNMASKED, DECK_RING, S, blob, base)
    if "D" in wanted:
        res["D"] = quad("D unmask+restore", UNMASKED, EMU_RING, S, blob, base)
    for q, (st, ret) in res.items():
        print(f"QUAD {q}: status {st:#x} boot_ret {ret:#x}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
