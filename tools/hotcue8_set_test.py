#!/usr/bin/env python3
"""hotcue8_set_test.py -- SET a hot cue on the deck from an empty D-H pad.

The pager gives every track pages A-C / D-F / G-H, but a press on an empty
pad used to do nothing at all (measured 2026-11-01). This gate covers the
fix: on pages 2-3, pressing an empty pad stores that cue at the current
playhead in RAM (state 6, number, ms) -- the stick is never written, and
page 1 is untouched so REC keeps recording A-C as before.

Emulator only. Needs the snapshot tools/hotcue8_emu.py saves (8-cue mini
stick, track loaded): $TMPDIR/hotcue8_loaded.pkl or argv[1]. Cue D (4) and
E (5) are BLANKED in the ANLZ ctx so page 2 has two empty slots, exactly
like a track exported with no cues.

Checks:
  1. page 2 slots empty before the press
  2. press pad A -> slot 0 becomes cue 4 at the playhead, hc_sets 1
  3. the cue survives paging away (2 -> 3 -> 1 -> 2): same ms
  4. press pad B -> slot 1 becomes cue 5 (independent cue, own slot)
  5. page 1 A-C are never rewritten by a page-2 press (stock row intact)
  6. no disk writes at any point (a stick write would be dangerous)
"""
import os
import subprocess
import sys

sys.path[:0] = [".", "tools"]
os.environ.setdefault("XDJ_DECK_SOUND", "0")

from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402
import hotcue8_emu as H                  # noqa: E402

SLOT = 0x0B6C4118
POS150 = 0x0C0E6D18
OUT = "screenshots/hotcue8"
ok = True


def chk(label, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}", flush=True)


def build():
    env = dict(os.environ, HC_BASE=hex(PP.HC_BASE), HCPAGE=hex(PP.HCPAGE),
               PMODE=hex(PP.PST + 2), MSOFF=hex(MS.MSOFF))
    subprocess.run(["sh", "tools/hotcue8/build.sh"], check=True, env=env,
                   capture_output=True)
    code = open("build/hotcue8/hc.bin", "rb").read()
    syms = {}
    for ln in open("build/hotcue8/hc.syms"):
        a, _, n = ln.split()
        syms[n.lstrip("_")] = int(a, 16)
    return code, syms


def main():
    ck = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hotcue8_loaded.pkl")
    os.makedirs(OUT, exist_ok=True)
    code, syms = build()
    m = Machine.load(ck)
    mem, dev = m.mem, m.mem.dev

    def run(n):
        m.cpu.run(limit=n)

    def tap(x, y, hold=12_000_000, after=30_000_000):
        dev.panel_frame = panl.build_frame(touch=(x, y))
        run(hold)
        dev.panel_frame = panl.build_frame()
        run(after)

    PP.install(mem)
    base = syms["hc_table"]
    for a in range(base, syms.get("end", syms["hc_count"] + 4), 4):
        mem.w32(a, 0)
    for i, b in enumerate(code):
        mem.w8(base + i, b)
    hook = 0x08C92E20
    mem.w32(syms["hc_prev"], mem.r32(hook))
    mem.w32(hook, syms["hc_entry"])
    mem.w8(MS.MSOFF, 0)

    run(20_000_000)
    tap(709, 37)                                   # PERFORM
    run(20_000_000)

    # blank cues D and E so page 2 has empty slots like a cue-less export
    for n in (4, 5):
        e = H.find_cue(m, n)
        if not e:
            sys.exit(f"snapshot has no cue {n} - rerun tools/hotcue8_emu.py")
        mem.w16(e + 0x0E, 0)

    def slot(k):
        s = SLOT + k * 0x40
        return (mem.r16(s + 0x08), mem.r16(s + 0x0A), mem.r32(s + 0x0C))

    tabx, taby = PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2
    disk = getattr(dev, "usb_disk", None)
    w0 = getattr(disk, "writes", 0)
    tap(tabx, taby)                                # page 2
    chk("page 2 open", mem.r8(PP.HCPAGE) == 1)
    chk("page 2 slots empty", [slot(k)[0] != 6 for k in range(3)])

    before = mem.r32(POS150)
    tap(166, 263)                                  # pad A: set cue D
    chk(f"pad A set cue D ({slot(0)}), hc_sets "
        f"{mem.r32(syms['hc_sets'])}", slot(0)[0] == 6 and slot(0)[1] == 4
        and mem.r32(syms["hc_sets"]) == 1)
    # the playhead advances during the hold, so the stored ms is the position
    # at the press frame: within a few 1/150 s units of the pre-press read
    lo, hi = before * 20 // 3, (before + 8) * 20 // 3
    chk(f"cue D ms {slot(0)[2]} at the playhead ({lo}..{hi})", lo <= slot(0)[2] <= hi)

    for _ in range(3):                              # 2 -> 3 -> 1 -> 2
        tap(tabx, taby, after=25_000_000)
    chk(f"back on page 2 ({mem.r8(PP.HCPAGE)})", mem.r8(PP.HCPAGE) == 1)
    chk(f"cue D survived paging ({slot(0)})", slot(0)[0] == 6 and slot(0)[1] == 4)

    tap(281, 263)                                  # pad B: set cue E
    chk(f"pad B set cue E ({slot(1)}), hc_sets "
        f"{mem.r32(syms['hc_sets'])}", slot(1)[0] == 6 and slot(1)[1] == 5
        and mem.r32(syms["hc_sets"]) == 2)
    chk("slot 0 untouched by pad B", slot(0)[1] == 4)

    for _ in range(2):                              # 2 -> 3 -> 1
        tap(tabx, taby, after=25_000_000)
    chk(f"page 1 A-C intact: {[slot(k) for k in range(3)]}",
        [slot(k)[1] for k in range(3)] == [1, 2, 3])

    w1 = getattr(disk, "writes", 0)
    chk(f"no disk writes ({w0} -> {w1})", w0 == w1)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
