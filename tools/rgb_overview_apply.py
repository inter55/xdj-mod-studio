#!/usr/bin/env python3
"""rgb_overview_apply.py [blue|rgb|3band] [--revert] -- waveform styles on the DECK.

NOT YET RUN ON HARDWARE (written 2026-10-04, emulator PASS 17000/17000 px
via tools/rgb_overview.py emu). RAM only; reboot = stock. ONE shell open.

Order (fail-stop at every step, nothing irreversible):
  1. BR the 4-byte hook pool 0x08DBC5C8: must read stock 0x08C2C984.
  2. BR the cave region (312 B @ 0x088BA000): must be ALL ZERO (the same
     zero run as the decoded image; a nonzero byte = something lives
     there on the deck -> ABORT, pick another cave).
  3. BW every cave byte, then BR the region back: must equal the code.
  4. LAST: BW the 4 hook-pool bytes (cave address), BR verify.
     DO NOT LOAD A TRACK DURING STEP 4 (painter would read a half-written
     pointer). Then load a rekordbox-exported track: overview = colour.
--revert: hook pool -> stock FIRST (verified), then zero the cave.

Bytes come from tools/rgb_overview.plan() -- the same source the
emulator test validated.
"""
import re
import struct
import sys
import time

sys.path.insert(0, "tools")
import hid
from xdj700_usb_shell import PIONEER_VID, open_deck, drain, drain_values
from rgb_overview import plan, CAVE, HOOK_POOL, ORIG_RELEASE, MODE_ADDR, MODES, OLD_CAVES

CHUNK = 32
CHUNK_B = 4096      # bytes per byte-write fallback session

# Fast path (2026-10-10): shared LW 32-bit loader, ~4x fewer rounds and
# no per-byte wait. Falls back to chunked BW if the deck rejects it.


def send(h, s, secs):
    q = s.encode() + b"\r\x00"
    h.write(q + b"\x00" * (26 - len(q)))
    return drain(h, secs)


def br(h, addr, n):
    """Read n (<=32) bytes; retries a short window once."""
    for _ in range(2):
        q = f"{addr:x},{n - 1:x}BR\r\x00".encode()
        h.write(q + b"\x00" * (26 - len(q)))
        raw = drain_values(h, n, 9.0)
        vals = [int(v, 16) for v in re.findall(
            r"\b([0-9A-Fa-f]{2})\b(?:\s|$)", raw.decode("ascii", "replace"))]
        if len(vals) >= n:
            return vals[-n:]
    sys.exit(f"FAIL: short read at {addr:#x} ({len(vals)}/{n}) - aborting "
             f"(RAM only; reboot clears anything written)")


def read_block(h, base, size):
    if size >= 128:                      # 32-bit reads when the deck allows
        import fastload
        return fastload.BR32(h, base, size)
    out = []
    for off in range(0, size, CHUNK):
        n = min(CHUNK, size - off)
        out += br(h, base + off, n)
    return bytes(out)


def bw(h, addr, v):
    """Byte write. Waits for the deck's reply line (usually a few ms) instead
    of a fixed 0.4 s; never longer than before. Results are verified by
    read-back in every installer."""
    q = f"{addr:x},{v:02x}BW".encode() + b"\r\x00"
    h.write(q + b"\x00" * (26 - len(q)))
    end = time.time() + 0.4
    buf = bytearray()
    while time.time() < end:
        chunk = h.read(64, timeout=20)
        if chunk:
            buf.extend(chunk)
            if b"\n" in buf or b"\r" in buf:
                break


def put_pool(h, new):
    cur = struct.unpack("<I", bytes(br(h, HOOK_POOL, 4)))[0]
    old = cur if cur in [ORIG_RELEASE] + OLD_CAVES and new == CAVE else \
        {ORIG_RELEASE: CAVE, CAVE: ORIG_RELEASE}[new]
    if cur == new:
        print(f"hook pool already {new:#x}")
        return
    if cur != old and new != ORIG_RELEASE:
        sys.exit(f"FAIL: hook pool = {cur:#x}, expected {old:#x} - aborting")
    # restoring stock is always allowed: it repairs a half-written pointer
    for i, b in enumerate(struct.pack("<I", new)):
        bw(h, HOOK_POOL + i, b)
    got = struct.unpack("<I", bytes(br(h, HOOK_POOL, 4)))[0]
    if got != new:
        sys.exit(f"FAIL: hook pool re-read {got:#x}, wanted {new:#x}")
    print(f"hook pool {HOOK_POOL:#x}: {old:#x} -> {new:#x} verified")


