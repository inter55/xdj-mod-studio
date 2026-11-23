#!/usr/bin/env python3
"""trackfilter_apply.py --survey | (install) | --revert -- the on-deck TRACK
FILTER (tools/trackfilter/, emulator PASS: tools/trackfilter.py emu) on the
DECK. RAM only, reboot = stock. NOT YET RUN ON HARDWARE.

  --survey   READ-ONLY: the whole module area (code + state) must read zero
             on THIS deck (deck RAM differs from the emulator, CLAUDE.md 38);
             writes ~/.xdj700_tf_survey_ok.<base>.<deck serial> on success
  (no flag)  chunked code write (resumable, foreign-data check; tf_prev =
             the stock copy routine), then a fresh run arms: LW self-test on
             our own counter, then the three list-copy pools -> tf_copy
             (each LW verified; a failure puts every pool back)
  --revert   the three pools back to the stock routine (each verified)

The settings are the TRACK FILTER rows in hold-MENU UTILITY (tools/utility,
step 5u); they say NOT INSTALLED until this is armed. A changed filter
shows when a list is opened again (BACK, then push).
"""
import os
import struct
import sys

sys.path[:0] = [".", "tools"]
import trackfilter as TF     # noqa: E402


# the deck's own filter back end the module calls (fw 1.15, checked before
# anything is armed): PRED 0x08B357CC, SETCOND 0x08B34D5A, the four checks,
# and the literal that points PRED at the condition store TF_ST 0x09603674
STOCK = {0x08B357CC: "962fa62fb62fc62f", 0x08B34D5A: "862f962fa62fb62f",
         0x08B35854: "7436600993244992", 0x08B35F6C: "6d6202e11332108b",
         0x08B3602A: "e62f53616d65436e", 0x08B36138: "6d6201e11332018b",
         0x08B361F8: "6d6201e11332038b"}


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    img, s = TF.build()
    base, end = s["tf_head"], s["end"]
    outside = sorted((v, k) for k, v in s.items() if not base <= v <= end)
    if outside:
        sys.exit(f"REFUSED: {outside[0][1]} at {outside[0][0]:#x} is outside "
                 f"the module {base:#x}..{end:#x}")
    want = bytearray(img) + bytes(end - base - len(img))
    po = s["tf_prev"] - base
    want[po:po + 4] = struct.pack("<I", TF.STOCK_COPY)
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, read_block, send

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    # survey marker per deck (shell serial): a survey of one deck (or of
    # the mock deck) never stands in for another's
    serial = str(getattr(h, "serial", "") or "unknown").replace("/", "_")
    ok_file = os.path.expanduser(f"~/.xdj700_tf_survey_ok.{base:08x}.{serial}")
    if not arg and not os.path.exists(ok_file):
        h.close()
        sys.exit(f"REFUSED: run --survey first for {base:#x} on this deck")
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        pools = [rd32(h, p) for p in TF.POOLS]
        for a, hx in STOCK.items():
            got = bytes(br(h, a, 8)).hex()
            if got != hx:
                sys.exit(f"REFUSED: firmware at {a:#x} is {got}, not fw 1.15's {hx}")
        fp, tp = rd32(h, TF.FLUSH_POOL), rd32(h, TF.TOUCH_POOL)
        print(f"list-copy pools {[hex(p) for p in pools]} (stock {TF.STOCK_COPY:#x}, "
              f"ours {s['tf_copy']:#x})")
        if arg == "--survey":
            blk = read_block(h, base, end - base)
            nz = [i for i, b in enumerate(blk) if b]
            if nz and bytes(blk[:4]) != struct.pack("<I", TF.MAGIC):
                sys.exit(f"SURVEY FAIL: {len(nz)} nonzero bytes, first at "
                         f"{base + nz[0]:#x} - pick another TF_BASE")
            open(ok_file, "w").write("zero\n")
            print(f"SURVEY OK: {base:#x}..{end:#x} " +
                  ("all zero" if not nz else "holds this module already"))
            return 0
        if arg == "--revert":
            # the text hook first, then the screen (back to whatever each was
            # chained in front of)
            for pool in TF.TEXT_POOLS:
                if rd32(h, pool) == s["tfs_txt_entry"]:
                    pv = rd32(h, s["tfs_prev_txt"])
                    lw(h, pool, pv)
                    if rd32(h, pool) != pv:
                        sys.exit(f"FAIL: pool {pool:#x} not restored - REBOOT the deck")
            for pool, ent, prev in ((TF.FLUSH_POOL, "tfs_flush_entry", "tfs_prev_flush"),
                                    (TF.TOUCH_POOL, "tfs_touch_entry", "tfs_prev_touch")):
                if rd32(h, pool) == s[ent]:
                    pv = rd32(h, s[prev])
                    lw(h, pool, pv)
                    if rd32(h, pool) != pv:
                        sys.exit(f"FAIL: pool {pool:#x} not restored - REBOOT the deck")
            if all(p == TF.STOCK_COPY for p in pools):
                print("not armed - nothing to do")
                return 0
            for p in TF.POOLS:
                lw(h, p, TF.STOCK_COPY)
                if rd32(h, p) != TF.STOCK_COPY:
                    sys.exit(f"FAIL: pool {p:#x} not restored - REBOOT the deck")
            print("RESULT: reverted")
            return 0
        if any(p not in (TF.STOCK_COPY, s["tf_copy"]) for p in pools):
            sys.exit("FAIL: list-copy pools not stock - nothing written")
        if all(p == s["tf_copy"] for p in pools) and fp == s["tfs_flush_entry"] \
                and tp == s["tfs_touch_entry"] \
                and all(rd32(h, p) == s["tfs_txt_entry"] for p in TF.TEXT_POOLS):
            print("RESULT: PASS (already armed)")
            return 0
        cur = read_block(h, base, len(want))
        diff = [i for i, (a, b) in enumerate(zip(cur, want)) if a != b]
        if diff:
            # our own settings/counters (bss, after the image) may be live
            # already (re-run after a partial arm): never treat them as foreign
            mine = set(range(s["tfs_prev_flush"] - base, s["tfs_prev_flush"] - base + 4)) | \
                set(range(s["tfs_prev_touch"] - base, s["tfs_prev_touch"] - base + 4)) | \
                set(range(s["tfs_prev_txt"] - base, s["tfs_prev_txt"] - base + 4))
            bad = [i for i in diff if cur[i] and i < len(img) and i not in mine]
            if bad:
                sys.exit(f"FAIL: module area +{bad[0]:#x} holds foreign data - nothing armed")
            import fastload
            left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                           allow_live=range(base + len(img), end),
                                           cur=cur)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
            if fastload.arm_later():
                print("code complete - run again to arm (fresh short session)")
                return 0
        if rd32(h, s["tf_prev"]) != TF.STOCK_COPY or rd32(h, base) != TF.MAGIC:
            sys.exit("FAIL: module header/tf_prev did not verify - nothing armed")
        scratch = s["tf_calls"]                      # our own counter word
        for val in (0x12345678, 0):
            lw(h, scratch, val)
            if rd32(h, scratch) != val:
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        for p in TF.POOLS:
            if rd32(h, p) == s["tf_copy"]:
                continue
            lw(h, p, s["tf_copy"])
            if rd32(h, p) != s["tf_copy"]:
                for q in TF.POOLS:
                    lw(h, q, TF.STOCK_COPY)
                sys.exit(f"FAIL: pool {p:#x} verify - restored; REBOOT before playing")
        # the screen + header button: chained in front of whatever owns the
        # display-flush and hit-tester pools now (pads, hot cues, TOUCH FX...)
        for pool, ent, prev in ((TF.FLUSH_POOL, "tfs_flush_entry", "tfs_prev_flush"),
                                (TF.TOUCH_POOL, "tfs_touch_entry", "tfs_prev_touch")):
            live = rd32(h, pool)
            if live == s[ent]:
                continue
            if not 0x08000000 <= live < 0x10000000:
                sys.exit(f"FAIL: pool {pool:#x} holds {live:#x} - screen not armed")
            lw(h, s[prev], live)
            if rd32(h, s[prev]) != live:
                sys.exit("FAIL: chain word did not verify - screen not armed")
            lw(h, pool, s[ent])
            if rd32(h, pool) != s[ent]:
                lw(h, pool, live)
                sys.exit(f"FAIL: pool {pool:#x} verify - restored")
        # the browse's text blender (both pools must agree: stock or the
        # light-mode wrapper): pixels inside the open screen are dropped
        live = {rd32(h, p) for p in TF.TEXT_POOLS} - {s["tfs_txt_entry"]}
        if len(live) > 1:
            sys.exit(f"FAIL: text pools disagree {[hex(x) for x in live]} - text hook not armed")
        if live:
            pv = live.pop()
            if not 0x08000000 <= pv < 0x10000000:
                sys.exit(f"FAIL: text pool holds {pv:#x} - text hook not armed")
            lw(h, s["tfs_prev_txt"], pv)
            if rd32(h, s["tfs_prev_txt"]) != pv:
                sys.exit("FAIL: text chain word did not verify - text hook not armed")
        for p in TF.TEXT_POOLS:
            if rd32(h, p) == s["tfs_txt_entry"]:
                continue
            old = rd32(h, p)
            lw(h, p, s["tfs_txt_entry"])
            if rd32(h, p) != s["tfs_txt_entry"]:
                lw(h, p, old)
                sys.exit(f"FAIL: pool {p:#x} verify - restored")
        print("RESULT: PASS - in a track list, tap the funnel at the right of the "
              "list header = filter on/off, hold it = the TRACK FILTER screen "
              "(also the UTILITY rows)")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
