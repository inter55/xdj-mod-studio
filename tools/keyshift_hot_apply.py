#!/usr/bin/env python3
"""keyshift_hot_apply.py -- REAL-TIME KEY SHIFT on the DECK, NO chip reset.
RAM only; a power-cycle always returns the deck to stock.

Replaces the reload route (keyshift_dsp_apply.py), which fails on hardware
(CLAUDE.md §111-118). This one never resets the audio chip: a one-shot cave
(build/keyshift/ksh.bin @0x09800000, tools/keyshift_dsp/ksh.c) writes the
key-shift code into the RUNNING chip's memory through the same host port
the deck uses for its mailboxes, read-checks it, and only then flips one
jump. Emulator: tools/keyshift_hot_emu.py PASS + keyshift_dsp_emu.py pitch.

Steps (one at a time, each its own short USB session, stick OUT / no track):
  keyshift_hot_apply.py --probe     READ-ONLY on the audio chip: proves the
                                    host port can see its program memory.
                                    Expect "status 6". Nothing changes.
  keyshift_hot_apply.py             install (status 1) + take over the
                                    key word (one 2-byte store NOP)
  keyshift_hot_apply.py --semis 2   with a track PLAYING and MASTER TEMPO ON
  keyshift_hot_apply.py --semis 0   no shift
  keyshift_hot_apply.py --revert    stock jump back + stock store

TOUCH FX engine (2026-11-01): --engine fx installs the TOUCH FX DSP code
(tools/fx_dsp/fx.c, cave build/fx/ksh.bin -- same installer, other payload)
instead of key shift. They share the DSP hook and home: only one at a time;
--revert the installed one first (the install refuses otherwise, status 7).
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, "tools")
OUT = "build/keyshift"
HOOK = 0x08C92E20
TRACK_LEN = 0x0BA3C050
NOP_SITE, NOP_STOCK, NOP = 0x08D36B3A, 0x1C15, 0x0009
KEY_WORD = 0x09D24DD8
OFF = 0x00100000
FLAG = {"probe": 0x4B534850, "install": 0x4B534831, "revert": 0x4B534830}
WANT = {"probe": 6, "install": 1, "revert": 5}
MEANING = {1: "installed", 2: "a track is loaded", 3: "deck words not stock",
           5: "reverted", 6: "probe OK - the audio chip's memory is reachable",
           7: "audio chip memory did not match the stock program (NOTHING written)",
           8: "host-port DMA busy (still retrying)",
           9: "code did not read back (jump NOT switched)",
           10: "jump did not read back (stock jump restored)"}


def syms():
    s = {}
    for line in open(f"{OUT}/ksh.syms"):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def key_word(semis):
    if semis == 0:
        return OFF
    return 0x40000000 | int(round(2.0 ** (semis / 12.0) * (1 << 20)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--semis", type=float)
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--engine", choices=("keyshift", "fx"), default="keyshift",
                    help="which DSP code: real-time key shift (default) or TOUCH FX")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite an OLDER build of this cave (only while it "
                         "is idle: the screen hook is not pointing at it)")
    a = ap.parse_args()
    if a.semis is not None and not -6 <= a.semis <= 6:
        sys.exit("semis range is -6..+6")
    what = "probe" if a.probe else "revert" if a.revert else "install"
    global OUT
    if a.engine == "fx":
        OUT = "build/fx"
        if a.semis is not None:
            sys.exit("--semis is for key shift; TOUCH FX is driven from the PERFORM screen")
        MEANING[7] = (MEANING[7] + " -- or key shift is installed: run with "
                      "--revert (no --engine) first")
    s = syms()
    base = s["ksh_entry"]
    img = open(f"{OUT}/ksh.bin", "rb").read()

    import hid
    import fastload
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, read_block, send
    from shellbyte import cmd

    def rd32(h, addr):
        return struct.unpack("<I", bytes(br(h, addr, 4)))[0]

    def rd16(h, addr):
        vals = br(h, addr, 2)            # 1-value BR answers E06 on the deck
        return vals[0] | (vals[1] << 8)

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)
        return rd32(h, addr) == val

    def ww(h, addr, v):
        cmd(h, f"{addr:x},{v:x}WW\r\x00".encode(), f"WW {addr:#x}", 2.0)
        return rd16(h, addr) == v

    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        site = rd16(h, NOP_SITE)
        if site not in (NOP_STOCK, NOP):
            sys.exit(f"FAIL: key-word store {site:04x} is not stock - nothing written")

        if a.semis is not None:
            if site != NOP or rd32(h, s["ksh_status"]) != 1:
                sys.exit("REFUSED: not installed on this boot - run without --semis first")
            if rd32(h, TRACK_LEN) == 0:
                sys.exit("REFUSED: load and play a track first")
            w = key_word(a.semis)
            if not lw(h, KEY_WORD, w):
                sys.exit("FAIL: key word did not verify - try again while playing")
            print(f"RESULT: PASS - key {a.semis:+g} semitones (word {w:#010x}); "
                  f"needs MASTER TEMPO ON")
            return 0

        if rd32(h, TRACK_LEN) != 0:
            sys.exit("REFUSED: a track is loaded - eject the USB stick first")
        live = rd32(h, HOOK)
        if live == base:
            sys.exit("FAIL: cave already hooked (an earlier run did not finish) - "
                     "wait a second and run again; if it repeats, power-cycle")

        # cave bytes (prev = the live flush pool; our runtime words zero)
        want = bytearray(img)
        po = s["ksh_prev"] - base
        want[po:po + 4] = struct.pack("<I", live)
        own = []
        for k in ("ksh_flag", "ksh_status", "ksh_tries"):
            o = s[k] - base
            want[o:o + 4] = b"\0\0\0\0"
            own += range(o, o + 4)
        o = s["ksh_dbg"] - base
        own += range(o, o + 64)
        own += range(po, po + 4)
        cur = read_block(h, base, len(want))
        bad = [i for i, (c, w) in enumerate(zip(cur, want))
               if c and c != w and i not in own]
        if bad and not a.replace:
            sys.exit(f"FAIL: cave +{bad[0]:#x} holds foreign data - nothing written "
                     f"(an older build of ours? re-run with --replace)")
        if any(cur[i] != want[i] for i in range(len(want)) if i not in own):
            live_ok = range(base, base + len(want)) if a.replace \
                else range(base + po, base + po + 4)
            left = fastload.install_region(h, base, bytes(want), fastload.SESSION_BUDGET,
                                           allow_live=live_ok, cur=cur)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
        for k in ("ksh_status", "ksh_tries"):
            if not lw(h, s[k], 0):
                sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        if not lw(h, s["ksh_prev"], live):
            sys.exit("FAIL: prev word - nothing armed")
        if not lw(h, s["ksh_flag"], FLAG[what]):
            sys.exit("FAIL: flag - nothing armed")
        # arm: the cave runs on the next screen frame (~16 ms) and unhooks
        # itself, so a hook read-back loses that race -- judge by status
        q = f"{HOOK:x},{base:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.3)
        t = time.time()
        status = 0
        while time.time() - t < 15:
            status = rd32(h, s["ksh_status"])
            if status and status != 8:
                break
            time.sleep(0.3)
        raw = read_block(h, s["ksh_dbg"], 64)
        dbg = [raw[k] | raw[k + 1] << 8 | raw[k + 2] << 16 | raw[k + 3] << 24
               for k in range(0, 64, 4)]
        print("  dbg", " ".join(f"{x:08x}" for x in dbg))
        hook = rd32(h, HOOK)
        print(f"  status {status} = {MEANING.get(status, '?')}; hook back to "
              f"{'stock chain' if hook == live else hex(hook)}")
        if hook == base:                       # never ran / still retrying
            lw(h, s["ksh_flag"], 0)
            lw(h, HOOK, live)
            sys.exit("FAIL: cave did not finish - unhooked again, nothing changed")
        if status == 0:
            lw(h, s["ksh_flag"], 0)
            sys.exit("FAIL: arming write did not land (status 0, hook stock) - "
                     "nothing ran; run again")
        if status != WANT[what]:
            sys.exit(f"FAIL: status {status}")
        if what == "install" and site != NOP:
            if not ww(h, NOP_SITE, NOP):
                sys.exit("FAIL: key-word store NOP did not verify - run --revert")
        if what == "revert" and site == NOP:
            lw(h, KEY_WORD, OFF)
            if not ww(h, NOP_SITE, NOP_STOCK):
                sys.exit("FAIL: store restore - power-cycle the deck")
        print({"probe": "RESULT: PASS - probe OK, nothing changed. Next: run "
                        "without --probe to install.",
               "install": ("RESULT: PASS - TOUCH FX engine installed (no reset, sound "
                           "never stopped). MASTER TEMPO ON, then PERFORM > TOUCH FX"
                           if a.engine == "fx" else
                           "RESULT: PASS - key shift installed (no reset, sound "
                           "never stopped). Insert the stick, play a track with "
                           "MASTER TEMPO ON, then --semis 2"),
               "revert": "RESULT: PASS - stock jump + stock store back"}[what])
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
