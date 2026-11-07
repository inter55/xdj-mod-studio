#!/usr/bin/env python3
"""anlz_cues.py -- read and WRITE the hot cues (A-H) in a rekordbox
analysis file, in the layout the XDJ-700 itself parses (proven: a stick
written this way loaded all eight cues, tools/hotcue8_emu.py).

    .venv/bin/python tools/anlz_cues.py show  ANLZ0000.DAT [ANLZ0000.EXT]
    .venv/bin/python tools/anlz_cues.py set   4=61000,5=62000 ANLZ0000.DAT [...]
    .venv/bin/python tools/anlz_cues.py clear 4,5 ANLZ0000.DAT [...]

Only the PCOB (type 1) hot-cue section in a .DAT/.EXT is rewritten; every
other section is copied byte for byte and the file length is fixed up.
Cue numbers 1..8 = A..H. Times are milliseconds.
"""
import struct
import sys

TAGS = (b"PCOB", b"PCO2")


def sections(b):
    """(tag, bytes, type) for each section after the 8/16-byte header"""
    hl = struct.unpack(">I", b[4:8])[0]
    out, p = [], hl
    while p + 12 <= len(b):
        tag = bytes(b[p:p + 4])
        ln = struct.unpack(">I", b[p + 8:p + 12])[0]
        if ln < 12 or p + ln > len(b):
            break
        typ = struct.unpack(">I", b[p + 12:p + 16])[0] if tag in TAGS else None
        out.append((tag, bytes(b[p:p + ln]), typ))
        p += ln
    return b[:hl], out


def read_cues(b):
    """{cue number 1..8: time ms} from the PCOB (type 1) section"""
    _, secs = sections(b)
    cues = {}
    for tag, sec, typ in secs:
        if tag != b"PCOB" or typ != 1:
            continue
        q, end = 16, len(sec)
        while q + 16 <= end:
            if sec[q:q + 4] == b"PCPT":
                step = struct.unpack(">I", sec[q + 8:q + 12])[0] or 0x38
                num = struct.unpack(">H", sec[q + 14:q + 16])[0]
                if 0x41 <= num <= 0x48:
                    num -= 0x40
                ms = struct.unpack(">I", sec[q + 0x20:q + 0x24])[0]
                if 1 <= num <= 8:
                    cues[num] = ms
                q += step
            else:
                q += 8
    return cues


def pcpt(n, ms):
    """one hot-cue entry, in the layout the deck accepts (0x38 bytes)"""
    return (b"PCPT" + struct.pack(">II", 0x1C, 0x38) +
            struct.pack(">IIIHH", n, 4, 0x10000, 0xFFFF, 0xFFFF) +
            struct.pack(">BBHII", 1, 0, 0x3E8, ms, 0xFFFFFFFF) + bytes(16))


def pcp2(n, ms):
    body = (struct.pack(">I", n) + struct.pack(">B3x", 1) +
            struct.pack(">II", ms, 0xFFFFFFFF) + struct.pack(">B7x", n) +
            struct.pack(">HHI", 0, 0, 0) + bytes([n, 0xFF, 0x40, 0x40]))
    return (b"PCP2" + struct.pack(">II", 0x10, 0x58) + body).ljust(0x58, b"\0")


def write_cues(path, cues):
    """cues = {1..8: ms}; rewrites PCOB+PCO2 (type 1); returns {n: ms} written"""
    b = open(path, "rb").read()
    head, secs = sections(b)
    have = read_cues(b)
    have.update({int(k): int(v) for k, v in cues.items() if v is not None})
    out = bytearray(head)
    touched = 0
    for tag, sec, typ in secs:
        if tag in TAGS and typ == 1:
            if tag == b"PCOB":
                ents = b"".join(pcpt(n, have[n]) for n in sorted(have))
                sec = (b"PCOB" + struct.pack(">II", 0x18, 0x18 + len(ents)) +
                       struct.pack(">IHHI", 1, 0, len(have), 0xFFFFFFFF) + ents)
            else:
                ents = b"".join(pcp2(n, have[n]) for n in sorted(have))
                sec = (b"PCO2" + struct.pack(">II", 0x14, 0x14 + len(ents)) +
                       struct.pack(">IHH", 1, len(have), 0) + ents)
            touched += 1
        out += sec
    if not touched and have:
        # no hot-cue section in this file at all: append one, so a track
        # that never had cues can still carry A-H (200 of 263 library
        # tracks have an empty PCOB, but older exports may have none)
        ext = path.lower().endswith(".ext")
        tag = b"PCO2" if ext else b"PCOB"
        out += (b"PCO2" + struct.pack(">II", 0x14, 0x14 + 0x58 * len(have)) +
                struct.pack(">IHH", 1, len(have), 0) +
                b"".join(pcp2(n, have[n]) for n in sorted(have))) if ext else (
               b"PCOB" + struct.pack(">II", 0x18, 0x18 + 0x38 * len(have)) +
               struct.pack(">IHHI", 1, 0, len(have), 0xFFFFFFFF) +
               b"".join(pcpt(n, have[n]) for n in sorted(have)))
        touched = 1
    if not touched:
        return {}
    struct.pack_into(">I", out, 8, len(out))
    open(path, "wb").write(out)
    return have


def _cue_section(tag, cues):
    """The PCOB (DAT) / PCO2 (EXT) section holding {n: ms}."""
    if tag == b"PCOB":
        ents = b"".join(pcpt(n, cues[n]) for n in sorted(cues))
        return (b"PCOB" + struct.pack(">II", 0x18, 0x18 + len(ents)) +
                struct.pack(">IHHI", 1, 0, len(cues), 0xFFFFFFFF) + ents)
    ents = b"".join(pcp2(n, cues[n]) for n in sorted(cues))
    return (b"PCO2" + struct.pack(">II", 0x14, 0x14 + len(ents)) +
            struct.pack(">IHH", 1, len(cues), 0) + ents)


def clear_cues(path, nums):
    """Remove cues 1..8 (A..H) from the file; returns {n: ms} that remain.
    Every other section is copied byte for byte, like write_cues."""
    b = open(path, "rb").read()
    head, secs = sections(b)
    drop = {int(n) for n in nums}
    keep = {n: ms for n, ms in read_cues(b).items() if n not in drop}
    out = bytearray(head)
    touched = 0
    for tag, sec, typ in secs:
        if tag in TAGS and typ == 1:
            out += _cue_section(tag, keep)
            touched += 1
        else:
            out += sec
    if not touched:
        return keep
    struct.pack_into(">I", out, 8, len(out))
    open(path, "wb").write(out)
    return keep


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd = argv[1]
    spec = argv[2] if cmd in ("set", "clear") else None
    files = argv[3:] if spec is not None else argv[2:]
    if cmd == "show":
        for f in argv[2:]:
            print(f, read_cues(open(f, "rb").read()))
        return 0
    if cmd == "set":
        cues = {}
        for pair in spec.split(","):
            n, _, ms = pair.partition("=")
            cues[int(n)] = int(ms)
        for f in files:
            print(f, "->", write_cues(f, cues))
        return 0
    if cmd == "clear":
        for f in files:
            print(f, "->", clear_cues(f, [int(n) for n in spec.split(",")]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
