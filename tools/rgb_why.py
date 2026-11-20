#!/usr/bin/env python3
"""rgb_why.py -- READ-ONLY: why is the colour waveform not showing?

Report (2026-11-18): "RGB waveform did work in a version before 1.0, but now
installing it seems to do nothing." This reads the handful of bytes that
decide that, in ONE shell session, and names the cause. It writes nothing,
so it is safe on a deck in any state.

Use it with a rekordbox-analysed track loaded and the main (TRACK) screen
up, after the normal install:

    .venv/bin/python tools/rgb_why.py

What it checks, in the order the deck applies them:
  1. UTILITY > MODS        (MSOFF = 1 switches every colour mod off)
  2. UTILITY > WAVEFORM    (the mode byte: 0 = BLUE/THEME, 1 = RGB,
                            2 = 3-BAND -- 0 draws the deck's blue)
  3. the overview hook     (pool word still stock = not installed)
  4. the centre module     (palette pool still stock = not installed)
  5. the same-track gate   (1.1: the colours are only drawn when the ANLZ
                            cache really holds THIS track's PWAV -- it
                            compares the cache's PWAV with the overview the
                            painter is drawing, 360 of 400 columns required)
"""
import struct
import sys

sys.path[:0] = [".", "tools"]
import hid                                              # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain   # noqa: E402
from rgb_overview_apply import br, read_block, send    # noqa: E402
import rgb_overview as RO                              # noqa: E402
import rgb_detail as RD                                # noqa: E402
import mysettings as MS                                # noqa: E402
import theme_switch as TS                              # noqa: E402


def rd32(h, a):
    return struct.unpack("<I", bytes(br(h, a, 4)))[0]


def tag(v):
    if v in (0, 0xFFFFFFFF):
        return "empty"
    b = struct.pack("<I", v)
    return b.decode("ascii", "replace") if all(32 <= c < 127 for c in b) else f"{v:#010x}"


