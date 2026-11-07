#!/usr/bin/env python3
"""anlz_cues_test.py -- hot cues A-H: read, write, delete, and the Studio
editor that drives them.

File-format check only (no emulator, no deck):
  1. cues survive a write and come back with the same numbers and times;
  2. deleting some cues keeps the rest, on the right numbers;
  3. every non-cue section is byte-for-byte identical afterwards (the beat
     grid and waveform live in those sections);
  4. the file's length word matches the file;
  5. Studio's cues_read / cues_edit do the same and refuse a track outside
     the library, a folder with no analysis file, cue numbers above H and
     negative times.
"""
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]
import anlz_cues as AC                          # noqa: E402

FAILS = []


def ck(name, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(name)


def section(tag, payload):
    # a section is tag(4) + size word(4) + LENGTH(4) + body, and the length
    # counts the whole section: 12 + len(body)
    return tag + struct.pack(">II", 0x10, 12 + len(payload)) + payload


PQTZ = section(b"PQTZ", bytes(range(16)))        # stands in for the beat grid
PWV3 = section(b"PWV3", bytes(range(32)))        # ... and the waveform


def make(path, cues):
    """A tiny ANLZ file: header, one PCOB, a beat-grid and a waveform section."""
    head = b"PMAI" + struct.pack(">I", 16) + bytes(8)
    b = bytearray(head + AC._cue_section(b"PCOB", cues) + PQTZ + PWV3)
    struct.pack_into(">I", b, 8, len(b))
    open(path, "wb").write(b)
    return path


def find(b, tag):
    p = struct.unpack(">I", b[4:8])[0]
    while p + 12 <= len(b):
        ln = struct.unpack(">I", b[p + 8:p + 12])[0]
        if b[p:p + 4] == tag:
            return bytes(b[p:p + ln])
        if ln < 12:
            break
        p += ln
    return b""


def file_checks(tmp):
    p = make(os.path.join(tmp, "ANLZ0000.DAT"), {1: 5000, 2: 13000})
    ck("reads the cues that were written", AC.read_cues(open(p, "rb").read()) ==
       {1: 5000, 2: 13000})

    AC.write_cues(p, {3: 21000, 4: 29000, 5: 37000, 6: 45000, 7: 53000, 8: 61000})
    b = open(p, "rb").read()
    want = {1: 5000, 2: 13000, 3: 21000, 4: 29000, 5: 37000,
            6: 45000, 7: 53000, 8: 61000}
    ck("all eight cues (A-H) read back", AC.read_cues(b) == want,
       str(AC.read_cues(b)))
    ck("the beat grid is untouched", find(b, b"PQTZ") == PQTZ)
    ck("the waveform is untouched", find(b, b"PWV3") == PWV3)
    ck("the length word matches the file",
       struct.unpack(">I", b[8:12])[0] == len(b))

    keep = AC.clear_cues(p, [1, 2, 5])
    b = open(p, "rb").read()
    ck("delete leaves the other cues", keep == {3: 21000, 4: 29000, 6: 45000,
                                                7: 53000, 8: 61000}, str(keep))
    ck("the file agrees with the returned list", AC.read_cues(b) == keep)
    ck("deleting keeps the beat grid", find(b, b"PQTZ") == PQTZ)
    ck("deleting keeps the waveform", find(b, b"PWV3") == PWV3)
    ck("the length word still matches",
       struct.unpack(">I", b[8:12])[0] == len(b))

    # a file that never had a hot-cue section gets one, rather than failing
    p2 = os.path.join(tmp, "ANLZ0001.DAT")
    head = b"PMAI" + struct.pack(">I", 16) + bytes(8)
    bare = bytearray(head + PQTZ)
    struct.pack_into(">I", bare, 8, len(bare))
    open(p2, "wb").write(bare)
    AC.write_cues(p2, {8: 61000})
    ck("a track with no cue section can gain one",
       AC.read_cues(open(p2, "rb").read()) == {8: 61000})


def studio_checks(tmp):
    import studio.app as SA
    stick = os.path.join(tmp, "STICK")
    d = os.path.join(stick, "PIONEER", "USBANLZ", "P001", "0001")
    os.makedirs(d)
    audio = os.path.join(d, "Artist - Track.mp3")
    open(audio, "wb").write(bytes(64))
    dat = make(os.path.join(d, "ANLZ0000.DAT"), {1: 5000, 2: 13000})

    r = SA.cues_read(stick, audio)
    ck("Studio reads the cues on the stick",
       r["cues"] == {"1": 5000, "2": 13000}, str(r["cues"]))

    r = SA.cues_edit(stick, audio, {3: 21000, 4: 29000})
    ck("Studio adds C and D", r["cues"].get("3") == 21000 and
       r["cues"].get("4") == 29000, str(r["cues"]))

    r = SA.cues_edit(stick, audio, clear=[1, 2])
    ck("Studio deletes A and B", sorted(r["cues"]) == ["3", "4"], str(r["cues"]))
    ck("the count the track list shows agrees",
       SA.cues_of(open(dat, "rb").read()) == 2)

    r = SA.cues_edit(stick, audio, {8: 61000})
    ck("a cue set by the app survives the deck-facing count",
       SA.cues_of(open(dat, "rb").read()) == 3)

    bad = [("a track outside the library is refused",
            lambda: SA.cues_read("/tmp/definitely-not-the-stick", audio)),
           ("a folder with no analysis file is refused",
            lambda: SA.cues_read(stick, os.path.join(d, "nothing", "x.mp3"))),
           ("cue numbers above H are refused",
            lambda: SA.cues_edit(stick, audio, {9: 1000})),
           ("negative times are refused",
            lambda: SA.cues_edit(stick, audio, {1: -1}))]
    for label, fn in bad:
        try:
            fn()
            ck(label, False, "no error raised")
        except ValueError:
            ck(label, True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        file_checks(tmp)
        studio_checks(tmp)
    print("RESULT: PASS" if not FAILS else f"RESULT: FAIL ({len(FAILS)})")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
