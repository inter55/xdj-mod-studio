#!/usr/bin/env python3
"""ld_probe.py -- READ-ONLY: what the USB-stick loader did on the deck.

The stick route needs the LOADER in flash (the "loader update": stock
firmware + reader only). At every stick mount the deck's own MYSETTING.DAT
reader opens files through pool 0x08DE39A4, which points at the loader; the
loader reads <stick>/XDJMODS/PACK.BIN and replays it. When mods do not come
up, this says why, in one cable run and without writing anything:

  status IDLE    the loader never ran -> the file you flashed did not carry
                 it (the MODDED update file does not), or nothing mounted
  status NOFILE  it ran, no XDJMODS/PACK.BIN found at the stick root
  status BADDEV  the mount had no readable device behind it
  status HEADER  it read the file and REFUSED it (why/bad say where): most
                 often W2_OLD/W2_ZERO -- the pack was recorded against bytes
                 that are not in this deck's RAM (e.g. it was made for a
                 stock deck and this one already has mods baked in)
  status DONE    every entry applied (applied == the pack's entry count)

Usage: .venv/bin/python tools/ld_probe.py        (deck on USB, powered on)
"""
import os
import struct
import sys
sys.path[:0] = [".", "tools"]

SYMS = "build/usbloader/ld.syms"
ST = ["IDLE", "NOFILE", "BADDEV", "HEADER", "DONE"]
W = ["W_OK", "W_SIZE", "W_BAND", "W_LIVE", "W_SHORT", "W_CRC",
     "W2_WINDOW", "W2_ZERO", "W2_OLD", "W2_COUNT"]
POOL = 0x08DE39A4                    # mount-open pool the loader is armed on
MAGIC = 0x5453444C                   # 'LDST'
LDR2 = 0x3252444C                    # 'LDR2', ld.bin's first word


def main():
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, send
    s = {}
    for ln in open(SYMS):
        a, _, n = ln.split()
        s[n.lstrip("_")] = int(a, 16)

    def r32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    c = [d for d in hid.enumerate()
         if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if not c:
        print("no deck on USB (nothing was read, nothing was written)")
        return 1
    h = open_deck(PIONEER_VID, c[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        # the loader's fixed header (ldentry.S): +0 "LDR2", +8 &ld_state.
        # Read the state address FROM THE DECK: ld_state moves between
        # builds, and the deck runs whatever loader was flashed, not the
        # one this Mac last built (the local .syms is only a fallback).
        base = s["ld_head"]
        st = s["ld_state"]
        if r32(h, base) == LDR2:
            st = r32(h, base + 8)
        (magic, calls, status, tries,
         applied, bad, why, nbytes, done) = [r32(h, st + 4 * i) for i in range(9)]
        if magic != MAGIC:
            print(f"ld_state at {st:#x} does not carry our magic "
                  f"({magic:#x}): the loader never ran on this power-up.\n"
                  "  -> the file you flashed did not include it. Build "
                  "'loader update' (stock + reader) or send the loader over "
                  "the cable, then mount the stick again.")
            return 1
        print(f"loader state: done={done} calls={calls} tries={tries}")
        print(f"  last status  {status} ({ST[status] if status < len(ST) else '?'})")
        print(f"  entries applied {applied}, rejected entry {bad}, "
              f"reason {why} ({W[why] if why < len(W) else '?'}), "
              f"pack bytes consumed {nbytes}")
        head = r32(h, base)
        print(f"  loader at {s['ld_head']:#x} holds {head:#010x} "
              f"({'LDR2, present' if head == LDR2 else 'NOT the loader image'})")
        # the pool holds the loader's ENTRY (header +4 = ld_open), not its base
        entry = r32(h, base + 4) if head == LDR2 else s["ld_open"]
        pool = r32(h, POOL)
        print(f"  mount-open pool {POOL:#x} -> {pool:#x} "
              f"({'armed' if pool == entry else f'NOT armed: the loader entry is {entry:#x}'})")
        if status == 4 and why:
            print(f"  the replay STOPPED at entry {bad} ({W[why] if why < len(W) else why}): "
                  "the mods file on the stick is damaged -- write it again")
        elif status == 4 and applied == 0:
            print("  status DONE but nothing applied: the pack had no entries")
        if status == 3 and why == 0:
            print("  the file was not read as a mods file (wrong name/format, "
                  "an older app's file, or the read failed) -- write it again")
        elif status == 3:
            print("  the pack was refused as a whole (two-phase check): "
                  "it was made for different RAM than this deck has (a deck "
                  "with mods already baked in, or mods sent over the cable "
                  "this power-on) -- power-cycle, then insert the stick")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