def agree(cache, prev):
    """The same-track gate's comparison, in Python (rgb_overview.SAME)."""
    n = 0
    for i in range(min(400, len(cache), len(prev) // 2)):
        b = cache[i]
        hgt, wh = b & 31, b >> 5
        dh, dw = prev[2 * i], prev[2 * i + 1]
        if dw == wh and -1 <= dh - hgt <= 1:
            n += 1
    return n


def main():
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s) - plug the deck in "
                 f"(USB-B on the back), switch it on and quit rekordbox / DJ "
                 f"apps, then run this again")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        mode, tflag, p5 = br(h, RO.MODE_ADDR, 3)
        msoff = br(h, MS.MSOFF, 1)[0]
        msave = br(h, MS.MSSAVE, 2)
        hook = rd32(h, RO.HOOK_POOL)
        cpool = rd32(h, RD.POOL)
        cinsn = struct.unpack("<H", bytes(br(h, RD.INSN, 2)))[0]
        crow = struct.unpack("<H", bytes(br(h, RD.ROWINC[0], 2)))[0]
        snap = rd32(h, RO.SNAP_HASH)
        s4, s6, s5 = (rd32(h, RO.SLOT4), rd32(h, RO.SLOT6), rd32(h, RO.SLOT5))
        ctx = rd32(h, RO.CTX_TAB)
        ptag = "no context"
        pwav = b""
        if 0x08000000 <= ctx < 0x10000000 and ctx % 4 == 0:
            ptag = tag(rd32(h, ctx + RO.PWAV_OFF))
            if ptag == "PWAV":
                pwav = read_block(h, ctx + RO.PWAV_OFF + 20, 400)
        prev = read_block(h, RO.PREVIEW, 800)   # 400 (height, whiteness) pairs
        n = agree(pwav, prev) if pwav else -1
        # 2026-11-18: 1.1's theme switcher recolours these palettes; a LINK
        # track has no colour data, so the stock painter draws its fallback
        # from them (the "forest green" waveform). One word is enough.
        live = stock_w = theme_w = None
        bl = read_block(h, TS.TS_DATA, 72)
        if bl[:4] == b"XTSW":
            ns_, nt_ = struct.unpack_from("<II", bl, 8)
            cur_ = bl[32]
            w_off = struct.unpack_from("<I", bl, 68)[0]
            live = struct.unpack("<H", read_block(h, TS.WAVE_PAL[0][0], 2))[0]
            if w_off and ns_ and nt_ and cur_ <= nt_:
                p_ = read_block(h, TS.TS_DATA + w_off, 8)
                n_, m_ = struct.unpack("<II", p_)
                if 0 < n_ <= 64 and m_ >= n_ + 3:
                    a8 = read_block(h, TS.TS_DATA + w_off + 8, 4 * n_)
                    addr = [struct.unpack_from("<I", a8, 4 * k)[0] & ~1
                            for k in range(n_)]
                    pal0 = TS.WAVE_PAL[0][0]
                    if pal0 in addr:
                        i_ = addr.index(pal0)
                        r0 = TS.TS_DATA + w_off + 8 + 4 * n_ + 2 * i_
                        stock_w = struct.unpack("<H", read_block(h, r0, 2))[0]
                        if cur_:
                            rc = r0 + 2 * m_ * cur_
                            theme_w = struct.unpack("<H", read_block(h, rc, 2))[0]

        names = {0: "BLUE/THEME", 1: "RGB", 2: "3-BAND"}
        print(f"UTILITY > MODS          : {'OFF' if msoff else 'ON'} "
              f"(saved wave {msave[0]}, saved version {msave[1]})")
        print(f"UTILITY > WAVEFORM      : {names.get(mode, f'{mode:#x}?!')} "
              f"(mode byte {mode}, theme-flag {tflag}, p5-flag {p5})")
        print(f"overview hook           : {hook:#010x} "
              f"({'STOCK - not installed' if hook == RO.ORIG_RELEASE else 'our cave' if hook == RO.CAVE else 'old cave' if hook in RO.OLD_CAVES else 'UNEXPECTED'})")
        print(f"centre module           : palette pool {cpool:#010x} "
              f"({'STOCK - not installed' if cpool == RD.PALETTE else 'our shim' if cpool == RD.DCAVE else 'UNEXPECTED'}), "
              f"hook word {cinsn:#06x} "
              f"({'patched' if cinsn == RD.INSN_NEW else 'stock' if cinsn == RD.INSN_OLD else 'UNEXPECTED'}), "
              f"row load {crow:#06x} "
              f"({'patched' if crow == RD.ROWINC[2] else 'stock' if crow == RD.ROWINC[1] else 'UNEXPECTED'})")
        print(f"same-track gate         : cache {ptag}, agrees "
              f"{'--' if n < 0 else f'{n}/400'} (needs {RO.SAME_MIN}), "
              f"hash {snap:#010x}, slots PWV4 {tag(s4)} / PWV6 {tag(s6)} / "
              f"PWV5 {tag(s5)}")
        if live is not None and bl[:4] == b"XTSW":
            print(f"theme / blue palette    : theme {cur_ or 'OFF (stock)'}, "
                  f"overview blue word {live:#06x} (stock row "
                  f"{'?' if stock_w is None else f'{stock_w:#06x}'}" +
                  ("" if theme_w is None else f", theme row {theme_w:#06x}") + ")")

        print("\nVERDICT")
        if msoff:
            print("  MODS is OFF in UTILITY (hold MENU > MODS > ON). While it "
                  "is off the deck draws stock colours, whatever WAVEFORM "
                  "says. Switch it back on - nothing needs reinstalling.")
        elif hook == RO.ORIG_RELEASE:
            print("  The waveform mod is not armed in memory (the overview "
                  "hook still points at the stock release). Install again, or "
                  "power-cycle and reinstall: the mods live in memory only.")
        elif mode == 0:
            print("  WAVEFORM COLOR is BLUE/THEME, so the deck is drawing its "
                  "own blue. UTILITY > WAVEFORM COLOR > RGB (one push).")
        elif n >= 0 and n < RO.SAME_MIN:
            print("  The deck's ANLZ cache does not hold THIS track's PWAV, so "
                  "the 1.1 same-track check keeps the colours off on purpose "
                  "(it exists so a track never shows another track's "
                  "colours). The cache is filled when a track is loaded from "
                  "the deck's OWN stick: re-insert the stick and load the "
                  f"track again. Cache tag '{ptag}', {n}/400 columns agree.\n"
                  "  A LINK track lands here by design: before 1.1 nothing was "
                  "checked, so it drew ANOTHER track's colours (it looked "
                  "like RGB worked); 1.1 draws blue instead.")
        elif n < 0:
            print("  There is no ANLZ context, so no colour data is reachable "
                  "(a LINK track, or the track was not loaded from this "
                  "deck's stick). Colours cannot be drawn; the deck shows "
                  "blue by design.")
        else:
            print("  Mode and data are fine, so the painter is the suspect: "
                  "send this output back (it says whether the snapshot slots "
                  "are filled above).")
        print("\n(Read-only run; nothing was written to the deck.)")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
