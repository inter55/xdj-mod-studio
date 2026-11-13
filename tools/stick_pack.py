#!/usr/bin/env python3
"""stick_pack.py -- the mods FILE for a USB stick (XDJMODS/PACK.BIN).

A pack is a RECORDED cable install: the real deck installers
(tools/deck_session.py, same options as the app's Mods page) run against
the mock deck -- the emulator answering the deck's USB shell -- and every
RAM write is logged in order (MOCK_WRITE_LOG). This turns the log into
pack rev 2, which the on-deck loader (tools/usbloader/lc.c, -DSTICK)
replays at stick mount: same bytes, same order, so the stick gives exactly
what the cable gives.

Runs: consecutive log writes to adjacent addresses are merged. A run is
FIRST-TOUCH (no earlier run wrote any of its bytes) or RE-TOUCH. First-
touch runs carry a precondition the loader checks for the whole table
BEFORE writing anything: F_ZERO (all zero, inside a deck-surveyed free
band: a cave) or F_OLD (CRC of the bytes the installer found: stock pool
words, settings). Re-touch runs depend on earlier runs and are unchecked.

    stick_pack.py record [deck_session args] --out PACK.BIN   (mock install + pack)
    stick_pack.py pack LOG --out PACK.BIN                      (from a write log)
    stick_pack.py marker --at ADDR --size N --out PACK.BIN     (the cache test:
        no mods, N bytes of a known pattern into a zero home, so a read-back
        over the cable proves the copy-back cache handling on silicon)
"""
import binascii
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tools")]
import lzss_boot as L  # noqa: E402

MAGIC = b"XDJP001\x00"
REV = 2
ESZ = 24
F_ZERO, F_OLD, F_CALL = 1, 2, 4
MARKER_AT, MARKER_SIZE = 0x09779000, 4096     # a zero home inside band 1


def marker_bytes(size):
    """the rehearsal pattern: position-dependent, so a shifted or stale copy
    fails the comparison instead of matching"""
    return bytes(((i * 7 + 3) ^ (i >> 8)) & 0xFF for i in range(size))
POOL_FLUSH = 0x08C92E20
KSH_BUSY = 8                    # ksh_status "DSP link busy, try again"


def _syms(path):
    out = {}
    try:
        for line in open(os.path.join(ROOT, path)):
            a, _, n = line.split()
            out[n.lstrip("_")] = int(a, 16)
    except OSError:
        pass
    return out


def one_shots():
    """{flush-hook entry: (function to call, status addr)} for caves that
    do their work at the next screen flush and then unhook themselves:
    live key shift / TOUCH FX engine (both the ksh cave)."""
    out = {}
    for p in ("build/keyshift/ksh.syms", "build/fx/ksh.syms"):
        s = _syms(p)
        if {"ksh_entry", "ksh_hook", "ksh_status"} <= set(s):
            out[s["ksh_entry"]] = (s["ksh_hook"], s["ksh_status"])
    return out


def with_calls(log):
    """The write log plus CALL events where the mock deck ran a one-shot
    between two installer writes: the installer armed it on the flush
    pool, and the next pool write finds it already unhooked (the CPU ran
    it). A replay is instant -- no flush in between -- so the loader calls
    the one-shot itself at that point."""
    shots = one_shots()
    ev = []
    pending = None                               # one-shot armed, not yet run
    for addr, old, new in log:
        if addr == POOL_FLUSH and len(new) == 4:
            cur = struct.unpack("<I", old)[0]
            if pending is not None and cur != pending[0]:
                ev.append(("call",) + pending[1])
                pending = None
            v = struct.unpack("<I", new)[0]
            if v in shots:
                pending = (v, shots[v])
        ev.append(("w", addr, old, new))
    if pending is not None:
        ev.append(("call",) + pending[1])
    return ev
MAXE = 1200   # must match tools/usbloader/lc.c MAXE (2026-11-03: a full
              # install records 1036 runs; the old 1024 refused its own pack)
BANDS = ((0x0972B000, 0x0980F000), (0x09836000, 0x0996D000),
         (0x0996E000, 0x09A00000))
LOADER = (0x09890000, 0x098A0000)


def read_log(path):
    out, b = [], open(path, "rb").read()
    i = 0
    while i < len(b):
        addr, n = struct.unpack_from("<IB", b, i)
        i += 5
        old, new = b[i:i + n], b[i + n:i + 2 * n]
        i += 2 * n
        out.append((addr, old, new))
    return out


def runs(log):
    """[(addr, old bytes (first-touch) or None, new bytes)] in write order,
    with ("call", fn, status_addr) tuples where a one-shot must run."""
    touched = set()
    res = []
    cur = None                                   # [addr, old|None, new]
    for e in with_calls(log):
        if e[0] == "call":
            if cur is not None:
                res.append(cur)
                cur = None
            res.append(e)
            continue
        _, addr, old, new = e
        first = not any(a in touched for a in range(addr, addr + len(new)))
        if (cur is not None and addr == cur[0] + len(cur[2])
                and (cur[1] is not None) == first):
            if first:
                cur[1] += old
            cur[2] += new
        else:
            if cur is not None:
                res.append(cur)
            cur = [addr, bytearray(old) if first else None, bytearray(new)]
        touched.update(range(addr, addr + len(new)))
    if cur is not None:
        res.append(cur)
    return [r if r[0] == "call" else (r[0], bytes(r[1]) if r[1] is not None else None,
                                      bytes(r[2])) for r in res]


