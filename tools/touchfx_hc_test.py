#!/usr/bin/env python3
"""touchfx_hc_test.py -- EMULATOR: nothing shows through the TOUCH FX panel
from the 8 hot cue pages (2026-11-16 deck report: "the screen randomly
showing things underneath").

    .venv/bin/python tools/touchfx_hc_test.py [FIXTURE]

FIXTURE default $TMPDIR/hc_s0/hc_rb_s0_loaded.pkl (tools/hotcue8_emu.py
HC_LAYOUT=rekordbox). Installs the pads, the 8 hot cue pages and TOUCH FX in
the deck's order, pages the hot cues to D-F, opens TOUCH FX, and checks that
the D/E/F letters are NOT painted over the panel for many frames. HC_NO_TFX=1
builds the hot cue module the old way (it must FAIL then).
"""
import os
import sys
sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
from ghosttap_emu import fb_png          # noqa: E402
import perform_pads as PP                # noqa: E402
import hotcue8_test as HT                # noqa: E402
import touchfx as TF                     # noqa: E402

FIX = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "hc_s0", "hc_rb_s0_loaded.pkl")


def main():
    m = Machine.load(FIX)
    mem = m.mem
    PP.install(mem)
    hcode, hs = HT.build()
    HT.install_hc(mem, hcode, hs)
    s = TF.install(mem)
    mem.w16(TF.NOPSITE, 0x0009)
    mem.w8(PP.MSOFF, 0)

    def run(n, frame=None):
        mem.dev.panel_frame = frame or panl.build_frame()
        m.cpu.run(limit=n)

    def tap(x, y):
        run(12_000_000, panl.build_frame(touch=(x, y)))
        run(30_000_000)

    run(20_000_000)
    tap(709, 37)                                     # PERFORM
    tx = PP.TAB_X + PP.TAB_W // 2
    tap(tx, PP.TAB_Y[0] + PP.TAB_H // 2)             # HOT CUE tab: next page (D-F)
    page = mem.r8(PP.HCPAGE)
    ty = 264 + 7 if mem.r8(PP.FEATB) & 1 else 297 + 6  # tfx.c TAB_Y
    tap(16 + 46, ty)                                 # TOUCH FX tab
    open_ = mem.r32(s["tfx_mode"])
    draws0 = mem.r32(hs["hc_draws"])
    run(200_000_000)
    draws = mem.r32(hs["hc_draws"]) - draws0
    out = "screenshots/touchfx/hc_under.png"
    fb_png(m, out)
    print(f"hot cue page {page}, TOUCH FX open {open_}, letter paints while open {draws}")
    ok = page == 1 and open_ != 0 and draws == 0
    print("screenshot:", out)
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
