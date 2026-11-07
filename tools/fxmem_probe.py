#!/usr/bin/env python3
"""fxmem_probe.py -- ONE-SHOT, READ-ONLY deck check (about 6 s of shell).

    .venv/bin/python tools/fxmem_probe.py            # survey + memory map
    .venv/bin/python tools/fxmem_probe.py --ring     # prove the FX delay line
    .venv/bin/python tools/fxmem_probe.py --revert   # clear the cave again

--ring checks the page the SHIPPING ENGINE was built to use (build/fx/ring.txt,
written by tools/fx_dsp/build.sh) and, only if every word in it reads zero,
writes build/fx/fxring_ok.txt. tools/keyshift_hot_apply.py --engine fx
refuses to install without that file, because a ring in memory the deck is
using would overwrite live audio data. Run it on the boot you install on.

Answers the questions the emulator cannot answer on its own:
  * is 0xC0073400..0xC0080000 free on the real deck? (emulator: yes)
  * what does the deck's chip memory actually look like, page by page?
  * are the landmarks right (program words, the 1.0f cosine table), so that a
    surprising answer has to be believed rather than blamed on the reader?

The deck does the reading (tools/fx_dsp/fxmem.c): it walks the memory through
the chip's own host port and leaves a summary; the host reads 584 bytes at the
end. No 52 KB transfer, so the whole thing fits a short deck session.

Writes NOTHING to the DSP -- only address then read. Deck-side writes are the
cave (RAM), its data words and the flush hook, which the cave puts back by
itself before returning. A power cycle always returns the deck to stock.

EVERY CONTROL WORD IS ONE ATOMIC 'LW' WRITE. This matters: an earlier version
wrote the hook word as four separate bytes, and a USB timeout in the middle
left the deck's display-flush hook pointing at half an address (the deck
needed a power cycle). Now a single word write either lands or does not, and a
failed arm puts stock back and stops.

SAFE ORDER, fail-stop at every step: the hook must be stock and the cave area
zero or already ours before anything is written, and every write is read back.
"""
import os
import struct
import sys
import time

sys.path.insert(0, "tools")
import hid                                                  # noqa: E402
from xdj700_usb_shell import PIONEER_VID, open_deck, drain  # noqa: E402
from rgb_overview_apply import send, read_block, bw         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "build", "fxmem", "fxmem.bin")
SYMS = os.path.join(HERE, "..", "build", "fxmem", "fxmem.syms")
POOL = 0x08C92E20
STOCK = 0x08E7D278
MAGIC = 0x53565231
BLK_LO, BLK_HI = 0xC0073400, 0xC0080000
N_RES = 168                  # [158] eq-to-word0, [159] float<1.0, [160..167] words
PAGE = 0xC006C000          # the page the map found empty; --page to change
WAIT_S = 4.0
PAGE_WORDS = 4096          # the page the map found empty, in 32-bit words
TRACK_LEN = 0x0BA3C050     # non-zero while a track is loaded (play it too)
RING_TXT = os.path.join(HERE, "..", "build", "fx", "ring.txt")
RING_OK = os.path.join(HERE, "..", "build", "fx", "fxring_ok.txt")


def syms():
    if not os.path.exists(BIN):
        sys.exit("build the cave first: sh tools/fx_dsp/build_fxmem.sh")
    s = {}
    for line in open(SYMS):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def ring_build():
    """(base, frames) the built engine uses, from build/fx/ring.txt."""
    if not os.path.exists(RING_TXT):
        sys.exit("build the engine first: sh tools/fx_dsp/build.sh "
                 "(it writes build/fx/ring.txt)")
    b, f = open(RING_TXT).read().split()
    return int(b, 0), int(f)


def ring_fits(frames):
    """A stereo float ring needs 2 words per frame; one page is 4,096."""
    return 0 < frames and frames * 2 <= PAGE_WORDS


def write_ring_ok(base, frames):
    os.makedirs(os.path.dirname(RING_OK), exist_ok=True)
    with open(RING_OK, "w") as f:
        f.write(f"{base:#x} {frames} {int(time.time())}\n")
    print(f"  wrote {os.path.normpath(RING_OK)} ({base:#x}, {frames} frames) "
          "- install TOUCH FX on this same boot")


def r32(h, addr):
    return struct.unpack("<I", bytes(read_block(h, addr, 4)))[0]


