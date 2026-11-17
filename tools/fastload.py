#!/usr/bin/env python3
"""fastload.py -- SHARED fast block loader for deck installers.

Why: the byte-write (BW) path costs one shell round per byte. Real-world
installs wall-clock at ~30 min and twice (2026-10-04) long sessions wedged
the deck's USB link. This module cuts that:

  * one LW (32-bit, table-verified command) per 4 aligned bytes -> ~4x
    fewer rounds than BW,
  * no per-write drain: commands are pipelined in bursts, the deck replies
    are drained AFTER the burst (BW could not be trusted unsynced; LW's
    handler is a single mov.l per the decoded table + handler disasm),
  * every 4 B pays an aligned write so no partial-word hazards,
  * read-back verification per session block (BR 32 bytes per round),
  * fail-stop on any verify mismatch, and NEVER arms hooks here - the
    arming session stays tiny and separately paced (shell session rules:
    one open per command, 30 s pacing handled by the calling installer).

API:
    fastblock(h, base, blob) -> None    write blob at base (4-aligned),
                                        pipelined LW bursts + BR verify
    lw_selftest(h, addr) -> bool        write-read-back-restore of one
                                        scratch word in a KNOWN-EMPTY area
                                        of our own cave
    BR32(h, base, size) -> bytes        block read in 32-B rounds

Safety: RAM window only (0x08000000-0x0FFFFFFF, asserted), fail-stop,
verify-after-write like every installer. Nothing here touches flash or
hooks; installers keep the no-track / not-armed gates and do the arming
in their own fresh sessions.
"""
import os
import re
import struct
import sys
import time

sys.path.insert(0, "tools")

RAM_LO, RAM_HI = 0x08000000, 0x10000000
SESSION_BUDGET = 32768   # bytes written per session on the LW path (~1 min)
LAST_WROTE = False       # install_region wrote something this call
BURST = 32          # LW commands pipelined before draining (=128 B = one LR verify read)
READ_CHUNK = 32     # BR reads 32 values max per round


def _q(cmd_bytes, *fill):
    q = bytearray(cmd_bytes)
    q += b"\r\x00"
    while len(q) < 26:
        q += b"\x00"
    return bytes(q)


def _parse_values(raw):
    """ASCII 'XX ' triples across 16-B reports -> byte list."""
    toks = re.findall(rb"\b([0-9A-Fa-f]{2})\b", raw)
    return [int(t, 16) for t in toks]


FIRST_SAMPLE = int(os.environ.get("XDJ_FIRST_SAMPLE", "4"))  # 1 = full read
_LW_SESSION = None  # (handle, open time) whose LW self-test passed
LR_OK = None        # 32-bit reads: None = untested this session, True/False


def _flush(h):
    """Drop anything left over from an earlier reply (slow USB stacks, e.g.
    Windows, can deliver the tail of one reply after the next request)."""
    for _ in range(50):
        if not h.read(64, timeout=5):
            return


def _done(raw, count, n, quiet=0.0):
    """n values seen AND the reply has ended: a CR/LF after them, or no new
    bytes for 150 ms (in case a reply type has no line end)."""
    return count >= n and (b"\n" in raw or b"\r" in raw or quiet > 0.15)


def _lr_round(h, addr, n, tries=3):
    """n (<=32) u32 values from addr (4-aligned) via the shell's LR.
    Retries a short/garbled reply (tries in all) before giving up."""
    pat = re.compile(rb"\b([0-9A-Fa-f]{8})\b")
    raw = bytearray()
    for k in range(tries):
        if k and not raw.strip():          # silent: revive the shell first
            try:
                from xdj700_usb_shell import cdj
                cdj(h, 1.0)
            except Exception:
                pass
        _flush(h)
        h.write(_q(f"{addr:x},{n - 1:x}LR".encode()))
        raw = bytearray()
        end = time.time() + 9.0
        last = time.time()
        while time.time() < end and not _done(raw, len(pat.findall(raw)), n, time.time() - last):
            chunk = h.read(64, timeout=20)
            if chunk:
                raw.extend(chunk)
                last = time.time()
        vals = [int(t, 16) for t in pat.findall(raw)]
        if len(vals) >= n:
            return vals[-n:]
    return None


