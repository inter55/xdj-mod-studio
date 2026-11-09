#!/usr/bin/env python3
"""hotcue8_browse_test.py -- the hot-cue pads never take another track's cues.

Reddit, 2026-11-09: "I have track Y loaded, hit cue C, and it teleports to
track X's cue C"; some tracks "dominate". The deck's ANLZ ctx is one buffer
per stick and BROWSE's track preview parses the track under the cursor into
it (CLAUDE.md 40, the same reuse that hit the centre RGB waveform). The pads
re-read that buffer on every page change and 8-pad toggle, so after browsing
they wrote the previewed track's A-C into the live slots + pad models.

This gate loads the cue track (snapshot from tools/hotcue8_emu.py), lets the
pads settle, then OVERWRITES the ctx cue lists with another track's times --
what a browse preview does -- and checks:
  1. 8-pad toggle and back: live slots + both pad models still the loaded
     track's A-C (the 1.0.5 code writes the browsed track's)
  2. page 2 = the loaded track's D E F, page 1 again = its A-C
  3. a NEW track whose ctx is still a browsed track's (A-C disagree with the
     deck's own slots): D-H are not taken from it (no foreign D-H), A-C stay
     the deck's; once the ctx matches, D-H come back

Usage: .venv/bin/python tools/hotcue8_browse_test.py [$TMPDIR/hc_rb_loaded.pkl]
"""
import os
import sys

sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402
import hotcue8_test as T                 # noqa: E402
import hotcue8_emu as H                  # noqa: E402

SLOT, MA, MB = 0x0B6C4118, 0x0BA28800, 0x0BA2A52C
TIMES = T.TIMES
FAKE = [300000 + 1000 * i for i in range(8)]     # the "browsed" track
PREVIEW = 0x0BA3BCCC


def main():
    ck = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hc_rb_loaded.pkl")
    code, syms = T.build()
    m = Machine.load(ck)
    mem, dev = m.mem, m.mem.dev
    PP.install(mem)
    T.install_hc(mem, code, syms)
    mem.w8(MS.MSOFF, 0)
    ok = True

    def chk(label, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + label)
        ok &= bool(cond)

    def run(n):
        m.cpu.run(limit=n)

    def tap(x, y, hold=12_000_000, after=30_000_000):
        dev.panel_frame = panl.build_frame(touch=(x, y))
        run(hold)
        dev.panel_frame = panl.build_frame()
        run(after)

    tab = (PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2)

    def swipe(x0, x1, y=tab[1]):
        for x in (x0, (x0 + x1) // 2, x1):
            dev.panel_frame = panl.build_frame(touch=(x, y))
            run(8_000_000)
        dev.panel_frame = panl.build_frame()
        run(30_000_000)

    def slots():
        return [(mem.r16(SLOT + k * 0x40 + 0x0A), mem.r32(SLOT + k * 0x40 + 0x0C))
                for k in range(3)]

    def models():
        return [(mem.r16(b + k * 0x38 + 2), mem.r32(b + k * 0x38 + 4))
                for b in (MA, MB) for k in range(3)]

    def ctx_entries():
        """(address of the ms word) for every cue in both ctx lists"""
        out = []
        for n in range(1, 9):
            e = H.find_cue(m, n)
            if e:
                out.append((n, e + 0x20))
            e2 = H.find_cue2(m, n)
            if e2:
                out.append((n, e2 + 0x14))
        return out

    def pollute(times):
        for n, a in ctx_entries():
            mem.w32(a, times[n - 1])

    run(20_000_000)
    tap(709, 37)                                  # PERFORM
    run(30_000_000)
    own = [(k + 1, TIMES[k]) for k in range(3)]
    chk(f"loaded: slots A-C {slots()}", slots() == own)
    chk(f"captured this track's cues (hc_count {mem.r32(syms['hc_count'])})",
        mem.r32(syms["hc_count"]) == 8 and mem.r32(syms.get("hc_caps", 0x09790000)) >= 1)

    pollute(FAKE)                                 # the BROWSE preview
    run(30_000_000)
    tap(*tab)                                     # 8-pad view
    tap(*tab)                                     # and back to the stock row
    chk(f"after browsing + 8-pad toggle: slots {slots()}", slots() == own)
    chk("... and both pad models are still this track's",
        models() == own + own)
    swipe(100, 30)                                # page 2
    chk(f"page 2 = this track's D E F {slots()}",
        mem.r8(PP.HCPAGE) == 1 and slots() == [(4 + k, TIMES[3 + k]) for k in range(3)])
    swipe(30, 100)                                # back to page 1
    chk(f"page 1 again = this track's A-C {slots()}",
        mem.r8(PP.HCPAGE) == 0 and slots() == own and models() == own + own)

    # a NEW track while the ctx still holds a browsed track (A-C disagree
    # with the deck's own slots): no foreign D-H may be taken
    caps = mem.r32(syms.get("hc_caps", 0x09790000))
    mem.w32(PREVIEW, mem.r32(PREVIEW) ^ 0x5A5A5A5A)      # new overview = new track
    run(40_000_000)
    chk(f"new track, ctx foreign: not captured (hc_count {mem.r32(syms['hc_count'])})",
        mem.r32(syms.get("hc_caps", 0x09790000)) == caps and mem.r32(syms["hc_count"]) == 3)
    swipe(100, 30)
    chk(f"... page 2 has no foreign cues {slots()}",
        all(n == 0xFFFF or ms == 0 for n, ms in slots()) or
        all(ms not in FAKE for _, ms in slots()))
    swipe(30, 100)
    chk(f"... page 1 still the deck's own A-C {slots()}", slots() == own)
    pollute(TIMES)                                # the ctx is this track again
    run(40_000_000)
    chk(f"ctx matches again: captured (hc_count {mem.r32(syms['hc_count'])})",
        mem.r32(syms.get("hc_caps", 0x09790000)) == caps + 1 and mem.r32(syms["hc_count"]) == 8)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
