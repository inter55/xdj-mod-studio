#!/usr/bin/env python3
"""trackfilter.py -- on-deck TRACK FILTER (XDJ-1000MK2 style) for the XDJ-700.

    .venv/bin/python tools/trackfilter.py emu     # emulator test, user's library
    .venv/bin/python tools/trackfilter.py plan    # addresses

The module (tools/trackfilter/tf.c, built by tools/trackfilter/build.sh at
TF_BASE 0x09960000) wraps the DB task's browse-list copy FUN_08CDFC8C
(three literal-pool words, five call sites) and drops the track rows that
fail the filter, with the XDJ-1000MK2's own rules (measured on its
firmware in the reference emulator): BPM value +/- 0..6 %, RELATED KEY
(musical key names only), RATING exactly, COLOUR any of a bitmask. The settings are six UTILITY rows
(tools/utility/ut.c: TRACK FILTER, FILTER BPM, FILTER BPM RANGE, FILTER
KEY, FILTER COLOR, FILTER RATING). A changed filter shows the next time a
list is opened (BACK and push again), like Apply on the XDJ-1000MK2.
Full write-up: docs/TRACK_FILTER_1.1.md.

The emulator test opens UTILITY on the user's real library snapshot
(build/review_1110/tl_before.pkl = BROWSE category list, cursor on TRACK)
and, through the real rows, sets: ON / BPM 128 / +-2 % / KEY Am (related);
then BPM 124 +-0 % with KEY off; then COLOR PINK (nobody in this library:
empty list); then TRACK FILTER OFF. After each it opens TRACK and compares the
deck's rows with the export.pdb (the deck's own unfiltered order,
filtered in Python); the last leg must bring back all 263.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

POOLS = (0x08CE339C, 0x08CE42F8, 0x08CE45AC)   # all hold FUN_08CDFC8C
FLUSH_POOL = 0x08C92E20        # display flush (the screen draws here)
TOUCH_POOL = 0x08D5278C        # hit-tester (the header button + the screen)
TEXT_POOLS = (0x08C2F8FC, 0x08C31FC4)   # per-pixel text blender (tfentry.S)
TEXT_STOCK = 0x08C2F5C2
STOCK_COPY = 0x08CDFC8C
TF_LIMIT = 0x09960000
MAGIC = 0x544C4654
MAXPCT = 6                       # tf.c: +/- percent 0..6
LEFTBUF = 0x0B0E0FE8          # the browse list buffer seen in the emulator
BPM_LO = 60


def _out():
    for d in (os.path.join(ROOT, "build", "trackfilter"),
              os.path.join(ROOT, "prebuilt", "deck", "trackfilter")):
        if os.path.exists(os.path.join(d, "tf.bin")):
            return d
    return os.path.join(ROOT, "build", "trackfilter")


def build():
    """rebuild when the SH toolchain exists; else use the shipped bytes"""
    if os.path.exists(os.path.join(ROOT, "work", "sh-tools", "bin", "sh-elf-gcc")):
        subprocess.run(["sh", os.path.join(HERE, "trackfilter", "build.sh")],
                       check=True, capture_output=True)
    d = _out()
    img = open(os.path.join(d, "tf.bin"), "rb").read()
    syms = {}
    for ln in open(os.path.join(d, "tf.syms")):
        a, _, n = ln.split()
        syms[n.lstrip("_")] = int(a, 16)
    assert syms["end"] <= TF_LIMIT, hex(syms["end"])
    return img, syms


def install(mem):
    """emulator install (same order as tools/trackfilter_apply.py)"""
    img, s = build()
    base = s["tf_head"]
    for a in range(base, s["end"], 4):
        assert mem.r32(a) in (0,) or mem.r32(base) == MAGIC, f"cave not empty at {a:#x}"
    for i, x in enumerate(img):
        mem.w8(base + i, x)
    for a in range(base + len(img), s["end"]):
        mem.w8(a, 0)
    mem.w32(s["tf_prev"], STOCK_COPY)
    for p in POOLS:                       # pools LAST (the module is live then)
        assert mem.r32(p) in (STOCK_COPY, s["tf_copy"]), hex(p)
        mem.w32(p, s["tf_copy"])
    # the screen: chained in front of whatever owns the two pools now
    for pool, prev, ent in ((FLUSH_POOL, "tfs_prev_flush", "tfs_flush_entry"),
                            (TOUCH_POOL, "tfs_prev_touch", "tfs_touch_entry")):
        live = mem.r32(pool)
        if live != s[ent]:
            mem.w32(s[prev], live)
        mem.w32(pool, s[ent])
    live = {mem.r32(p) for p in TEXT_POOLS} - {s["tfs_txt_entry"]}
    assert len(live) <= 1, [hex(x) for x in live]
    if live:
        mem.w32(s["tfs_prev_txt"], live.pop())
    for p in TEXT_POOLS:
        mem.w32(p, s["tfs_txt_entry"])
    return s


# --- the filter in Python (the test's oracle) ------------------------------
CAM_MAJ = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]


def key_code(name):
    """tf.c key_code(): 1..12 = 1A..12A, 13..24 = 1B..12B, 0 = unknown
    (musical names only, like the XDJ-1000MK2)"""
    if not name:
        return 0
    if name[0].isdigit():                 # Camelot / Open Key: no match
        return 0
    pc = {"a": 9, "b": 11, "c": 0, "d": 2, "e": 4, "f": 5, "g": 7}.get(name[0].lower())
    if pc is None:
        return 0
    i = 1
    if name[1:2] == "#":
        pc, i = pc + 1, 2
    elif name[1:2] == "b":
        pc, i = pc - 1, 2
    pc %= 12
    minor = name[i:i + 1] in ("m", "M") and name[i + 1:i + 2].lower() != "a"
    return CAM_MAJ[(pc + 3) % 12] if minor else CAM_MAJ[pc] + 12


def library(pdb):
    sys.path.insert(0, os.path.join(ROOT, "vendor"))
    from rekordbox_pdb import Database
    db = Database(open(pdb, "rb").read())
    keys = {k.id: k.name for k in db.keys}
    return {t.id: (t.tempo, key_code(keys.get(t.key_id, "")), t.color_id, t.rating)
            for t in db.tracks}


def related(k, w):
    """RELATED KEY: same, relative, a fifth up/down (Camelot +-1)"""
    if not k:
        return False
    n, m = (k - 1) % 12, (w - 1) % 12
    if (k > 12) != (w > 12):
        return n == m
    return (n - m) % 12 in (0, 1, 11)


def matches(rec, bpm=0, rng=0, key=0, colour=0, rating=0):
    """the deck's own check (0x08B357CC), which tf.c calls; colour = bitmask
    (bit c-1). Reproduces every XDJ-1000MK2 count measured on its firmware."""
    tempo, k, col, rat = rec
    if bpm:
        p = min(rng, MAXPCT)
        lo, hi = bpm * (100 - p), bpm * (100 + p)
        if lo == hi:                     # +-0 %: the BPM it rounds to (the
            lo, hi = lo - 50, lo + 49    # deck's own check, 0x08B35F6C)
        if not lo <= tempo <= hi:
            return False
    if key and not related(k, key):
        return False
    if colour and not (1 <= col <= 8 and colour >> (col - 1) & 1):
        return False
    if rating:
        r = rat // 51 if rat > 5 else rat
        if r != rating:
            return False
    return True


TF_ST = 0x09603674            # the deck's filter-condition store (tf.c)


def nat_get(mem, k):
    """tf.c tf_get() for k 1..5, read from the deck's condition store"""
    ent = lambda j: TF_ST + 8 + 0x1C * j
    def val(j, i):
        n, p = mem.r16(ent(j) + 0xE), mem.r32(ent(j) + 0x10)
        return mem.r32(p + 4 * i) if i < n and 0x08000000 <= p < 0x10000000 else 0
    tick = lambda j: mem.r32(ent(j) + 4) == 1
    if k in (1, 2):
        lo, hi = val(0, 0), val(0, 1)
        if not lo or hi < lo:
            return 0 if k == 1 else 6
        if k == 2:
            return (hi - lo) * 100 // (lo + hi)
        return (lo + hi) // 200 if tick(0) else 0
    if k == 3:
        e = val(1, 0)
        return ((e + 1) // 2 if e & 1 else e // 2 + 12) if tick(1) and 1 <= e <= 24 else 0
    if k == 4:
        return val(3, 0) & 0xFF if tick(3) else 0
    return val(2, 0) if tick(2) else 0


def rows(mem, buf=LEFTBUF):
    n, p = mem.r16(buf + 2), mem.r32(buf + 0x4C)
    return [mem.r32(p + 20 * i) for i in range(n)], mem.r16(buf + 4)


def emu():
    import utility as U
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    out = os.path.join(ROOT, "screenshots", "trackfilter")
    os.makedirs(out, exist_ok=True)
    stick = os.path.expanduser(os.environ.get("STICK", "~/Downloads/Rekordbox stuff"))
    lib = library(os.path.join(stick, "PIONEER", "rekordbox", "export.pdb"))
    # the deck's own unfiltered TRACK order (same library, same snapshot lineage)
    ref = Machine.load(os.path.join(ROOT, "build", "review_1110", "tl_tracklist.pkl"))
    order, _ = rows(ref.mem)
    del ref
    print("unfiltered TRACK list:", len(order), "rows")
    m = Machine.load(os.environ.get("SNAP", os.path.join(ROOT, "build", "review_1110",
                                                          "tl_before.pkl")))
    mem, dev, c = m.mem, m.mem.dev, m.cpu
    s = install(mem)
    us = U.install(mem)
    cfg = s["tf_cfg"]
    ok = True

    def chk(label, good):
        print(("ok   " if good else "FAIL ") + label)
        return bool(good)

    def run(n, keys=()):
        dev.panel_frame = panl.build_frame(keys=list(keys))
        c.run(limit=n)

    def key(k, hold=12_000_000, after=25_000_000):
        run(hold, [k])
        run(after)

    def turn(n):
        for _ in range(abs(n)):
            mem.w16(panl.ROTARY_COUNTER, (mem.r16(panl.ROTARY_COUNTER) + (1 if n > 0 else -1)) & 0xFFFF)
            run(8_000_000)
        run(20_000_000)

    def utility_open():
        run(200_000_000, ["MenuUtility"])   # long hold = UTILITY
        run(60_000_000)

    def edit(steps):
        key("RotaryPush")
        turn(steps)
        key("RotaryPush")

    def leave_utility(tag):
        key("Browse", hold=4_000_000, after=80_000_000)   # short tap: back to BROWSE
        fb_png(m, f"{out}/{tag}_browse.png")

    def open_track():
        n0 = mem.r32(s["tf_calls"])
        key("RotaryPush", after=200_000_000)
        return mem.r32(s["tf_calls"]) - n0


    def idx(i, v):                          # cfg byte -> row value index
        if i == 1:
            return v - BPM_LO + 1 if v else 0
        if i == 4:                           # colour bitmask -> one colour
            return v.bit_length() if v else 0
        return v

    def cur(i):
        return mem.r8(cfg) if i == 0 else nat_get(mem, i)

    def set_rows(tag, on, bpm=0, rng=None, key=0, colour=0, rating=0):
        """open UTILITY (it opens at the top), edit the six rows, leave"""
        if rng is None:
            rng = cur(2)                     # the range is kept while BPM is off
        want = [on, bpm, rng, key, colour, rating]
        utility_open()
        turn(U.STOCK_N)                      # first of our rows: TRACK FILTER
        for i, v in enumerate(want):
            d = idx(i, v) - idx(i, cur(i))
            if i:
                turn(1)
            if d:
                edit(d)
        fb_png(m, f"{out}/{tag}_rows.png")
        got = [cur(i) for i in range(6)]
        print(tag, "settings written by the rows:", got)
        leave_utility(tag)
        return got == want

    def in_track_list():
        """the shown list holds track rows (kind byte +0x12, tf.c is_track)
        -- an empty filtered list counts as one too (title says TRACK)"""
        n, p = mem.r16(LEFTBUF + 2), mem.r32(LEFTBUF + 0x4C)
        if n == 0:
            return True
        k = mem.r8(p + 0x12)
        return 1 <= k <= 5 or 0x16 <= k <= 0x18

    def visit(tag):
        """BACK to the categories (if inside a list), push TRACK, read it"""
        if in_track_list():
            key("Back", after=60_000_000)
        calls = open_track()
        fb_png(m, f"{out}/{tag}_track.png")
        ids, total = rows(mem)
        print(f"{tag}: TRACK list {len(ids)} rows, copies {calls}, "
              f"lookup fails {mem.r32(s['tf_fail'])}, saves skipped {mem.r32(s['tf_skips'])}")
        return ids, total

    exc0 = m.exc_total
    run(20_000_000)
    utility_open()
    fb_png(m, f"{out}/a_utility.png")
    leave_utility("a")
    legs = [("b", dict(on=1, bpm=128, rng=2, key=8)),        # 128 +-2, Am (8A)
            ("c", dict(on=1, bpm=124, rng=0, key=0)),        # changed inside the list
            ("d", dict(on=1, bpm=0, rng=0, key=0, colour=1)),  # PINK (bit 0): nobody
            ("e", dict(on=0, colour=1))]                     # switch off -> all back
    for tag, w in legs:
        ok &= chk(f"{tag}: UTILITY rows set {w}", set_rows(tag, **w))
        ids, total = visit(tag)
        f = {k: v for k, v in w.items() if k != "on"} if w["on"] else {}
        exp = [i for i in order if matches(lib[i], **f)]
        ok &= chk(f"{tag}: list = export.pdb filter in deck order ({len(exp)})",
                  ids == exp and total == len(exp))
        if tag == "b":
            ok &= chk("b: every row BPM 125.44..130.56 and key related to Am",
                      len(ids) > 0 and all(12544 <= lib[i][0] <= 13056 and related(lib[i][1], 8)
                                           for i in ids))
            turn(len(ids) + 3)               # scroll to the end: no junk rows
            fb_png(m, f"{out}/b_track_end.png")
    ok &= chk("e: all 263 back", len(ids) == len(order) == 263)
    # the deck saved the conditions on the stick (its own RBFLTR.DAT writer)
    run(1_500_000_000)
    disk = m.mem.dev.usb.stick.disk
    f = [v for v in disk.written.values() if v[:4] == b"FMAI"]
    blk = f[-1][0x74:0xE8] if f else b""
    ok &= chk("RBFLTR.DAT rewritten on the stick with the last conditions "
              "(COLOR ticked, PINK)", b"FCND" in blk and
              blk.find(bytes.fromhex("0000000f01110001")) > 0 and blk.endswith(b"\0\0\0\x01"))
    ok &= chk("no CPU exceptions", m.exc_total == exc0)
    if os.environ.get("SAVE"):
        m.save(os.environ["SAVE"])
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["plan"]:
        img, s = build()
        print(f"module {s['tf_head']:#x}..{s['end']:#x} ({len(img)} B code), "
              f"cfg {s['tf_cfg']:#x}; pools {[hex(p) for p in POOLS]} "
              f"{STOCK_COPY:#x} -> {s['tf_copy']:#x}")
        sys.exit(0)
    sys.exit(emu())