def _lr_usable(h, base):
    """One-time check per session: an LR round must equal the same bytes
    read with BR (byte order + format proven on THIS deck), else every
    read stays on BR. Read-only either way."""
    global LR_OK
    if LR_OK is None:
        a = base & ~3
        v = _lr_round(h, a, 8)
        b = _br_only(h, a, 32)
        LR_OK = v is not None and b"".join(struct.pack("<I", x) for x in v) == b
        print(f"  32-bit reads: {'on (4x faster)' if LR_OK else 'off (byte reads)'}")
    return LR_OK


def BR32(h, base, size):
    """Read `size` bytes: 32-bit LR rounds (128 B each) when this deck
    proves them equal to BR, else 32-B BR rounds."""
    assert RAM_LO <= base < RAM_HI and RAM_LO <= base + size <= RAM_HI
    if size >= 128 and _lr_usable(h, base):
        out = bytearray()
        head = (-base) % 4
        if head:
            out += _br_only(h, base, head)
        a, left = base + head, size - head
        while left >= 4:
            n = min(READ_CHUNK, left // 4)
            v = _lr_round(h, a, n)
            if v is None:
                sys.exit(f"fastload: short LR at {a:#x} - aborting")
            out += b"".join(struct.pack("<I", x) for x in v)
            a += 4 * n
            left -= 4 * n
        if left:
            out += _br_only(h, a, left)
        return bytes(out)
    return _br_only(h, base, size)


def _br_only(h, base, size):
    """Read `size` bytes in 32-B BR rounds (parse via XX triples)."""
    assert RAM_LO <= base < RAM_HI and RAM_LO <= base + size <= RAM_HI
    out = bytearray()
    off = 0
    while off < size:
        n = min(READ_CHUNK, size - off)
        q = f"{base + off:x},{n - 1:x}BR\r\x00".encode()
        raw = bytearray()
        for k in range(3):                 # retry a short/garbled reply
            if k and not raw.strip():      # silent: revive the shell first
                try:
                    from xdj700_usb_shell import cdj
                    cdj(h, 1.0)
                except Exception:
                    pass
            _flush(h)
            h.write(_q(q))
            raw = bytearray()
            end = time.time() + 9.0
            last = time.time()
            # each value is 'XX ' = 3 chars; harvest when the line is complete
            while time.time() < end and not _done(raw, len(_parse_values(raw)), n, time.time() - last):
                chunk = h.read(64, timeout=20)
                if chunk:
                    raw.extend(chunk)
                last = time.time()
            vals = _parse_values(raw)
            if len(vals) >= n:
                break
        if len(vals) < n:
            sys.exit(f"fastload: short BR at {base + off:#x} "
                     f"({len(vals)}/{n}) - aborting")
        out.extend(vals[-n:])
        off += n
    return bytes(out)


def lw_selftest(h, addr, val_sentinel=0x58A):
    """Write a sentinel word to an EMPTY (zero) 4-aligned scratch address
    inside our own cave, read back, restore 0, re-read. Returns True iff
    everything round-tripped exactly. Never use on live data."""
    assert RAM_LO <= addr < RAM_HI and addr % 4 == 0, f"{addr:#x} bad site"
    cur = struct.unpack("<I", BR32(h, addr, 4))[0]
    if cur != 0:
        sys.exit(f"fastload: selftest site {addr:#x} is {cur:#x}, "
                 f"refusing (wanted zero scratch)")
    h.write(_q(f"{addr:x},{val_sentinel:x}LW\r\x00".encode()))
    time.sleep(0.3)
    got = struct.unpack("<I", BR32(h, addr, 4))[0]
    if got != val_sentinel:
        sys.exit(f"fastload: LW selftest wrote {got:#x}, wanted "
                 f"{val_sentinel:#x} - staying on byte writes")
    h.write(_q(f"{addr:x},0LW\r\x00".encode()))
    time.sleep(0.3)
    got2 = struct.unpack("<I", BR32(h, addr, 4))[0]
    if got2 != 0:
        sys.exit(f"fastload: LW selftest restore read {got2:#x}, wanted 0")
    return True


def lw_words(h, addr, val, drain_after=False):
    h.write(_q(f"{addr:x},{val & 0xFFFFFFFF:x}LW\r\x00".encode()))
    if drain_after:
        _sweep(h, 0.2)


def _sweep(h, secs):
    """Collect replies without blocking the pipeline."""
    end = time.time() + secs
    while time.time() < end:
        chunk = h.read(64, timeout=20)
        if not chunk:
            break


def fastblock(h, base, blob):
    """Write `blob` at `base` using pipelined LW words + per-burst BR
    verify. Handles a non-aligned start/end (.ends fall back to BW pairs
    imported from rgb_overview_apply to avoid new dependency cycles)."""
    assert RAM_LO <= base < RAM_HI, f"{base:#x} outside RAM window"
    assert RAM_LO <= base + len(blob) <= RAM_HI, "blob spills RAM window"
    if base % 4 or len(blob) % 4:
        sys.exit("fastload: base and blob length must be 4-aligned "
                 "(pad the blob)")
    # pipeline LWs in BURST groups; verify each group right after
    n_words = len(blob) // 4
    t0 = time.time()
    for w in range(n_words):
        off = 4 * w
        val = blob[off] | (blob[off + 1] << 8) | (blob[off + 2] << 16) | \
            (blob[off + 3] << 24)
        lw_words(h, base + off, val)
        if (w + 1) % BURST == 0 or w == n_words - 1:
            _sweep(h, 0.25)
            start = 4 * (w - (w % BURST))      # group start word offset
            got = BR32(h, base + start, 4 * ((w % BURST) + 1))
            exp = blob[start:start + len(got)]
            if got != exp:
                bad = next((i for i, (a, b) in enumerate(zip(got, exp))
                            if a != b), None)
                sys.exit(f"fastload: verify mismatch at {base + start + bad:#x}"
                         f" ({got[bad]:#04x} != {exp[bad]:#04x}) - aborting")
    print(f"fastload: {len(blob)} bytes in {time.time() - t0:.0f}s "
          f"({n_words} LW)")


QUICK_SESSION_S = 45     # a session this short may also arm (see arm_later)


def arm_later():
    """True when this session wrote something AND has already run long:
    arming then waits for a fresh short session (the 2026-10-04 lesson:
    the link died at the END of long write sessions). A quick write
    session arms straight away (one 30 s cool-down saved per mod)."""
    if not LAST_WROTE:
        return False
    try:
        import xdj700_usb_shell as S
        t0 = S.OPEN_TIME
    except Exception:
        t0 = None
    return t0 is None or time.time() - t0 > QUICK_SESSION_S


def _known_zero(serial, base, n):
    import json
    import os
    try:
        c = json.load(open(os.path.expanduser(os.environ.get(
            "XDJ_SURVEY_CACHE", "~/.xdj700_surveys.json"))))
    except Exception:
        return False
    return f"{base:#x}:{n:#x}" in c.get(serial, {})


def _remember_zero(serial, base, n):
    import json
    import os
    import time as _t
    p = os.path.expanduser(os.environ.get("XDJ_SURVEY_CACHE", "~/.xdj700_surveys.json"))
    try:
        c = json.load(open(p))
    except Exception:
        c = {}
    c.setdefault(serial, {})[f"{base:#x}:{n:#x}"] = _t.strftime("%Y-%m-%d")
    json.dump(c, open(p, "w"), indent=1)


def install_region(h, base, want, budget, allow_live=(), cur=None):
    """Bring RAM [base, base+len(want)) to `want`, one budget-sized window
    per session, reading as little as safety allows:

      * a window whose wanted bytes are all ZERO (work areas): sampled the
        first time on this deck (1 in FIRST_SAMPLE 128-B blocks; foreign-
        data check), remembered per
        deck like the surveys, spot-checked (1 in 16 rows) after that;
      * a window with code/data: read in full and compared byte for byte;
        equal = written earlier -> skip; otherwise the window is
        foreign-checked (any byte neither zero nor wanted, outside
        allow_live, stops everything) and written (32-bit LW bursts, each
        burst read back; byte writes if LW is unavailable) -- and the
        session ends there. (A one-row probe cannot tell a small patch
        from installed bytes: 2026-10-16 a 4-byte diagnostic stub was
        silently skipped on deck because only its last row was probed.)
    Returns bytes still to write (0 = done). LAST_WROTE says whether this
    call wrote anything (installers arm only in a session that did not).
    `cur` (a fresh full read by the caller) skips the reads it covers.
    """
    global LAST_WROTE
    LAST_WROTE = False
    assert RAM_LO <= base and base + len(want) <= RAM_HI
    serial = str(getattr(h, "serial", "") or "unknown")
    have = cur if cur is not None and len(cur) == len(want) else None
    W = max(4096, budget) & ~31
    left = 0
    for w0 in range(0, len(want), W):
        w1 = min(len(want), w0 + W)
        seg = want[w0:w1]
        if not any(seg):                                   # zero work area
            if have is not None:
                live = have[w0:w1]
            elif _known_zero(serial, base + w0, w1 - w0):
                rows = list(range(0, w1 - w0, 32 * 16)) + [((w1 - w0 - 1) // 32) * 32]
                live = bytearray(w1 - w0)
                for r in rows:
                    live[r:r + 32] = BR32(h, base + w0 + r, min(32, w1 - w0 - r))
            else:
                # first time on this deck: 128-B blocks, 1 in FIRST_SAMPLE +
                # the last one (live data is never zero at every sample; the
                # ranges used were also proven zero idle/loaded/playing)
                live = bytearray(w1 - w0)
                blk = 128
                offs = list(range(0, w1 - w0, blk * FIRST_SAMPLE))
                offs.append(max(0, (w1 - w0 - blk) & ~3))
                for r in sorted(set(offs)):
                    n_ = min(blk, w1 - w0 - r)
                    live[r:r + n_] = BR32(h, base + w0 + r, n_) if n_ >= 128 \
                        else _br_only(h, base + w0 + r, n_)
            nz = [i for i, x in enumerate(live) if x and (base + w0 + i) not in allow_live]
            if nz:
                sys.exit(f"FAIL: {base + w0 + nz[0]:#x} holds foreign data "
                         f"({live[nz[0]]:#04x}) - nothing written")
            if have is None and serial != "unknown":
                _remember_zero(serial, base + w0, w1 - w0)
            continue
        if LAST_WROTE:                                     # one window per session
            left += sum(1 for x in seg if x)
            continue
        last = max(i for i in range(0, w1 - w0, 32) if any(seg[i:i + 32]))
        if have is not None:
            live = have[w0:w1]
        elif _known_zero(serial, base + w0, w1 - w0):
            # this deck showed this window holds nothing foreign before:
            # spot-check 1 row in 16 (zero or ours), then (re)write it all
            ours = True
            for r in list(range(0, w1 - w0, 32 * 16)) + [last]:
                row = BR32(h, base + w0 + r, min(32, w1 - w0 - r))
                for i, x in enumerate(row):
                    if x and x != seg[r + i] and (base + w0 + r + i) not in allow_live:
                        sys.exit(f"FAIL: {base + w0 + r + i:#x} holds foreign data "
                                 f"({x:#04x}) - nothing written")
                ours = ours and bytes(row) == bytes(seg[r:r + len(row)])
            # 2026-10-18: every sampled row already ours -> read it all and
            # skip when complete. Without this a remembered-zero window was
            # rewritten EVERY session and multi-window installs (themes,
            # 76 KB) never got past it (mock: 40 sessions, "85681 left").
            if ours:
                live = BR32(h, base + w0, w1 - w0)
                if bytes(live) == bytes(seg):
                    continue
            else:
                live = bytes(w1 - w0)
        else:
            live = BR32(h, base + w0, w1 - w0)
            if bytes(live) == bytes(seg):
                continue                           # written earlier, verified
        rem = _write_window(h, base + w0, bytes(seg), bytes(live), allow_live)
        if rem == 0 and serial != "unknown" and have is None:
            _remember_zero(serial, base + w0, w1 - w0)   # nothing foreign here
        LAST_WROTE = LAST_WROTE or rem is not None
        left += rem or 0
    return left


def _write_window(h, base, want, cur, allow_live):
    """foreign check + write one window; returns 0 when done, None when it
    already matched (nothing written)"""
    bad = [i for i, (c, w) in enumerate(zip(cur, want))
           if c != w and c != 0 and (base + i) not in allow_live]
    if bad:
        sys.exit(f"FAIL: {base + bad[0]:#x} holds foreign data "
                 f"({cur[bad[0]]:#04x}) - nothing written")
    diff = [i for i, (c, w) in enumerate(zip(cur, want)) if c != w]
    if not diff:
        return None
    lo, hi = base, base + len(want)
    words = sorted({(base + i) & ~3 for i in diff})
    words = [w for w in words if lo <= w and w + 4 <= hi]
    # self-test site: any word of OUR window that is zero right now (the
    # window write overwrites it right after; dense code windows have no
    # word that is zero in `want` too -- 2026-10-12 FLAC+Opus fell back to
    # 4 KB byte-write sessions because of that). One test per session.
    scratch = next((base + i for i in range(len(want) - 4, -1, -4)
                    if (base + i) % 4 == 0 and not any(cur[i:i + 4])), None)
    use_lw = False
    global _LW_SESSION
    try:
        import xdj700_usb_shell as S
        sess = (id(h), getattr(S, "OPEN_TIME", None))
    except Exception:
        sess = None
    if words and sess is not None and _LW_SESSION == sess:
        use_lw = True
    elif scratch is not None and words:
        try:
            use_lw = lw_selftest(h, scratch)
        except SystemExit:
            use_lw = False
        if use_lw:
            _LW_SESSION = sess
    from rgb_overview_apply import bw
    written = set()
    if use_lw:
        run = []
        for w in words:
            if run and w != run[-1] + 4:
                _block(h, run, base, want)
                run = []
            run.append(w)
        if run:
            _block(h, run, base, want)            # exits on any verify mismatch
        for w in words:
            written.update(range(w - base, w - base + 4))
    todo = [i for i in diff if i not in written]
    if not use_lw:
        todo = todo[:4096]                        # byte writes: short sessions
    for i in todo:                                # edges / no-LW fallback
        bw(h, base + i, want[i])
    if todo:
        got = BR32(h, base, len(want))
        for i in todo:
            if got[i] != want[i]:
                sys.exit(f"FAIL: verify {base + i:#x} = {got[i]:#04x}, "
                         f"wanted {want[i]:#04x} - nothing armed")
        written.update(todo)
    return len([i for i in diff if i not in written])


def _block(h, run, base, want):
    a = run[0]
    blob = bytes(want[a - base:a - base + 4 * len(run)])
    fastblock(h, a, blob)


def main():
    print("fastload.py is a library, not a command")
    return 0


if __name__ == "__main__":
    sys.exit(main())
