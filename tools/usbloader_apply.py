#!/usr/bin/env python3
"""usbloader_apply.py -- the USB-stick mods loader (stage 1) on the DECK,
RAM only: a power cycle is stock again. NOT YET RUN ON HARDWARE.

  --survey        READ-ONLY: the loader's band UL_BASE..+64 KB must read zero
                  and the hook pool word must read stock (nothing written)
  (no flag)       write build/usbloader/ul.bin (fastload, read back), then
                  ARM: one atomic LW of the MYSETTING.DAT mount reader's
                  open pool word -> ul_open. From then on every stick mount
                  runs the loader once per power-up: it reads
                  <stick>/PIONEER/MODS/MODS.BIN (stage 2) which streams
                  PACK.BIN into RAM (tools/usbpack.py; the Studio's
                  "Write to USB stick")
  --status        READ-ONLY: the loader's state block (calls, status, loads
                  ..) and, after a payload ran, the stage-2 state (records,
                  bytes, arms, error)
  --check-marker  READ-ONLY: after a mount with the marker pack
                  (usbpack.py --marker): the pattern bytes must read back
                  exactly (the copy-back cache test the emulator cannot do)
  --rearm         clear the loader's one-shot flag: the next stick mount
                  installs again (a pack whose arms are already live stops
                  at ARMSTOCK with its data rewritten -- harmless)
  --revert        hook pool word back to stock (the loader stays in RAM,
                  inert; power cycle to clear it)

Rehearsal order (docs/ROADMAP.md): --survey; install+arm; a plain stick
(status NOFILE); the marker pack (--check-marker); the real pack.
"""
import os
import struct
import sys
import time

sys.path[:0] = [".", "tools"]
import usbpack as UP                      # noqa: E402  (MARKER_*, mods_bin)

OUT = "build/usbloader"
UL_BASE = 0x09890000
UL_END = UL_BASE + 0x10000
POOL, STOCK_OPEN = 0x08DE39A4, 0x08DDF53C
ST = ["IDLE", "NOFILE", "BADDEV", "SHORT", "MAGIC", "SIZE", "SUM", "CALLED"]
S2ST = ["IDLE", "NOFILE", "SHORT", "MAGIC", "COUNT", "SURVEY", "EXPSUM", "BADREC",
        "SUM", "ARMSTOCK", "DONE"]
UL_WORDS = ["magic", "calls", "mark", "status", "tries", "loads", "len", "sum", "done",
            "drive", "mode", "ctx"]
S2_WORDS = ["magic", "status", "nrec", "done", "bytes", "armed", "err_rec", "err_addr",
            "sum", "want"]


def syms():
    import permanent_build as PB
    return PB._syms(f"{OUT}/ul.syms"), PB._blob(f"{OUT}/ul.bin")


def rd32(h, a):
    from rgb_overview_apply import br
    return struct.unpack("<I", bytes(br(h, a, 4)))[0]


def words(h, base, names):
    import fastload
    raw = bytes(fastload.BR32(h, base, 4 * len(names)))
    return dict(zip(names, struct.unpack(f"<{len(names)}I", raw)))


def status(h, s):
    ul = words(h, s["ul_state"], UL_WORDS)
    st = ST[ul["status"]] if ul["status"] < len(ST) else ul["status"]
    print(f"loader: magic {'ok' if ul['magic'] == 0x54534C55 else 'MISSING'}, "
          f"calls {ul['calls']}, tries {ul['tries']}, status {st}, loads {ul['loads']}, "
          f"done {ul['done']}, drive {chr(ul['drive']) if 32 < ul['drive'] < 127 else '?'}")
    pool = rd32(h, POOL)
    print(f"hook pool {POOL:#x}: {pool:#x} ({'ARMED' if pool == s['ul_open'] else 'stock' if pool == STOCK_OPEN else 'UNKNOWN'})")
    mark = ul["mark"] & 0x1FFFFFFF
    if ul["loads"] and 0x09891000 <= mark < 0x098A0000:
        s2 = words(h, mark, S2_WORDS)
        st2 = S2ST[s2["status"]] if s2["status"] < len(S2ST) else s2["status"]
        print(f"stage-2: {st2}, records {s2['done']}/{s2['nrec']}, bytes {s2['bytes']:,}, "
              f"arms {s2['armed']}, error record {s2['err_rec']} at {s2['err_addr']:#x}, "
              f"sum {s2['sum']:#x} want {s2['want']:#x}")
        return ul, s2
    return ul, None


