#!/usr/bin/env python3
"""mockdeck_server.py CKPT SOCK -- a pretend XDJ-700 service connection.

Holds an emulator Machine (loaded from CKPT) and answers the deck's USB
shell commands on a unix socket, exactly as the installers send them:
  $CDJ            -> "Welcome"
  ?V              -> version line
  ADDR,N-1BR      -> N (<=32) byte values "XX " + CRLF, in 16-B reports
  ADDR,VALBW/WW/LW -> write 1/2/4 bytes (WW masks -2, LW masks -4) + CRLF
Only the RAM window 0x08000000-0x0FFFFFFF is served (like our tools).
The emulated CPU runs a short slice per command so hooks and caves live.
Control lines: "#SAVE path" saves the machine, "#QUIT" stops.
"""
import os
import struct
import socket
import sys

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", ".."),
                os.path.join(os.path.dirname(__file__), "..")]
from emu.machine import Machine          # noqa: E402

RAM_LO, RAM_HI = 0x08000000, 0x10000000
SLICE = int(os.environ.get("MOCK_SLICE", "0"))           # per write
RSLICE = int(os.environ.get("MOCK_RSLICE", "0"))         # per read (deck keeps
                                                          # running while a tool polls)
SESSION_RUN = int(os.environ.get("MOCK_SESSION_RUN", "20000000"))  # per open


def reports(text):
    b = text.encode()
    out = bytearray()
    for i in range(0, len(b), 16):
        out += b[i:i + 16].ljust(16, b"\0")
    return bytes(out)


WLOG = (open(os.environ["MOCK_WRITE_LOG"], "ab")
        if os.environ.get("MOCK_WRITE_LOG") else None)


def handle(m, line):
    s = line.strip().rstrip("\0")
    if s == "$CDJ":
        return reports("Welcome\r\n")
    if s == "?V":
        return reports("XDJ-700 Ver1.15 (mock)\r\n")
    for op, n, mask in (("BR", 0, 0), ("LR", 0, 3), ("BW", 1, 0), ("WW", 2, 1), ("LW", 4, 3)):
        if s.upper().endswith(op):
            a, v = s[:-2].split(",")
            addr, val = int(a, 16), int(v, 16)
            if not (RAM_LO <= addr < RAM_HI):
                return reports("E05\r\n")
            if op in ("LR", "BR") and RSLICE:
                m.cpu.run(limit=RSLICE)
            if op == "LR":         # 32-bit values, 8 hex digits each
                cnt = (val & 31) + 1
                addr &= ~3
                vals = " ".join(f"{m.mem.r32(addr + 4 * i):08X}" for i in range(cnt))
                return reports(vals + " \r\n")
            if op == "BR":
                cnt = (val & 31) + 1
                vals = " ".join(f"{m.mem.r8(addr + i):02X}" for i in range(cnt))
                return reports(vals + " \r\n")
            addr &= ~mask
            if WLOG:                    # stick packs replay this (tools/stick_pack.py)
                old = bytes(m.mem.r8(addr + i) for i in range(n))
                WLOG.write(struct.pack("<IB", addr, n) + old +
                           bytes((val >> (8 * i)) & 0xFF for i in range(n)))
                WLOG.flush()
            for i in range(n):
                m.mem.w8(addr + i, (val >> (8 * i)) & 0xFF)
            if SLICE:
                m.cpu.run(limit=SLICE)
            return reports("\r\n")
    return reports("E04\r\n")


def main():
    ckpt, sock = sys.argv[1], sys.argv[2]
    m = Machine.load(ckpt)
    if os.path.exists(sock):
        os.unlink(sock)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock)
    srv.listen(4)
    print("mock deck ready", flush=True)
    # real-deck time model (measured 2026-10-04/05): BR round ~66 ms,
    # write ~4 ms with reply wait, open+handshake ~2 s, 30 s cool-down
    stats = {"cmds": 0, "sessions": 0, "br": 0, "wr": 0, "est": 0.0}
    while True:
        c, _ = srv.accept()
        sb, sw = stats["br"], stats["wr"]
        stats["sessions"] += 1
        m.cpu.run(limit=SESSION_RUN)       # the deck keeps running between sessions
        stats["est"] += 2.0 + (float(os.environ.get("MOCK_PACE", "30"))
                               if stats["sessions"] > 1 else 0)
        f = c.makefile("rwb")
        for raw in f:
            line = raw.decode("ascii", "replace")
            if line.startswith("#SAVE "):
                m.save(line[6:].strip())
                f.write(b"saved\n"); f.flush()
                continue
            if line.startswith("#STATS"):
                f.write((f"sessions {stats['sessions']} cmds {stats['cmds']} "
                         f"reads {stats['br']} writes {stats['wr']} "
                         f"est_minutes {stats['est'] / 60:.1f}\n").encode()); f.flush()
                continue
            if line.startswith("#QUIT"):
                f.write(b"bye\n"); f.flush()
                return
            stats["cmds"] += 1
            if line.strip().upper().endswith(("BR", "LR")):
                stats["br"] += 1
                stats["est"] += 0.066
            else:
                stats["wr"] += 1
                stats["est"] += 0.004
            out = handle(m, line)
            f.write(out.hex().encode() + b"\n")
            f.flush()
        c.close()
        if os.environ.get("MOCK_SESSION_LOG"):
            with open(os.environ["MOCK_SESSION_LOG"], "a") as lf:
                lf.write(f"{stats['sessions']} reads {stats['br'] - sb} writes {stats['wr'] - sw}\n")


if __name__ == "__main__":
    main()