def main():
    args = sys.argv[1:]
    check = args == ["--check"]
    if check:
        args = []
    revert = args == ["--revert"]
    cols = None
    if "--colours" in args:                   # custom: 1-3 '#rrggbb' comma list
        i = args.index("--colours")
        from rgb_overview import hex565
        cols = [hex565(c) for c in args[i + 1].split(",") if c.strip()][:3]
        del args[i:i + 2]
    if not revert and (len(args) > 1 or (args and args[0] not in MODES)):
        sys.exit("usage: rgb_overview_apply.py [blue|rgb|3band|custom --colours "
                 "#rrggbb[,#rrggbb[,#rrggbb]]] | --revert | --check")
    mode = MODES[args[0]] if args and not revert else MODES["rgb"]
    if mode == MODES["custom"] and not cols:
        sys.exit("custom needs --colours #rrggbb[,#rrggbb[,#rrggbb]]")
    code, _ = plan(mode, cols)
    cands = [d for d in hid.enumerate()
             if d["vendor_id"] == PIONEER_VID and d["usage"] == 0x01]
    if len(cands) != 1:
        sys.exit(f"found {len(cands)} shell interface(s); check cable/power "
                 f"and quit music/MIDI apps")
    h = open_deck(PIONEER_VID, cands[0]["product_id"], 0x01)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        if check:                         # READ-ONLY survey, writes nothing
            hook = struct.unpack("<I", bytes(br(h, HOOK_POOL, 4)))[0]
            live = read_block(h, CAVE, len(code))
            mb = br(h, MODE_ADDR, 1)[0]
            print(f"hook pool {HOOK_POOL:#x} = {hook:#x} "
                  f"({'STOCK' if hook == ORIG_RELEASE else 'MOD' if hook == CAVE else 'UNEXPECTED'})")
            print(f"cave {CAVE:#x}+{len(code)}: "
                  + ("all zero (usable)" if not any(live) else
                     "already our code" if live == code else
                     f"NONZERO ({sum(1 for b in live if b)} bytes) - do not use"))
            print(f"mode byte {MODE_ADDR:#x} = {mb}")
            return 0
        if revert:
            put_pool(h, ORIG_RELEASE)
            for i in range(len(code)):
                bw(h, CAVE + i, 0)
            bw(h, MODE_ADDR, 0)
            if read_block(h, CAVE, len(code)) != bytes(len(code)):
                sys.exit("FAIL: cave not zeroed (hook already stock: safe)")
            print("RESULT: PASS - stock overview restored")
            return 0
        cur = struct.unpack("<I", bytes(br(h, HOOK_POOL, 4)))[0]
        if cur not in [ORIG_RELEASE, CAVE] + OLD_CAVES:
            sys.exit(f"FAIL: hook pool = {cur:#x} (unexpected) - aborting")
        moving = cur in OLD_CAVES
        live = read_block(h, CAVE, len(code))
        if live != code:
            # resumable + chunked: long shell sessions wedge the deck's USB
            # (2026-10-04, twice, at 4 and 16 min). Write at most CHUNK_B
            # differing bytes per session, then exit; rerun to continue.
            bad = [i for i, (a, b) in enumerate(zip(live, code))
                   if a != b and a != 0]
            if bad:
                sys.exit(f"FAIL: cave byte +{bad[0]:#x} holds foreign data "
                         f"({live[bad[0]]:#04x}) - aborting, nothing written")
            todo = [i for i, (a, b) in enumerate(zip(live, code)) if a != b]
            t = time.time()
            try:
                import fastload
                # Whole-word write: LW overwrites 4 bytes, so clobbering a
                # nearby live byte is unsafe. Fast path only when every
                # partial word (live!=0 but not fully differing) is zero --
                # which the earlier `bad` check already proved for the
                # target bytes themselves.
                todo = [i for i, (a, b) in enumerate(zip(live, code)) if a != b]
                full_words = (todo[0] & ~3, (todo[-1] + 4) & ~3)
                w0, w1 = full_words
                safe = all(live[j] == 0 or live[j] == code[j] or j in todo
                           for j in range(w0, w1))
                if todo and safe:
                    w_start = (todo[0]) & ~3
                    w_end = (todo[-1] + 4) & ~3
                    blob = code[w_start:w_end]
                    assert (CAVE + w_start) % 4 == 0, "CAVE must be 4-aligned"
                    fastload.fastblock(h, CAVE + w_start, blob)
                    if read_block(h, CAVE, len(code)) != code:
                        sys.exit("FAIL: verify mismatch - nothing armed")
                    print(f"fast path: {w_end - w_start} B in "
                          f"{time.time() - t:.0f}s")
                    print("cave complete - run again to arm the hook in a "
                          "fresh short session")
                    return 0
                raise RuntimeError("partial-word danger - use BW")
            except SystemExit:
                raise
            except Exception as e:
                print(f"fast path unavailable ({e}); falling back to BW")
                for i in todo[:CHUNK_B]:
                    bw(h, CAVE + i, code[i])
                print(f"wrote {min(len(todo), CHUNK_B)} cave bytes in "
                      f"{time.time() - t:.0f}s; {max(0, len(todo) - CHUNK_B)} left")
                if len(todo) > CHUNK_B:
                    print("RESULT: PARTIAL - run again to continue "
                          "(hook NOT armed, deck behaviour unchanged)")
                    return 0
            if read_block(h, CAVE, len(code)) != code:
                sys.exit("FAIL: cave verify mismatch - hook NOT armed")
            print("cave complete - run again to arm the hook in a fresh "
                  "short session")
            return 0
        print("cave verified")
        # the snapshot routine (called at cave entry) must be in place
        # BEFORE the hook is armed
        from rgb_overview import extra_writes
        ex = extra_writes()
        live_ex = read_block(h, ex[0][0], len(ex))
        want_ex = bytes(b for _, b in ex)
        if live_ex != want_ex:
            if any(a and a != b for a, b in zip(live_ex, want_ex)):
                sys.exit("FAIL: snapshot-routine area holds foreign data")
            diff = [(a, b) for (a, b), cur in zip(ex, live_ex) if cur != b]
            for a, b in diff[:CHUNK_B]:
                bw(h, a, b)
            if len(diff) > CHUNK_B:
                print("RESULT: PARTIAL (snapshot routine) - run again")
                return 0
            if read_block(h, ex[0][0], len(ex)) != want_ex:
                sys.exit("FAIL: snapshot routine verify - hook NOT armed")
            print("snapshot routine written - run again to arm")
            return 0
        if mode == MODES["custom"]:          # the colour table, before the mode
            from rgb_overview import custom_writes, CUSTOM_TAB
            tw = custom_writes(cols)
            want_t = bytes(b for _, b in tw)
            live_t = read_block(h, CUSTOM_TAB, len(tw))
            if live_t != want_t:
                diff = [(a, b) for (a, b), cur_ in zip(tw, live_t) if cur_ != b]
                for a, b in diff[:CHUNK_B]:
                    bw(h, a, b)
                if len(diff) > CHUNK_B:
                    print("RESULT: PARTIAL (colour table) - run again")
                    return 0
                if read_block(h, CUSTOM_TAB, len(tw)) != want_t:
                    sys.exit("FAIL: colour table verify - mode NOT set")
                print(f"custom colour table written ({len(tw)} B)")
        cur_mode = br(h, MODE_ADDR, 1)[0]
        if cur_mode not in MODES.values():
            sys.exit(f"FAIL: mode byte reads {cur_mode:#x} - aborting")
        bw(h, MODE_ADDR, mode)
        if br(h, MODE_ADDR, 1)[0] != mode:
            sys.exit("FAIL: mode byte verify")
        print(f"mode {cur_mode} -> {mode} verified")
        put_pool(h, CAVE)
        if moving:                    # hook now on the new cave: clear old
            old_live = read_block(h, cur, len(code))
            for i in [i for i, b in enumerate(old_live) if b][:CHUNK_B]:
                bw(h, cur + i, 0)
            if any(read_block(h, cur, len(code))):
                print("WARN: old cave not fully zeroed (cosmetic only)")
            else:
                print(f"old cave {cur:#x} zeroed (dotted-line glitch gone)")
        print("RESULT: PASS - load a rekordbox-analysed track; the bottom "
              "overview should be colour. --revert restores stock.")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
