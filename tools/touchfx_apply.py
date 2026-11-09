#!/usr/bin/env python3
"""touchfx_apply.py --survey | (install) [--engine-ok] | --revert -- the
TOUCH FX panel on the DECK (tools/touchfx/tfx.c; emulator PASS:
tools/touchfx.py emu + tools/touchfx_e2e.py). RAM only, reboot = stock.
NOT YET RUN ON HARDWARE.

  --survey     READ-ONLY: the cave (code + state, 0x09844000..) must read
               zero on THIS deck (deck RAM differs from the emulator: always
               survey, CLAUDE.md sec. 38/76)
  (no flag)    chunked code write (resumable, foreign-data check), then a
               fresh short session arms: tfx_prev_touch = the live touch
               hook, tfx_prev_flush = the stock blit the pads' normal exit
               jumps to; LW self-test, then the touch hook and the pads'
               exit literal (LW = atomic: both are read every frame) -- the
               panel draws after the pads (its tab sits in their column)
  --engine-ok  also mark the DSP FX engine as present (deck_session passes
               it after `keyshift_hot_apply.py --engine fx` reported PASS);
               without it the panel says FX ENGINE NOT INSTALLED and never
               writes the effect word
  --revert     both hooks back to the values they chained
Install the PERFORM pads first (the panel uses their tab state).
"""
import os
import struct
import sys

sys.path.insert(0, "tools")
HOOK_FLUSH, HOOK_TOUCH = 0x08C92E20, 0x08D5278C
STOCK_TOUCH = 0x08DD0128
OUT = "build/touchfx"


def syms():
    s = {}
    for line in open(f"{OUT}/tfx.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def main():
    args = sys.argv[1:]
    arg = next((x for x in args if x in ("--survey", "--revert")), "")
    engine_ok = "--engine-ok" in args
    s = syms()
    base = s["tfx_flush_entry"]
    img = open(f"{OUT}/tfx.bin", "rb").read()
    end = s["end"]
    ok_file = os.path.expanduser(f"~/.xdj700_tfx_survey_ok.{base:08x}")
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, read_block, send
    import perform_pads as PP
    pads_touch = PP.hooks()[0][2]
    # The 8-hot-cue view (tools/hotcue8, since 1.0.4) hooks the touch pool
    # too and is installed BEFORE TOUCH FX, so its entry is a normal live
    # value here. Without it every default install (8 hot cues on) refused
    # with "unknown touch hook 0x979003c" and TOUCH FX never went in.
    known_touch = {STOCK_TOUCH, pads_touch}
    try:
        for ln in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "build", "hotcue8", "hc.syms")):
            a_, _, n_ = ln.split()
            if n_.lstrip("_") == "hc_tentry":
                known_touch.add(int(a_, 16))
    except OSError:
        pass
    # the flush side chains in BEHIND the pads (their normal-exit literal,
    # stock blit 0x08E7D278), so the panel and its tab draw after them
    HOOK_FLUSH = PP.pbo_lit()

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    if not arg and not os.path.exists(ok_file):
        sys.exit(f"REFUSED: run --survey first for {base:#x}")
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        lf, lt = rd32(h, HOOK_FLUSH), rd32(h, HOOK_TOUCH)
        print(f"pads exit = {lf:#x}, touch hook = {lt:#x} (pads {pads_touch:#x}, "
              f"ours {base:#x} / {s['tfx_touch_entry']:#x})")
        if arg == "--survey":
            blk = read_block(h, base, end - base)
            nz = [i for i, b in enumerate(blk) if b]
            if nz:
                sys.exit(f"SURVEY FAIL: {len(nz)} nonzero bytes, first at "
                         f"{base + nz[0]:#x} - nothing written")
            open(ok_file, "w").write("zero\n")
            print(f"SURVEY OK: {base:#x}..{end:#x} all zero")
            return 0
        if arg == "--revert":
            done = 0
            if lt == s["tfx_touch_entry"]:
                prev = rd32(h, s["tfx_prev_touch"])
                lw(h, HOOK_TOUCH, prev)
                if rd32(h, HOOK_TOUCH) != prev:
                    sys.exit("FAIL: touch hook not restored - REBOOT the deck")
                done += 1
            if lf == base:
                prev = rd32(h, s["tfx_prev_flush"])
                lw(h, HOOK_FLUSH, prev)
                if rd32(h, HOOK_FLUSH) != prev:
                    sys.exit("FAIL: flush hook not restored - REBOOT the deck")
                done += 1
            print("RESULT: reverted" if done else "not armed - nothing to do")
            return 0
        if lf == base and lt == s["tfx_touch_entry"]:
            if engine_ok:
                lw(h, s["tfx_engine"], 1)
            print("RESULT: PASS (already armed)")
            return 0
        # a half-armed earlier run: never chain the module to itself
        if lf == base:
            lf = rd32(h, s["tfx_prev_flush"])
        if lt == s["tfx_touch_entry"]:
            lt = rd32(h, s["tfx_prev_touch"])
        if lf == base or lt == s["tfx_touch_entry"]:
            sys.exit("FAIL: hook chain points at itself - REBOOT the deck")
        if lt not in known_touch:
            sys.exit(f"FAIL: unknown touch hook {lt:#x} - nothing written")
        if lf != PP.ORIG_BLIT:
            sys.exit(f"FAIL: pads exit literal {lf:#x} is not the stock blit "
                     "(pads missing or another build) - nothing written")
        # code + data (+ zeroed state); the prev words chain what is live now
        want = bytearray(img) + bytes(end - base - len(img))
        live_words = []
        for k, val in (("tfx_prev_flush", lf), ("tfx_prev_touch", lt)):
            o = s[k] - base
            want[o:o + 4] = struct.pack("<I", val)
            live_words += range(base + o, base + o + 4)
        cur = read_block(h, base, len(want))
        diff = [i for i, (a, b) in enumerate(zip(cur, want)) if a != b]
        if diff:
            bad = [i for i in diff if cur[i] and base + i not in live_words]
            if bad:
                sys.exit(f"FAIL: cave +{bad[0]:#x} holds foreign data - nothing armed")
            import fastload
            left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                           allow_live=live_words, cur=cur)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
            if fastload.arm_later():
                print("code complete - run again to arm (fresh short session)")
                return 0
        scratch = s["tfx_draws"]                      # our own counter word
        for val in (0x12345678, 0):
            lw(h, scratch, val)
            if rd32(h, scratch) != val:
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        if engine_ok:
            lw(h, s["tfx_engine"], 1)
            if rd32(h, s["tfx_engine"]) != 1:
                sys.exit("FAIL: engine flag - nothing armed")
        lw(h, HOOK_TOUCH, s["tfx_touch_entry"])       # touch first: it only
        if rd32(h, HOOK_TOUCH) != s["tfx_touch_entry"]:   # acts once drawn
            lw(h, HOOK_TOUCH, lt)
            sys.exit("FAIL: touch hook verify - restored; REBOOT before playing")
        lw(h, HOOK_FLUSH, base)
        if rd32(h, HOOK_FLUSH) != base:
            lw(h, HOOK_FLUSH, lf)
            lw(h, HOOK_TOUCH, lt)
            sys.exit("FAIL: flush hook verify - restored; REBOOT before playing")
        print("RESULT: PASS - PERFORM > TOUCH FX (in the tab stack). The "
              "effects need MASTER TEMPO ON" +
              ("." if engine_ok else "; the DSP FX engine is NOT marked installed "
               "(run keyshift_hot_apply.py --engine fx, then this with --engine-ok)."))
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
