#!/usr/bin/env python3
"""RETIRED 2026-11-12: this checked pad_arm's writes to the 16-byte per-pad
record; pad_arm is gone (it did not fix the jump: QEMU still played A-C).
The real cause was the slot's cue number (hc.c PADNUM). The jump itself is
now tested by tools/hotcue8_jump_emu.py (our emulator, real audio chip) and
tools/hotcue8_qemu_test.py (CALL=1 HOLD=2 QOFF=1). Kept for the record only.

hotcue8_recall_test.py -- the D-H recall fix, gated WITHOUT a seek.

The emulator never performs the deck's seek, so no test here can hear a pad
jump (docs/HOTCUE8_GRID.md §8). What decides recall is the per-pad record the
press path reads: 0x8D39940 takes the pad number, checks 0x0B6C3C84 + 4*i and
jumps to the point remembered in the 16-byte record array at 0x0BA14AD8 +
16*i (mirrored at 0x0C0CCA48 for the three live pads). hc.c now arms that
record with the pad's own cue (`pad_arm`) instead of clearing it.

This gate checks the record, which is what a seek would consume:

  1. pressing D-H arms {0, 0x40, cue_n*3/20, -1} for slot (n-1)%3;
  2. it is still armed on later flushes while the pad is held (the handler
     runs after the touch hook, in the same flush -- the borrow's restore
     window is exactly where a stale point would be read);
  3. releasing puts the empty pattern {0, 0, -1, -1} back;
  4. A-C never touch it: page 1 is stock, so its press must leave no trace.

Needs the snapshot tools/hotcue8_emu.py saves (mini stick, cues A-H at
5,13,..61 s loaded): $TMPDIR/hotcue8_loaded.pkl or argv[1].

  .venv/bin/python tools/hotcue8_recall_test.py
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

SLOT = 0x0B6C4118
PADMEM = 0x0C0CCA48
TIMES = [5000 + 8000 * i for i in range(8)]
EMPTY = (0, 0, 0xFFFFFFFF, 0xFFFFFFFF)
PADX = [116 + 63 * i + 29 for i in range(8)]
PADY = 263
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
    print("retired: run tools/hotcue8_jump_emu.py instead"); return 0

    ck = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hotcue8_loaded.pkl")
    if not os.path.exists(ck):
        print(f"SKIP: {ck} missing -- build it first with "
              "`.venv/bin/python tools/hotcue8_emu.py`")
        return 0
    code, syms = build()
    m = Machine.load(ck)
    mem, dev = m.mem, m.mem.dev
    PP.install(mem)
    base = syms["hc_table"]
    for a in range(base, syms.get("end", syms["hc_count"] + 4), 4):
        mem.w32(a, 0)
    for i, b in enumerate(code):
        mem.w8(base + i, b)
    hook, touch = 0x08C92E20, 0x08D5278C
    mem.w32(syms["hc_prev"], mem.r32(hook))
    mem.w32(syms["hc_tprev"], mem.r32(touch))
    mem.w32(syms["hc_armed"], 1)
    mem.w32(touch, syms["hc_tentry"])
    mem.w32(hook, syms["hc_entry"])
    mem.w8(MS.MSOFF, 0)

    def run(n):
        m.cpu.run(limit=n)

    def rec(k):
        r = PADMEM + k * 16
        return (mem.r32(r), mem.r32(r + 4), mem.r32(r + 8), mem.r32(r + 12))

    run(20_000_000)
    dev.panel_frame = panl.build_frame(touch=(709, 37))      # PERFORM
    run(12_000_000)
    dev.panel_frame = panl.build_frame()
    run(30_000_000)
    chk("PERFORM open", mem.r32(0x0BA371D4) == 7)

    tabx, taby = PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[0] + PP.TAB_H // 2
    dev.panel_frame = panl.build_frame(touch=(tabx, taby))    # 8-pad view
    run(12_000_000)
    dev.panel_frame = panl.build_frame()
    run(30_000_000)
    chk("8-pad view on", mem.r32(syms["hc_view"]) == 1 and mem.r8(PP.HCPAGE) == 0)

    # --- D-H: the record is armed with the pad's own cue -----------------
    for n in range(4, 9):
        k = (n - 1) % 3
        want_pos = TIMES[n - 1] * 3 // 20
        chk(f"pad {'ABCDEFGH'[n - 1]}: slot {k} record starts empty", rec(k) == EMPTY)
        dev.panel_frame = panl.build_frame(touch=(PADX[n - 1], PADY))
        run(6_000_000)                        # several flushes, still held
        held = rec(k)
        chk(f"pad {'ABCDEFGH'[n - 1]}: armed {held} -> pos {want_pos}",
            held[0] == 0 and held[1] == 0x40 and held[2] == want_pos
            and held[3] == 0xFFFFFFFF)
        run(12_000_000)                       # held longer: must stay armed
        chk(f"pad {'ABCDEFGH'[n - 1]}: still armed after more flushes",
            rec(k) == held)
        dev.panel_frame = panl.build_frame()
        run(30_000_000)                       # release + holdoff + restore
        chk(f"pad {'ABCDEFGH'[n - 1]}: record empty again after release",
            rec(k) == EMPTY)

    # --- A-C: page 1 is stock, the record must never be touched ----------
    for n in (1, 2, 3):
        k = n - 1
        dev.panel_frame = panl.build_frame(touch=(PADX[n - 1], PADY))
        run(6_000_000)
        chk(f"pad {'ABC'[n - 1]}: stock press leaves the record alone",
            rec(k) == EMPTY)
        dev.panel_frame = panl.build_frame()
        run(30_000_000)

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