def in_band(a, n):
    return any(lo <= a and a + n <= hi for lo, hi in BANDS)


def build(rs):
    if len(rs) > MAXE:
        raise SystemExit(f"{len(rs)} runs: more than the loader's {MAXE}")
    ents, bodies = [], b""
    off = 0x10 + len(rs) * ESZ
    for r in rs:
        if r[0] == "call":
            ents.append(struct.pack("<IIIIHHI", r[1], 0, KSH_BUSY, r[2], F_CALL, 0, 0))
            continue
        a, old, new = r
        if LOADER[0] < a + len(new) and a < LOADER[1]:
            raise SystemExit(f"run {a:#x} overlaps the loader")
        fl, oc = 0, 0
        if old is not None:
            if in_band(a, len(new)) and not any(old):
                fl = F_ZERO
            else:
                fl, oc = F_OLD, binascii.crc32(old) & 0xFFFFFFFF
        z = bytes(L.encode(new))
        pad = (-off) % 0x10
        ents.append(struct.pack("<IIIIHHI", a, len(new),
                                binascii.crc32(new) & 0xFFFFFFFF, oc, fl, 0, len(z)))
        bodies += bytes(pad) + z
        off += pad + len(z)
    tbl = b"".join(ents)
    head = MAGIC + struct.pack("<HHI", REV, len(rs), binascii.crc32(tbl) & 0xFFFFFFFF)
    return head + tbl + bodies


def apply_model(mem_r8, mem_w8, pack):
    """Python model of lc.c's replay (tests compare it with the deck)."""
    n = struct.unpack_from("<H", pack, 10)[0]
    pos = 0x10 + n * ESZ
    for i in range(n):
        a, ln, crc, oc, fl, _, zl = struct.unpack_from("<IIIIHHI", pack, 0x10 + i * ESZ)
        if fl & F_CALL:
            continue
        pos += (-pos) % 0x10
        data = bytes(L.decode(pack[pos:pos + zl]))[:ln]
        pos += zl
        for k, v in enumerate(data):
            mem_w8(a + k, v)


def summary(rs):
    print(f"one-shot calls: {sum(1 for r in rs if r[0] == 'call')}")
    rs = [r for r in rs if r[0] != "call"]
    big = sum(len(n) for a, o, n in rs if len(n) > 64)
    print(f"{len(rs)} runs, {sum(len(n) for _, _, n in rs)} bytes "
          f"({big} in caves), checked first-touch: "
          f"{sum(1 for _, o, _ in rs if o is not None)}")


def main(argv):
    if not argv or argv[0] not in ("record", "pack", "marker"):
        print(__doc__)
        return 2
    out = argv[argv.index("--out") + 1] if "--out" in argv else "PACK.BIN"
    if argv[0] == "marker":
        at = int(argv[argv.index("--at") + 1], 0) if "--at" in argv else MARKER_AT
        size = (int(argv[argv.index("--size") + 1], 0)
                if "--size" in argv else MARKER_SIZE)
        data = marker_bytes(size)
        if not in_band(at, size):
            raise SystemExit(f"{at:#x}+{size} is outside the loader's bands")
        pack = build([(at, bytes(size), data)])
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        open(out, "wb").write(pack)
        print(f"marker pack: {size} B of a known pattern at {at:#x}, no mods, "
              f"no calls -> {out} ({len(pack)} bytes)")
        print("  write it, mount the stick, then read those bytes back over the "
              "cable: byte-exact = the copy-back cache path is right")
        return 0
    rest = [x for i, x in enumerate(argv[1:], 1)
            if x != "--out" and (i == 0 or argv[i - 1] != "--out")]
    if argv[0] == "pack":
        log = rest[0]
    else:
        log = os.path.join(tempfile.mkdtemp(), "writes.bin")
        env = dict(os.environ, MOCK_WRITE_LOG=log, XDJ_DECK_SOUND="0",
                   MOCK_CKPT=os.environ.get("MOCK_CKPT", os.path.join(
                       ROOT, "cache", "emu_dsp_mounted6.pkl")),
                   MOCK_RSLICE=os.environ.get("MOCK_RSLICE", "300000"))
        print("recording: launching the install runner on the emulated deck", flush=True)
        rc = subprocess.call(([sys.executable, "--run-tool"] if getattr(sys, "frozen", False) else [sys.executable]) + [os.path.join(ROOT, "tools", "mock",
                                                           "mock_install.py")] + rest,
                             cwd=ROOT, env=env)
        if rc:
            raise SystemExit(f"mock install failed ({rc}): no pack written")
    rs = runs(read_log(log))
    summary(rs)
    pack = build(rs)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "wb").write(pack)
    print(f"wrote {out} ({len(pack)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
