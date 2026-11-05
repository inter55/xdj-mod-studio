#!/usr/bin/env python3
"""stems_apply.py [--revert] -- live STEM mute/solo on the DECK
(tools/stems/sm.c; emulator PASS: tools/stems_emu_test.py masks 0/5/14).
RAM only, reboot = stock. NOT YET RUN ON HARDWARE.

Needs the PERFORM pads (their STEMS tab owns the mute mask) and works on
STEMS-4 files made by Mod Studio / tools/stems4.py. Chains on the media
server's audio read (pool 0x08D5FFA8) in front of whatever is there (the
version switcher or the stock read), so key/stem versions keep working.

  (no flag)  read the whole area first (foreign-data check), write code +
             zeroed work area (32-bit bursts, each read back), then arm:
             sm_prev = live hook, one atomic LW of the pool word, verify.
  --revert   pool back to the hook it chained.
"""
import os
import struct
import subprocess
import sys

sys.path[:0] = [".", "tools"]
HOOK = 0x08D5FFA8
POLL = 0x08C1D56C            # player task's buffer-level poll (fast mute trigger)
POLL_ACC = 0x08BF2BCC        # its stock value: the host-port read accessor
DIRECT = 0x08DDFFD4          # the media API's direct USB read slot (also fread)
FREAD = 0x08C61A6C
OUT = "build/stems"


def build():
    import perform_pads as PP
    env = dict(os.environ, PMASK=hex(PP.PMASK))
    import shbuild
    shbuild.build("tools/stems/build.sh", [f"{OUT}/sm.bin", f"{OUT}/sm.syms"], env)
    s = {}
    for line in open(f"{OUT}/sm.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return open(f"{OUT}/sm.bin", "rb").read(), s


def entry():
    """address the stems engine puts in the read slots (tools/flac_apply.py
    chains to it)"""
    if not os.path.exists(f"{OUT}/sm.syms"):
        build()
    for line in open(f"{OUT}/sm.syms"):
        a, _, n = line.split()
        if n.lstrip("_") == "sm_fread":
            return int(a, 16)
    raise KeyError("sm_fread")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    img, s = build()
    base, end, entry = s["sm_table"], s["end"], s["sm_fread"]
    import hid
    import fastload
    import perform_pads as PP
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, send

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if len(c) != 1:
        sys.exit(f"found {len(c)} shell interface(s)")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        live = rd32(h, HOOK)
        print(f"audio read hook = {live:#x} (ours {entry:#x})")
        poll = s["sm_poll_entry"]

        def arm_poll():
            """fast trigger: the poll pool -> sm_poll (only from stock)"""
            pv = rd32(h, POLL)
            if pv == poll:
                return True
            if pv != POLL_ACC:
                print(f"poll pool {pv:#x} not stock - fast trigger left off "
                      f"(mutes still work, at read-burst speed)")
                return False
            lw(h, POLL, poll)
            if rd32(h, POLL) != poll:
                lw(h, POLL, POLL_ACC)
                print("poll pool verify failed - restored, fast trigger off")
                return False
            return True

        if arg == "--revert":
            if rd32(h, POLL) == poll:
                lw(h, POLL, POLL_ACC)
                if rd32(h, POLL) != POLL_ACC:
                    sys.exit("FAIL: poll pool not restored - REBOOT the deck")
            if live != entry:
                print("not armed - nothing to do")
                return 0
            prev = rd32(h, s["sm_prev"])
            if rd32(h, DIRECT) == entry:
                lw(h, DIRECT, FREAD)
            lw(h, HOOK, prev)
            if rd32(h, HOOK) != prev:
                sys.exit("FAIL: hook not restored - REBOOT the deck")
            print(f"RESULT: reverted (hook = {prev:#x})")
            return 0
        if live == entry:
            arm_poll()
            print("RESULT: PASS (already armed)")
            return 0
        if not (0x08000000 <= live < 0x10000000):
            sys.exit(f"FAIL: unexpected audio read hook {live:#x} - nothing written")
        if rd32(h, PP.PC0) == 0:
            sys.exit("REFUSED: PERFORM pads not installed (they hold the STEMS mask)")
        want = bytearray(img) + bytes(max(0, end - base - len(img)))
        po = s["sm_prev"] - base
        want[po:po + 4] = struct.pack("<I", live)
        left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                       allow_live=range(s["sm_prev"], s["sm_prev"] + 4))
        if left:
            print(f"RESULT: PARTIAL ({left} B left) - run again")
            return 0
        if fastload.arm_later():
            print("code complete - run again to arm (fresh short session)")
            return 0
        lw(h, HOOK, entry)
        if rd32(h, HOOK) != entry:
            lw(h, HOOK, live)
            sys.exit("FAIL: hook verify - restored; REBOOT before playing")
        # 2026-10-12: on hardware the media server hook alone saw no reads;
        # route the direct read slot too (only when it is still stock --
        # the FLAC layer owns it otherwise and chains to fread itself)
        if rd32(h, DIRECT) == FREAD:
            lw(h, DIRECT, entry)
            if rd32(h, DIRECT) != entry:
                lw(h, DIRECT, FREAD)
                sys.exit("FAIL: direct slot verify - restored")
        fast = arm_poll()
        print("RESULT: PASS - load a STEMS file; PERFORM > STEMS mutes/solos live"
              + (" (fast trigger on)" if fast else ""))
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
