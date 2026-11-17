#!/usr/bin/env python3
"""rgb_sametrack_test.py -- EMULATOR: colour waveforms never use ANOTHER
track's colours (2026-11-15, hyyzt's LINK report).

    .venv/bin/python tools/rgb_sametrack_test.py [SNAP]

SNAP (default cache/emu_flush_playing.pkl) = a track from the deck's own
stick playing. Installs the RGB overview (rgb_overview.plan: cave + same-track
check + snapshot routine, the bytes the deck gets), then calls the snapshot
routine (rgb_detail.COPY5) the way the overview cave does:

  1. the cache holds this track       -> colour slots filled, hash stored
  2. called again                     -> nothing copied again (same track)
  3. the cache holds ANOTHER track's   -> slots emptied (= stock blue),
     analysis (what a LINK load looks     hash 0
     like: the deck's own cache is not
     the loaded track)
  4. called again, still another track -> still empty (asks every paint)
  5. the cache gets this track back   -> slots filled again
  6. a real repaint (PERFORM open/close) with the other track in the cache
     -> the snapshot is still empty, the centre module's PWV7 slot too
"""
import os
import sys
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import rgb_overview as RO                # noqa: E402
import rgb_detail as RD                  # noqa: E402

HALT = 0x0F300000


def main():
    snap = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_flush_playing.pkl"
    m = Machine.load(snap)
    mem, c = m.mem, m.cpu
    ok = True

    def chk(label, good):
        nonlocal ok
        print(("  ok   " if good else "CHECK FAIL: ") + label, flush=True)
        ok &= bool(good)

    for a, b in RO.plan(1)[1]:
        mem.w8(a, b)
    ctx = mem.r32(RO.CTX_TAB)
    pw = ctx + RO.PWAV_OFF + 20
    own = bytes(mem.r8(pw + i) for i in range(400))
    has4 = any(mem.r32(ctx + RO.IDX_OFF + 4 + 4 + 16 * k) == RO.TAG_PWV4
               for k in range(min(mem.r32(ctx + RO.IDX_OFF), 32)))
    print(f"ctx {ctx:#x}, track has PWV4: {has4}")

    def copy5():
        saved = {r: getattr(c, r) for r in ("pc", "pr", "gbr", "mach", "macl", "sr")}
        gr = [c.gr(i) for i in range(16)]
        c.pr, c.pc = HALT, RD.COPY5
        m.run(limit=20_000_000, stop_on_pc=[HALT])
        for i, v in enumerate(gr):
            c.set_gr(i, v)
        for r, v in saved.items():
            setattr(c, r, v)

    def slots():
        return mem.r32(RO.SLOT4), mem.r32(RO.SLOT5), mem.r32(RO.SNAP_HASH)

    def other():                         # another track's analysis in the cache
        for i in range(400):
            mem.w8(pw + i, own[(i * 7 + 113) % 400] ^ 0x21)

    def mine():
        for i, b in enumerate(own):
            mem.w8(pw + i, b)

    mem.w32(RO.SNAP_HASH, 1)             # as if a new track was just loaded
    copy5()
    s4, s5, h = slots()
    chk(f"1 own track: PWV4 slot filled ({s4 == RO.TAG_PWV4}), hash {h:#x}",
        h not in (0, 1) and (s4 == RO.TAG_PWV4 or not has4))
    h1 = h
    mem.w32(RO.SLOT4 + 4, 0x5A5A5A5A)    # marker: a re-copy would overwrite it
    copy5()
    chk("2 same track again: not copied again", mem.r32(RO.SLOT4 + 4) == 0x5A5A5A5A
        and slots()[2] == h1)

    other()
    mem.w32(RO.SNAP_HASH, 1)
    copy5()
    s4, s5, h = slots()
    chk(f"3 another track in the cache: slots empty ({s4:#x} {s5:#x}), hash {h:#x}",
        s4 == 0 and s5 == 0 and h == 0)
    copy5()
    chk("4 still another track: still empty, hash 0", slots() == (0, 0, 0))

    mine()
    copy5()
    s4, s5, h = slots()
    chk(f"5 own track back: filled again, hash {h:#x}",
        h == h1 and (s4 == RO.TAG_PWV4 or not has4))

    # 6: real paints, the centre module (cw) installed as the deck has it
    RD.install(mem)
    other()
    mem.w32(RO.SNAP_HASH, 1)
    for _ in range(2):
        mem.dev.panel_frame = panl.build_frame(touch=(705, 37))
        m.run(limit=8_000_000)
        mem.dev.panel_frame = panl.build_frame()
        m.run(limit=200_000_000)
    s4, s5, h = slots()
    syms = {}
    for ln in open("build/centre/cw.syms"):
        a_, _, nm = ln.split()
        syms[nm.lstrip("_")] = int(a_, 16)
    calls = mem.r32(syms["cw_calls"]) if "cw_calls" in syms else -1
    s7 = mem.r32(RD.SLOT7) if hasattr(RD, "SLOT7") else 0
    chk(f"6 repaint with another track cached: slots empty, hash {h:#x}, "
        f"PWV7 slot {s7:#x}, centre calls {calls}",
        s4 == 0 and s5 == 0 and h == 0 and s7 == 0)
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