def main():
    a = sys.argv[1:]
    s, img = syms()
    assert s["ul_head"] == UL_BASE
    import hid
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    from rgb_overview_apply import br, send
    import fastload
    cands = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s)")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        pool = rd32(h, POOL)
        if a == ["--status"]:
            status(h, s)
            return 0
        if a == ["--check-marker"]:
            ul, s2 = status(h, s)
            want = UP.marker_bytes()
            got = bytes(fastload.BR32(h, UP.MARKER_AT, len(want)))
            bad = [i for i in range(len(want)) if got[i] != want[i]]
            print(f"marker {UP.MARKER_AT:#x}: {len(want)} B read back, {len(bad)} differ"
                  + (f" (first at +{bad[0]:#x}: got {got[bad[0]]:#04x} want {want[bad[0]]:#04x})" if bad else ""))
            ok = not bad and s2 is not None and s2["status"] == 10
            print("RESULT:", "PASS - the stick path copies bytes exactly on this deck"
                  if ok else "FAIL")
            return 0 if ok else 1
        if a == ["--revert"]:
            if pool == STOCK_OPEN:
                print("hook pool already stock")
                return 0
            if pool != s["ul_open"]:
                sys.exit(f"FAIL: pool holds {pool:#x}, not ours - nothing written")
            fastload.lw_words(h, POOL, STOCK_OPEN)
            ok = rd32(h, POOL) == STOCK_OPEN
            print("RESULT:", "reverted (loader bytes stay, inert; power cycle clears them)"
                  if ok else "FAIL - REBOOT the deck")
            return 0 if ok else 1
        if a == ["--rearm"]:
            if pool != s["ul_open"]:
                sys.exit(f"FAIL: loader not armed (pool {pool:#x})")
            done_at = s["ul_state"] + 4 * UL_WORDS.index("done")
            fastload.lw_words(h, done_at, 0)
            ok = rd32(h, done_at) == 0
            print("RESULT:", "re-armed: the next stick mount runs the loader again" if ok else "FAIL")
            return 0 if ok else 1
        if a == ["--survey"]:
            from rgb_overview_apply import read_block
            n = UL_END - UL_BASE
            blk = read_block(h, UL_BASE, n)
            nz = [i for i, b in enumerate(blk) if b]
            print(f"band {UL_BASE:#x}..{UL_END:#x}: {len(nz)} nonzero bytes"
                  + (f", first at {UL_BASE + nz[0]:#x}" if nz else ""))
            print(f"hook pool: {pool:#x} ({'stock' if pool == STOCK_OPEN else 'NOT STOCK'})")
            ok = not nz and pool == STOCK_OPEN
            print("SURVEY", "OK" if ok else "FAIL")
            return 0 if ok else 1
        if a:
            sys.exit(__doc__)
        # ---- install + arm
        if pool == s["ul_open"]:
            print("RESULT: PASS (already armed)")
            status(h, s)
            return 0
        if pool != STOCK_OPEN:
            sys.exit(f"FAIL: hook pool holds {pool:#x} (neither stock nor ours) - nothing written")
        want = img + bytes(s["end"] - UL_BASE - len(img))      # code + zeroed state
        left = fastload.install_region(h, UL_BASE, want, fastload.SESSION_BUDGET)
        if left:
            print(f"RESULT: PARTIAL ({left} B left) - run again")
            return 0
        if fastload.arm_later():
            print("loader written - run again to arm (fresh short session)")
            return 0
        if bytes(fastload.BR32(h, UL_BASE, len(img))) != img:
            sys.exit("FAIL: loader read back differs - nothing armed")
        scratch = s["ul_state"] + 4 * UL_WORDS.index("mark")
        if not fastload.lw_selftest(h, scratch):
            sys.exit("FAIL: LW self-test - nothing armed (deck unchanged)")
        fastload.lw_words(h, POOL, s["ul_open"])
        if rd32(h, POOL) != s["ul_open"]:
            sys.exit("FAIL: hook pool verify - REBOOT the deck")
        print(f"RESULT: PASS - loader armed. Insert a stick: plain = nothing happens "
              f"(--status says NOFILE); PIONEER/MODS/MODS.BIN + PACK.BIN = installed "
              f"once per power-up.")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
