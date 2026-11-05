#!/usr/bin/env python3
"""theme_switch_apply.py --survey | (install) [--themes a b c] -- on-deck
theme SWITCHING (MY SETTINGS [THEME] cycles OFF -> themes -> OFF, live).
Emulator PASS: tools/theme_switch.py emu. RAM only, reboot = stock.
NOT YET RUN ON HARDWARE.

  --survey   READ-ONLY: TS_BASE .. end of data must read zero on THIS
             deck (code, 20 KB work area, theme tables); writes
             build/themesw/survey_ok.<base> on success
  (no flag)  chunked write of code (minus the entry word) + theme tables
             (resumable, foreign-data check), verify; then a fresh run arms:
             row text "OFF", LW self-test, LW entry table word (atomic).
Needs the MY SETTINGS rows installed with the [THEME] -> ts_push code
(tools/mysettings_apply.py from 2026-10-11 on); until the entry word is
set the [THEME] push does nothing at all.
Safety in the deck code itself: every 8 KB of artwork is recoloured only
if it still matches its stock fingerprint (firmware data -> skipped).
"""
import argparse
import os
import struct
import sys

sys.path[:0] = [".", "tools"]
import theme_switch as TS
import mysettings as MS

OUT = "build/themesw"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--themes", nargs="*", default=None)
    ap.add_argument("--precheck", action="store_true",
                    help="skip the separate survey: the write step reads the "
                         "whole area (code + work area + tables) first anyway")
    a = ap.parse_args()
    names = a.themes or TS.default_names()
    try:
        (cbase, code), (dbase, data) = TS.plan(names)
    except RuntimeError as e:
        print(f"REFUSED: {e}")
        print("RESULT: FAIL")
        return 1
    end = dbase + len(data)
    ok_file = os.path.expanduser(f"~/.xdj700_ts_survey_ok.{cbase:08x}.{end:08x}")  # never in the app
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, bw, read_block, send, CHUNK_B

    def rd32(h, x):
        return struct.unpack("<I", bytes(br(h, x, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    if not a.survey and not a.precheck and not os.path.exists(ok_file):
        sys.exit(f"REFUSED: run --survey first ({cbase:#x}..{end:#x})")
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        if a.survey:
            blk = read_block(h, cbase, end - cbase)
            nz = [i for i, b in enumerate(blk) if b]
            if nz:
                sys.exit(f"SURVEY FAIL: {len(nz)} nonzero bytes, first at "
                         f"{cbase + nz[0]:#x} - nothing written")
            os.makedirs(OUT, exist_ok=True)
            open(ok_file, "w").write("zero\n")
            print(f"SURVEY OK: {cbase:#x}..{end:#x} all zero")
            return 0
        entry = struct.unpack("<I", code[:4])[0]
        live = rd32(h, cbase)
        if live == entry:
            print("RESULT: PASS (already armed)")
            return 0
        if live:
            sys.exit(f"FAIL: entry word holds {live:#x} - nothing written")
        # everything except the entry word (that is the on-switch); the
        # work area between code and tables must stay zero (checked too)
        import fastload
        want = bytearray(end - cbase)
        want[4:len(code)] = code[4:]
        want[dbase - cbase:] = data
        left = fastload.install_region(h, cbase + 4, bytes(want[4:]),
                                       fastload.SESSION_BUDGET)
        if left:
            print(f"wrote this session; {left} left")
            print("RESULT: PARTIAL - run again to continue")
            return 0
        if fastload.arm_later():          # long session: arm in a fresh one
            print("tables complete - run again to arm (fresh short session)")
            return 0
        # arm: row text, LW self-test on our own counter, entry word
        if not bytes(br(h, MS.MSTNAME, 1))[0]:
            for i, x in enumerate(b"OFF\0"):
                bw(h, MS.MSTNAME + i, x)
        scratch = dbase + 36                       # nswitch counter
        for val in (0x12345678, 0):
            lw(h, scratch, val)
            if rd32(h, scratch) != val:
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        lw(h, cbase, entry)
        if rd32(h, cbase) != entry:
            sys.exit("FAIL: entry word not set - REBOOT the deck")
        print(f"RESULT: PASS - {len(names)} themes: BROWSE > MENU > MY "
              f"SETTINGS > [THEME], push to step through them")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