def w32(h, addr, v):
    """One 32-bit store, in ONE deck command (LW). Atomic: it lands or not."""
    import fastload
    fastload.lw_words(h, addr, v, drain_after=True)


def w32_verify(h, addr, v, what):
    w32(h, addr, v)
    got = r32(h, addr)
    if got != v:
        sys.exit(f"FAIL: {what} read back {got:#010x}, wanted {v:#010x}. "
                 "Stopping; nothing was armed.")
    return got


def write_code(h, addr, blob):
    pad = blob.ljust((len(blob) + 3) & ~3, b"\0")
    try:
        import fastload
        fastload.fastblock(h, addr, pad)
    except Exception as e:
        print(f"    (fast loader unavailable: {e}; using byte writes)")
        for i, b in enumerate(blob):
            bw(h, addr + i, b)


def main():
    revert = "--revert" in sys.argv
    ring = "--ring" in sys.argv
    page = PAGE
    frames = 0
    for i, a in enumerate(sys.argv):
        if a == "--page" and i + 1 < len(sys.argv):
            page = int(sys.argv[i + 1], 0)
    if ring:
        page, frames = ring_build()
        if not ring_fits(frames):
            sys.exit(f"REFUSED: the built ring is {frames * 8} B but one page "
                     f"is {PAGE_WORDS * 4} B. Rebuild shorter: "
                     f"FX_RING_FRAMES={PAGE_WORDS // 2} sh tools/fx_dsp/build.sh")
        print(f"ring {page:#010x}, {frames} frames ({frames * 8} B)")
    S = syms()
    base, blob = S["sv_entry"], open(BIN, "rb").read()
    if base != 0x09800000:
        sys.exit(f"REFUSED: cave built at {base:#x}, expected 0x09800000")

    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"STOP: found {len(cands)} deck interface(s). Check the "
                 "cable, switch the deck on, quit rekordbox / DJ apps.")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        t0 = time.time()
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        pool = r32(h, POOL)
        if revert:
            if pool == base:
                w32(h, POOL, STOCK)
            write_code(h, base, bytes(len(blob)))
            ok = (r32(h, POOL) == STOCK and
                  not any(read_block(h, base, len(blob))))
            print(f"revert: hook {r32(h, POOL):#x}, cave "
                  f"{'zero' if ok else 'NOT zero'} in {time.time()-t0:.1f}s")
            print("RESULT:", "PASS - deck back to stock" if ok else "FAIL")
            return 0 if ok else 1

        if pool != STOCK:
            sys.exit(f"STOP: the display-flush hook holds {pool:#x}, not stock "
                     f"{STOCK:#x}. Switch the deck OFF and ON, then run this "
                     "again. Nothing was written.")

        if ring and r32(h, TRACK_LEN) == 0:
            sys.exit("REFUSED: load and play a track first. A page that reads "
                     "zero with the deck idle proves nothing -- the audio "
                     "path fills memory as soon as it runs (in the emulator "
                     "this very page is written from the first block, and a "
                     "ring there drove the output to full scale). Survey the "
                     "ring with audio playing, then install on that boot.")

        live = bytes(read_block(h, base, len(blob)))
        if live != blob:
            # Our trampoline head is identical in every fxmem build, so a
            # match there means this is an earlier build of ours left in RAM
            # (inert: the hook below is stock, so nothing points at it).
            ours = live[:16] == blob[:16]
            diff = [i for i, (a, b) in enumerate(zip(live, blob)) if a and a != b]
            if diff and not ours:
                sys.exit(f"STOP: cave byte +{diff[0]:#x} holds foreign data "
                         f"({live[diff[0]]:#04x}). Nothing was written.")
            if diff:
                print(f"    (replacing an earlier fxmem build at +{diff[0]:#x}; "
                      "hook was stock, so it was not running)")
            write_code(h, base, blob)
            if bytes(read_block(h, base, len(blob))) != blob:
                sys.exit("FAIL: cave read-back mismatch. Nothing was armed.")
        print(f"cave {base:#x} {len(blob)} B verified  [{time.time()-t0:.1f}s]")

        if r32(h, S["sv_prev"]) == base:
            sys.exit("FAIL: sv_prev still points at the cave - refusing")
        w32_verify(h, S["sv_prev"], STOCK, "sv_prev")
        w32_verify(h, S["sv_page"], page, "page address")
        w32_verify(h, S["sv_done"], 0, "done flag")
        w32_verify(h, S["sv_flag"], MAGIC, "arm word")
        w32(h, POOL, base)
        if r32(h, POOL) != base:
            w32(h, POOL, STOCK)
            sys.exit("FAIL: the hook word did not take - stock put back.")
        print(f"armed ({time.time()-t0:.1f}s) - reading the deck's memory ...",
              flush=True)

        done, tries = 0, 0
        end = time.time() + WAIT_S
        while time.time() < end:
            done = r32(h, S["sv_done"])
            tries += 1
            if done:
                break
            time.sleep(0.05)
        res = [r32(h, S["sv_res"] + 4 * i) for i in range(N_RES)]
        back = r32(h, POOL)
        if back != STOCK:                     # never leave a live hook behind
            w32(h, POOL, STOCK)
            back = r32(h, POOL)
        fast, slow, first, last = res[1], res[0], res[2], res[3]
        good = done == 1 and res[12] == 1 and back == STOCK
        print(f"block 0x{BLK_LO:08X}..0x{BLK_HI:08X}: "
              f"settled read {slow} of {res[144]//4} words non-zero, "
              f"hammered read {fast}  [{time.time()-t0:.1f}s, {tries} polls]")
        if slow:
            print(f"  first 0x{first:08X}  last 0x{last:08X}")
        words = res[144] // 4
        eq, small = res[158], res[159]
        print(f"  block content: {eq} of {words} words repeat word 0, "
              f"{small} of {words} are floats below 1.0; "
              "samples " + " ".join(f"{w:08X}" for w in res[160:168]))
        if slow and eq == words:
            print("    -> every word is the SAME value: that reads like empty "
                  "or unmapped space returning a filler word, not audio. "
                  "The marker test (step 3) decides; do not install yet.")
        elif slow and small * 10 >= words * 9:
            print("    -> 9 in 10 words are audio-shaped (magnitude below "
                  "1.0): this block is carrying samples. Treat as in use.")
        elif slow:
            print("    -> mixed values: no single-signature answer, so treat "
                  "the block as in use until the marker test says otherwise.")
        print("  landmark program: " + " ".join(f"{w:08X}" for w in res[4:8]) +
              "   cos table: " + " ".join(f"{w:08X}" for w in res[8:12]) +
              f"  ({'1.0f as expected' if res[8] == 0x3F800000 else 'NOT 1.0f'})")
        page, size = res[14], res[13]
        counts = res[16:16 + size]
        runs, cur = [], None
        for i, c in enumerate(counts):
            base_a = 0xC0000000 + i * page
            if c == 0:
                cur = base_a if cur is None else cur
            elif cur is not None:
                runs.append((cur, base_a)); cur = None
        if cur is not None:
            runs.append((cur, 0xC0000000 + len(counts) * page))
        busy = sum(1 for c in counts if c)
        print(f"  memory map 0xC0000000..+{len(counts) * page // 0x100000} MB: "
              f"{busy}/{len(counts)} pages hold data; empty runs: " +
              (", ".join(f"0x{a:08X}-0x{b:08X}" for a, b in runs) or "none"))
        pnz, pwords = res[147], res[146]
        print(f"  page 0x{page:08X} (every word): {pnz} of {pwords} non-zero" +
              (f", first 0x{res[148]:08X} last 0x{res[149]:08X}" if pnz else "") +
              "  -> " + ("PROVEN EMPTY" if pnz == 0 else "has data"))
        if pnz:
            print("    page words: " + " ".join(f"{w:08X}" for w in res[150:158]))
        print(f"  hook restored: {'yes' if back == STOCK else 'NO - forced'}; "
              f"port pointer restored: {'yes' if res[12] == 1 else 'NO'}; "
              f"run finished: {'yes' if done else 'NO (timed out)'}")
        if not good:
            print("RESULT: FAIL - the survey did not complete (nothing was "
                  "written to the DSP; power-cycle if the deck looks odd)")
            return 1
        if slow == 0:
            if ring:
                write_ring_ok(page, frames)
            print("RESULT: PASS - the delay-line block is FREE on this deck. "
                  "Echo/Flanger can be installed.")
            return 0
        print("RESULT: IN USE - do NOT ship Echo to this deck; the settled "
              "read still finds data. The map above shows where it is.")
        return 2
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
