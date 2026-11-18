#!/usr/bin/env python3
"""linkrgb_test.py [STICK_ROOT] -- run the linkrgb machine code (tools/
linkrgb.py) on the project's SH-4 interpreter, firmware 1.15 mapped at
0x08000000 so the analysis hash really is the firmware's own 0x08D08878.

The deck's file API and the NFS client are Python stubs (native_hook):
  fopen/fread/fseek/fclose  serve files from a fake NFS mount, drive 'N:',
                            rooted at a SYNTHETIC stick built here (and at
                            STICK_ROOT too when given -- e.g. a real
                            rekordbox export, read-only);
  NFS open / NFS fread      the stock functions our hooks chain to: they
                            record their arguments and return.
Checks: hook pass-through (args, callee-saved regs, stack), the audio /
PIONEER filter, the log ring, the fetch (slot bytes == the file's
sections), every failure path, the snapshot decision vs the stock
snapshot, and the analysis path for real tracks.

Firmware: $XDJ_BLK1, else %LOCALAPPDATA%/XDJ Mod Studio/firmware/blk1.bin,
else cache/blk1.bin.
"""
import os
import random
import struct
import sys
import tempfile

sys.path[:0] = [".", "tools"]
from emu.sh4 import SH4, PcHalt, MD, BL
import linkrgb as L
import rgb_overview as RO
import rgb_detail as RD

MAGIC = 0x0F000000          # return address of a test call: stop there
STACK = 0x0E800000
FAILS = []


def chk(name, ok, extra=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' -- ' + extra) if extra and not ok else ''}")
    if not ok:
        FAILS.append(name)


def blk1_path():
    for p in (os.environ.get("XDJ_BLK1"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "XDJ Mod Studio",
                           "firmware", "blk1.bin"),
              "cache/blk1.bin"):
        if p and os.path.isfile(p):
            return p
    sys.exit("no firmware image: set XDJ_BLK1 to blk1.bin (fw 1.15)")


class Mem:
    """blk1 at 0x08000000 (writable copy) + sparse 4 KB pages elsewhere."""
    def __init__(self, img):
        self.img = bytearray(img)
        self.pages = {}
        self.tick = None

    def _loc(self, a):
        a &= 0x1FFFFFFF
        if 0x08000000 <= a < 0x08000000 + len(self.img):
            return self.img, a - 0x08000000
        if a < 0x01000000:
            raise IndexError(f"access to {a:#x} (null page / vector)")
        pg = self.pages.get(a >> 12)
        if pg is None:
            pg = self.pages[a >> 12] = bytearray(4096)
        return pg, a & 0xFFF

    def r8(self, a):
        b, o = self._loc(a)
        return b[o]

    def w8(self, a, v):
        b, o = self._loc(a)
        b[o] = v & 0xFF

    def r16(self, a):
        return self.r8(a) | (self.r8(a + 1) << 8)

    def r32(self, a):
        return self.r16(a) | (self.r16(a + 2) << 16)

    def r64(self, a):
        return self.r32(a) | (self.r32(a + 4) << 32)

    def w16(self, a, v):
        self.w8(a, v)
        self.w8(a + 1, v >> 8)

    def w32(self, a, v):
        self.w16(a, v)
        self.w16(a + 2, v >> 16)

    def w64(self, a, v):
        self.w32(a, v)
        self.w32(a + 4, v >> 32)

    def wbytes(self, a, b):
        for i, x in enumerate(b):
            self.w8(a + i, x)

    def rbytes(self, a, n):
        return bytes(self.r8(a + i) for i in range(n))

    def wstr(self, a):
        out = []
        while True:
            c = self.r16(a)
            if c == 0:
                return "".join(out)
            out.append(chr(c))
            a += 2

    def take_interrupt(self, imask):
        return None


class Deck:
    """CPU + memory + module installed + firmware stubs."""
    def __init__(self, img, roots):
        self.mem = Mem(img)
        self.roots = roots                 # drive letter -> host folder
        self.files = {}                    # handle -> [host file, pos]
        self.next_h = 0x0D000000
        self.calls = []                    # (name, args...)
        self.on_fread = None
        code, self.ent = L.build()
        self.mem.wbytes(L.LBASE, code)
        for a, b in L.data_init():
            self.mem.wbytes(a, b)
        self.mem.w32(L.SYS_MS, 100000)
        c = self.cpu = SH4(self.mem)
        from emu.sh4fpu import FPU
        c.fpu = FPU(c)
        c.sr = MD | BL | 0xF0
        c.native_hook = self.hook
        self.stubs = {L.FW_FOPEN: self.fopen, L.FW_FREAD: self.fread,
                      L.FW_FSEEK: self.fseek, L.FW_FCLOSE: self.fclose,
                      L.ORIG_OPEN: self.nfs_open, L.ORIG_READ: self.nfs_read,
                      RD.DCAVE: self.dcave}

    # ---- calling ----------------------------------------------------------
    def call(self, addr, regs=None, limit=20_000_000):
        c = self.cpu
        for i in range(16):
            c.set_gr(i, 0x11110000 + i)    # recognisable junk
        c.set_gr(15, getattr(self, "stack", STACK))
        for r, v in (regs or {}).items():
            c.set_gr(r, v)
        c.macl, c.mach = 0x4D41434C, 0x4D414348
        c.pr = MAGIC
        c.pc = addr
        self.before = [c.gr(i) for i in range(16)]

        def tr(pc, op):
            if pc == MAGIC:
                raise PcHalt(pc)
        c.trace = tr
        res = c.run(limit=limit)
        c.trace = None
        assert "halt" in res, res
        return c.gr(0)

    def hook(self, c, pc):
        f = self.stubs.get(pc)
        if f is None:
            return False
        c.set_gr(0, f(c) & 0xFFFFFFFF)
        c.pc = c.pr
        return True

    # ---- firmware stubs -----------------------------------------------------
    def host(self, wpath):
        if len(wpath) < 3 or wpath[1] != ":":
            return None
        root = self.roots.get(wpath[0])
        if root is None:
            return None
        return os.path.join(root, *wpath[3:].split("/"))

    def fopen(self, c):
        p = self.mem.wstr(c.gr(4))
        mode = self.mem.rbytes(c.gr(5), 2)
        self.calls.append(("fopen", p, mode, c.gr(6)))
        h = self.host(p)
        if h is None or not os.path.isfile(h):
            return 0
        self.next_h += 0x100
        self.files[self.next_h] = [open(h, "rb").read(), 0]
        return self.next_h

    def fread(self, c):
        buf, size, count, h = c.gr(4), c.gr(5), c.gr(6), c.gr(7)
        req = self.mem.r32(c.gr(15))
        self.calls.append(("fread", buf, size, count, h, req))
        if self.on_fread:
            self.on_fread(self)
        f = self.files[h]
        data = f[0][f[1]:f[1] + size * count]
        f[1] += len(data)
        self.mem.wbytes(buf, data)
        return len(data) // size

    def fseek(self, c):
        h, off, wh, req = c.gr(4), c.gr(5), c.gr(6), c.gr(7)
        self.calls.append(("fseek", h, off, wh, req))
        f = self.files[h]
        if wh != 0 or off > len(f[0]):
            return 0xFFFFFFFF
        f[1] = off
        return 0

    def fclose(self, c):
        self.calls.append(("fclose", c.gr(4)))
        self.files.pop(c.gr(4), None)
        return 0

    def nfs_open(self, c):
        self.calls.append(("nfs_open", [c.gr(i) for i in range(16)],
                           c.macl, c.mach, c.pr))
        return 0x0CAFE000

    def dcave(self, c):
        self.calls.append(("dcave", [c.gr(i) for i in range(16)],
                           c.macl, c.mach, c.pr))
        return c.gr(0)

    def nfs_read(self, c):
        self.calls.append(("nfs_read", [c.gr(i) for i in range(16)],
                           c.macl, c.mach, c.pr))
        return 0x1234

    # ---- helpers ------------------------------------------------------------
    def v(self, k):
        return self.mem.r32(L.V[k])

    def put_wstr(self, addr, s):
        self.mem.wbytes(addr, (s + "\0").encode("utf-16-le"))

    def open_track(self, wpath, mode=b"r\0"):
        """What generic fopen does for a type-5 file: call the hooked pool."""
        if getattr(self, "auto_ms", 0):      # opens spaced like real loads
            self.mem.w32(L.SYS_MS, self.mem.r32(L.SYS_MS) + self.auto_ms)
        self.put_wstr(0x0E100000, wpath)
        self.mem.wbytes(0x0E101000, mode)
        self.calls.clear()
        return self.call(self.ent["lopen"], {4: 0x0E100000, 5: 2 * len(wpath),
                                             6: 0x0E101000, 7: 0x0E102000})

    def load(self, wpath, gap=1500):
        """What the deck does on a Link load: open, re-open `gap` ms later."""
        self.open_track(wpath)
        self.mem.w32(L.SYS_MS, self.mem.r32(L.SYS_MS) + gap)
        return self.open_track(wpath)

    def col(self):
        """The centre painter's per-column colour call (hooked pool)."""
        self.calls.clear()
        return self.call(self.ent["lcol"], {2: 5, 8: 0x1238})

    def set_len(self, blob):
        self.mem.w32(L.TRACK_LEN, ent5(blob))

    def media_read(self, site=0):
        """An NFS fread through read hook `site` (0 = the media server)."""
        self.calls.clear()
        return self.call(self.ent[f"lrs{site}"], {4: 0x0E200000, 5: 0x4000,
                                                  6: 0x0CAFE000})


# ---- synthetic analysis files ---------------------------------------------
def section(tag, body, hlen=24):
    head = tag + struct.pack(">II", hlen, hlen + len(body))
    head += bytes(hlen - 12)
    return head + body


def make_ext(rng, n5=9000, with6=True):
    secs = [section(b"PPTH", "/x".encode("utf-16-be"), 16),
            section(b"PQT2", bytes(rng.randrange(256) for _ in range(300)), 56),
            section(b"PWV3", bytes(rng.randrange(256) for _ in range(n5)), 24),
            section(b"PWV4", bytes(rng.randrange(256) for _ in range(7200)), 24),
            section(b"PWV5", bytes(rng.randrange(256) for _ in range(2 * n5)), 24)]
    if with6:
        secs.append(section(b"PWV6", bytes(rng.randrange(256) for _ in range(3600)), 20))
    body = b"".join(secs)
    return b"PMAI" + struct.pack(">II", 28, 28 + len(body)) + bytes(16) + body


def sections(blob):
    out = {}
    off = struct.unpack(">I", blob[4:8])[0]
    while off + 12 <= len(blob):
        tag = blob[off:off + 4]
        tl = struct.unpack(">I", blob[off + 8:off + 12])[0]
        if tl < 12:
            break
        out[tag] = blob[off:off + tl]
        off += tl
    return out


def put_track(root, audio_path, ext_blob):
    rel = L.ext_path("N", audio_path)[3:]
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(ext_blob)
    return p


def ent5(blob):
    sec = sections(blob)[b"PWV5"]
    hl, tl = struct.unpack(">II", sec[4:12])
    return (tl - hl) // 2


def put_slots(d, blob):
    for tag, a in ((b"PWV4", RO.SLOT4), (b"PWV5", RO.SLOT5), (b"PWV6", RO.SLOT6)):
        sec = sections(blob).get(tag)
        if sec:
            d.mem.wbytes(a, sec)


def slot_ok(d, slot, tag, blob):
    s = sections(blob)[tag]
    return d.mem.rbytes(slot, len(s)) == s


# ---- tests ------------------------------------------------------------------
def test_hash(img):
    print("firmware hash 0x08D08878 vs the Python model")
    d = Deck(img, {})
    rng = random.Random(7)
    paths = ["/Contents/UnknownArtist/UnknownAlbum/Bob banco - Pickety.wav",
             "/Contents/Ünïcødé/Ñandú – ♫.flac", "/a.mp3"]
    paths += ["/Contents/" + "".join(chr(rng.randrange(32, 0x3000)) for _ in range(rng.randrange(1, 90)))
              for _ in range(20)]
    ok = True
    for p in paths:
        d.put_wstr(0x0E300000, p)
        got = d.call(L.FW_HASH, {4: 0x0E300000})
        ok &= got == L.anlz_hash(p)
    chk(f"{len(paths)} paths: firmware == model", ok)


def test_open(img, root):
    print("lopen: filter, log, pass-through, confirm on re-open")
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    r = d.open_track("N:/Contents/A/B/track.mp3")
    nfs = [x for x in d.calls if x[0] == "nfs_open"]
    chk("chains to the stock NFS open once", len(nfs) == 1)
    regs = nfs[0][1]
    chk("r4-r7 unchanged at the stock open",
        regs[4:8] == [0x0E100000, 2 * len("N:/Contents/A/B/track.mp3"), 0x0E101000, 0x0E102000])
    chk("r8-r14 preserved", regs[8:15] == d.before[8:15])
    chk("stack pointer unchanged", regs[15] == STACK)
    chk("pr = caller's return address", nfs[0][4] == MAGIC)
    chk("returns the stock result", r == 0x0CAFE000)
    chk("first open: remembered only (no load, nothing pending)",
        (d.v("NOPEN"), d.v("NAUD"), d.v("NFIRST"), d.v("GEN"), d.v("PEND")) == (1, 1, 1, 0, 0))
    d.mem.w32(L.SYS_MS, 101500)
    d.open_track("N:/Contents/A/B/track.mp3")
    chk("re-open 1.5 s later: confirmed load (gen 1, pending 1)",
        (d.v("NCONF"), d.v("GEN"), d.v("PEND")) == (1, 1, 1))
    chk("path copied", d.mem.wstr(L.V_PATH) == "N:/Contents/A/B/track.mp3")
    chk("load time stamped", d.v("TOPEN") == 101500)
    chk("current track = its N", d.v("CURN") == L.anlz_hash("/Contents/A/B/track.mp3"))
    d.auto_ms = 2000
    for p in ("N:/PIONEER/rekordbox/export.pdb", "N:/.PIONEER/x.DAT",
              "N:/pioneer/USBANLZ/P000/00000000/ANLZ0000.EXT", "N:/PIONEER"):
        d.load(p)
        chk(f"not audio: {p}", d.v("NAUD") == 2 and d.v("GEN") == 1)
    for m in (b"r+", b"w\0", b"a\0"):
        d.open_track("N:/Contents/w.mp3", m)
        d.open_track("N:/Contents/w.mp3", m)
        chk(f"mode {m!r} ignored", d.v("NAUD") == 2)
    d.open_track("N:/Contents/rb.wav", b"rb")
    d.open_track("N:/Contents/rb.wav", b"rb")
    chk("mode 'rb' accepted", d.v("NAUD") == 4 and d.v("GEN") == 2)
    d.load("N:/PIONEERS/track.mp3")
    chk("'/PIONEERS/...' is audio (only the PIONEER tree is skipped)", d.v("GEN") == 3)
    d.load("N:/Contents/" + "x" * 300 + ".mp3")
    chk("over-long path: logged, no fetch", d.v("GEN") == 3 and d.v("NAUD") == 8)
    ring = []
    for i in range(L.LOG_N):
        e = L.V_LOG + i * L.LOG_SZ
        ring.append((d.mem.r32(e) >> 31, d.mem.wstr(e + 4)))
    chk("log ring holds the last 4 opens, 60-char cap",
        ring[(d.v("LOGI") - 1) % 4] == (1, ("N:/Contents/" + "x" * 300)[:60]), str(ring))
    d2 = Deck(img, {"N": root})
    d2.mem.w32(L.V["EN"], 1)
    d2.load("N:/Contents/A/B/track.mp3")
    chk("EN=1: logged + confirmed, nothing pending",
        d2.v("NAUD") == 2 and d2.v("NCONF") == 1 and d2.v("PEND") == 0
        and d2.v("GEN") == 0 and d2.v("LOGI") == 2)
    chk("EN=1: full path kept for --status",
        d2.mem.wstr(L.V_PATH) == "N:/Contents/A/B/track.mp3")
    d3 = Deck(img, {"N": root})
    d3.load("N:/Contents/A/B/track.mp3")
    chk("EN=0: pure pass-through", d3.v("NAUD") == 0 and d3.v("LOGI") == 0
        and len([x for x in d3.calls if x[0] == "nfs_open"]) == 1)


def test_dedup(img, root, audio, blob):
    print("what the deck does around a load (seen on hardware)")
    audio2 = "/Contents/second.mp3"
    blob2 = make_ext(random.Random(9))
    put_track(root, audio2, blob2)
    # the loaded track, its neighbour pre-opened 100 ms later, the re-open
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.open_track("N:" + audio)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 100)
    d.open_track("N:" + audio2)                # neighbour, never re-opened
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 1500)
    d.open_track("N:" + audio)                 # the deck's re-open
    chk("track + neighbour + re-open: the TRACK is the load (gen 1)",
        (d.v("GEN"), d.v("NFIRST"), d.v("NCONF")) == (1, 2, 1)
        and d.mem.wstr(L.V_PATH) == "N:" + audio)
    d.media_read()
    chk("... its colours are published", d.v("DONE") == 1 and slot_ok(d, RO.SLOT5, b"PWV5", blob))
    for k in range(3):                         # re-opens while it plays
        d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 7000)
        d.open_track("N:" + audio)
    d.media_read()
    chk("later re-opens of the current track: nothing (dup 3, no fetch)",
        d.v("NDUP") == 3 and d.v("GEN") == 1
        and not [x for x in d.calls if x[0] == "fopen"])
    # fast skipping: 10 tracks 400 ms apart, each with its neighbour, then
    # the deck re-opens only the last one
    tracks = [f"/Contents/skip{k}.mp3" for k in range(10)]
    for t in tracks:
        put_track(root, t, make_ext(random.Random(hash(t) & 0xFFFF)))
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    for k, t in enumerate(tracks):
        d.open_track("N:" + t)
        d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 100)
        d.open_track("N:" + (tracks[k - 1] if k else audio2))
        d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 300)
        d.media_read()
    chk("10 tracks skipped 400 ms apart: nothing fetched, nothing pending",
        d.v("GEN") == 0 and d.v("NOK") == 0 and d.v("PEND") == 0)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 1200)
    d.open_track("N:" + tracks[-1])            # the deck settles on the last
    d.media_read()
    last = make_ext(random.Random(hash(tracks[-1]) & 0xFFFF))
    chk("... the last one, re-opened: fetched once, its colours",
        d.v("GEN") == 1 and d.v("NOK") == 1 and slot_ok(d, RO.SLOT5, b"PWV5", last))
    # same track again after a local track: fetched again
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:" + audio)
    d.media_read()
    set_preview(d, 77)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + L.WINDOW_MS + 100)
    d.call(d.ent["snap"])                      # a local track loads
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 3000)
    d.load("N:" + audio)                       # the Link track again
    chk("Link track again after a local one: a new load (gen 2)",
        d.v("GEN") == 2 and d.v("PEND") == 2)
    # both decks loading the same file at once: the re-open came 5.8 s late
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:" + audio, gap=5800)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 280)
    d.open_track("N:" + audio2)                # its neighbour
    d.media_read()
    chk("re-open 5.8 s late (both decks on one file): confirmed and fetched",
        d.v("GEN") == 1 and d.v("NOK") == 1 and slot_ok(d, RO.SLOT5, b"PWV5", blob))
    # a first open long ago does not confirm a later one
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.open_track("N:" + audio)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + L.CONFIRM_MS + 1)
    d.open_track("N:" + audio)
    chk(f"second open > {L.CONFIRM_MS} ms later: still unconfirmed", d.v("GEN") == 0)


def test_abort(img, root):
    print("a newer load stops a fetch between 32 KB pieces")
    big = "/Contents/big.wav"
    blob = make_ext(random.Random(21), n5=60000)        # PWV5 = 120 KB, 4 pieces
    put_track(root, big, blob)
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:" + big)
    n = [0]

    def newer(dk):
        n[0] += 1
        if n[0] == 8:                          # mid-PWV5
            dk.mem.w32(L.V["GEN"], dk.mem.r32(L.V["GEN"]) + 1)
    d.on_fread = newer
    d.media_read()
    reads = [x for x in d.calls if x[0] == "fread"]
    big_reads = [x for x in reads if x[3] > 12]
    chk("stopped right after the piece in flight (why 3), file closed",
        d.v("WHY") == 3 and not d.files and len(big_reads) < 6, f"{len(big_reads)} body reads")
    chk("pieces are at most 32 KB", max(x[3] for x in reads) <= L.CHUNK)
    chk("nothing published", d.mem.r32(RO.SLOT5) == 0)
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:" + big)
    d.media_read()
    chk("uninterrupted: the 120 KB section arrives whole, in pieces",
        slot_ok(d, RO.SLOT5, b"PWV5", blob) and d.v("NOK") == 1)


def test_fetch(img, root, audio, blob):
    print("lread / lfetch: the media server's first read after a Link open")
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    for s in (RO.SLOT4, RO.SLOT5, RO.SLOT6):
        d.mem.w32(s, 0xDEADBEEF)              # stale tags from a previous track
    d.load("N:" + audio)
    r = d.media_read()
    nr = [x for x in d.calls if x[0] == "nfs_read"]
    chk("chains to the stock NFS fread once", len(nr) == 1)
    regs = nr[0][1]
    chk("r4-r6 unchanged, r7 = stock fread (as the stock jsr @r7 leaves it)",
        regs[4:8] == [0x0E200000, 0x4000, 0x0CAFE000, L.ORIG_READ])
    chk("r8-r14, stack, macl/mach preserved",
        regs[8:16] == d.before[8:15] + [STACK] and nr[0][2:4] == (0x4D41434C, 0x4D414348))
    chk("returns the stock result", r == 0x1234)
    op = [x for x in d.calls if x[0] == "fopen"]
    want = L.ext_path("N", audio)
    chk("opens the analysis file the firmware would", op and op[0][1] == want,
        f"{op[0][1] if op else None} != {want}")
    chk("mode 'r', default options", op and op[0][2] == b"r\0" and op[0][3] == 0)
    rq = {x[-1] for x in d.calls if x[0] in ("fread", "fseek")}
    chk("every fread/fseek passes the firmware's request block", rq == {L.FW_REQ})
    chk("file closed", [x[0] for x in d.calls].count("fclose") == 1 and not d.files)
    has6 = b"PWV6" in sections(blob)
    chk("PWV4 slot == file section", slot_ok(d, RO.SLOT4, b"PWV4", blob))
    chk("PWV5 slot == file section", slot_ok(d, RO.SLOT5, b"PWV5", blob))
    if has6:
        chk("PWV6 slot == file section", slot_ok(d, RO.SLOT6, b"PWV6", blob))
    else:
        chk("no PWV6 in the file: slot invalidated? (left alone)", True)
    chk("done = gen, sections mask, ok counter",
        (d.v("DONE"), d.v("SECT"), d.v("NOK"), d.v("NFAIL"), d.v("PEND"))
        == (1, 3 | (4 if has6 else 0), 1, 0, 0),
        str((d.v("DONE"), d.v("SECT"), d.v("NOK"), d.v("NFAIL"), d.v("PEND"))))
    chk("N recorded", d.v("N") == L.anlz_hash(audio))
    d.media_read()
    chk("second read: no fetch", not [x for x in d.calls if x[0] == "fopen"])
    chk("claim released", d.mem.r8(L.V["CLAIM"]) == 0)
    return d


def test_sites(img, root, audio, blob):
    print("every NFS fread call site")
    for i, (pool, what, r7fn) in enumerate(L.READ_HOOKS):
        d = Deck(img, {"N": root})
        d.mem.w32(L.V["EN"], 3)
        d.load("N:" + audio)
        d.media_read(i)
        nr = [x for x in d.calls if x[0] == "nfs_read"]
        regs = nr[0][1] if nr else [None] * 16
        r7 = L.ORIG_READ if r7fn else d.before[7]
        chk(f"{what}: fetch published, stock read with r4-r7 as left",
            len(nr) == 1 and regs[4:8] == [0x0E200000, 0x4000, 0x0CAFE000, r7]
            and regs[8:16] == d.before[8:15] + [STACK]
            and slot_ok(d, RO.SLOT5, b"PWV5", blob))
        cnt = [d.mem.r32(L.V["SITE"] + 4 * k) for k in range(len(L.READ_HOOKS))]
        nested = [k for k, (_, w, _) in enumerate(L.READ_HOOKS) if w == "generic fread"]
        chk(f"{what}: recorded as the fetch trigger", d.v("WHO") == i + 1)
        chk(f"{what}: counted on its own site", cnt[i] == 1
            and sum(cnt) == 1, str(cnt))
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:" + audio)
    d.mem.w8(L.V["CLAIM"], 0x80)          # another task is mid-fetch
    d.media_read(2)
    chk("claimed by another task: no second fetch, stock read, still pending",
        not [x for x in d.calls if x[0] == "fopen"] and d.v("PEND") == 1
        and [x for x in d.calls if x[0] == "nfs_read"])


def test_failures(img, root, audio, blob):
    print("failure paths publish nothing")
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:/Contents/not/on/stick.mp3")
    d.media_read()
    chk("missing analysis file -> why 1, stock read still done",
        (d.v("WHY"), d.v("NFAIL"), d.v("NOK")) == (1, 1, 0)
        and [x for x in d.calls if x[0] == "nfs_read"])
    # stale: a newer Link open lands while we read
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:" + audio)

    def bump(dk):
        dk.mem.w32(L.V["GEN"], dk.mem.r32(L.V["GEN"]) + 1)
        dk.on_fread = None
    d.on_fread = bump
    d.media_read()
    chk("superseded -> why 3, no tag published",
        d.v("WHY") == 3 and d.mem.r32(RO.SLOT4) == 0 and d.mem.r32(RO.SLOT5) == 0)
    # not an ANLZ file
    bad = os.path.join(root, *L.ext_path("N", "/Contents/bad.mp3")[3:].split("/"))
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    open(bad, "wb").write(b"JUNK" + bytes(60))
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:/Contents/bad.mp3")
    d.media_read()
    chk("not PMAI -> why 4, closed", d.v("WHY") == 4 and not d.files)
    # section too big for its slot
    rng = random.Random(3)
    big = make_ext(rng, n5=RO.SLOT5_MAX // 2 + 100, with6=False)
    put_track(root, "/Contents/long.mp3", big)
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    d.load("N:/Contents/long.mp3")
    d.media_read()
    chk("PWV5 larger than its slot: skipped, PWV4 still published",
        d.v("SECT") == 1 and d.mem.r32(RO.SLOT5) == 0 and slot_ok(d, RO.SLOT4, b"PWV4", big))


def overview(seed):
    rng = random.Random(1000 + seed)
    return [(rng.randrange(25), rng.randrange(8)) for _ in range(L.PWAV_N)]   # deck: <= 24


def fake_ctx(d, blob, seed=None):
    """A USB ANLZ context whose extra-section index holds blob's sections;
    with `seed`, also the overview (PWAV) of that track at ctx+0xD9C."""
    ctx = 0x0B200000
    d.mem.w32(RO.CTX_TAB, ctx)
    pw = ctx + L.CTX_PWAV
    if seed is None:
        d.mem.wbytes(pw, bytes(20 + L.PWAV_N))
    else:
        d.mem.wbytes(pw, b"PWAV" + struct.pack("<II", 20, 20 + L.PWAV_N)
                     + struct.pack(">I", L.PWAV_N) + bytes(4)
                     + bytes(h | (w << 5) for h, w in overview(seed)))
    secs = sections(blob)
    idx = ctx + RO.IDX_OFF
    d.mem.w32(idx, len(secs))
    p = 0x0B300000
    for i, (tag, s) in enumerate(secs.items()):
        d.mem.wbytes(p, s)
        e = idx + 4 + 16 * i
        d.mem.w32(e, 0)
        d.mem.w32(e + 4, struct.unpack("<I", tag)[0])
        d.mem.w32(e + 8, len(s))
        d.mem.w32(e + 12, p)
        p += (len(s) + 15) & ~15


def set_preview(d, seed):
    """The loaded track's overview as the deck keeps it: {height, whiteness}."""
    d.mem.wbytes(RD.PREVIEW, bytes(b for hw in overview(seed) for b in hw))


def test_snap(img, root, audio, blob):
    print("lsnap: stock snapshot for local tracks, Link-aware otherwise")
    rng = random.Random(11)
    local = make_ext(rng, n5=5000)
    # 1. no Link activity: identical to the stock routine
    a, b = Deck(img, {}), Deck(img, {})
    for d in (a, b):
        fake_ctx(d, local, 1)
        set_preview(d, 1)
        d.set_len(local)
    for i, x in enumerate(RD.copy_bytes()):
        a.mem.w8(RD.COPY5 + i, x)
    a.call(RD.COPY5)
    b.call(b.ent["snap"])
    same = all(a.mem.rbytes(s, n) == b.mem.rbytes(s, n) for s, n in
               ((RO.SLOT4, 0x2000), (RO.SLOT6, 0x1000), (RO.SLOT5, 0x40000),
                (RO.SNAP_HASH, 4)))
    chk("no Link open: byte-identical to rgb_detail COPY5", same)
    chk("... and it did copy the USB sections", slot_ok(b, RO.SLOT5, b"PWV5", local))
    chk("counted as local", b.v("NLOCAL") == 1 and b.v("NLINK") == 0)
    # 2. Link load, fetch BEFORE the new-track paint: keep the fetched slots
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    fake_ctx(d, local, 1)                    # this deck also has a USB stick
    set_preview(d, 1)
    d.set_len(local)
    d.call(d.ent["snap"])                    # USB track showing
    d.load("N:" + audio)
    d.media_read()
    d.mem.w32(L.SYS_MS, d.v("TOPEN"))
    d.set_len(blob)
    set_preview(d, 2)                        # the Link track's preview arrives
    d.call(d.ent["snap"])
    chk("fetch first: Link track keeps the fetched PWV5 (not the USB one)",
        slot_ok(d, RO.SLOT5, b"PWV5", blob) and d.v("NLINK") == 1 and d.v("BOUND") == 1)
    chk("... PWV4 too", slot_ok(d, RO.SLOT4, b"PWV4", blob))
    d.call(d.ent["snap"])
    chk("same track repainted: nothing changes", slot_ok(d, RO.SLOT5, b"PWV5", blob)
        and d.v("NLINK") == 1)
    # 3. new-track paint BEFORE the fetch: blank, then the fetch fills it
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    fake_ctx(d, local, 1)
    set_preview(d, 1)
    d.set_len(local)
    d.call(d.ent["snap"])
    d.load("N:" + audio)
    d.set_len(blob)
    set_preview(d, 2)
    d.mem.w32(L.SYS_MS, d.v("TOPEN") + 300)
    d.call(d.ent["snap"])
    chk("paint first: slots blanked (no USB colours on the Link track)",
        d.mem.r32(RO.SLOT4) == 0 and d.mem.r32(RO.SLOT5) == 0 and d.mem.r32(RO.SLOT6) == 0)
    d.media_read()
    chk("... then the fetch publishes this track's colours",
        slot_ok(d, RO.SLOT5, b"PWV5", blob) and slot_ok(d, RO.SLOT4, b"PWV4", blob))
    # 4. local track loaded after the Link one
    d.mem.w32(L.SYS_MS, d.v("TOPEN") + 900)
    d.set_len(local)
    fake_ctx(d, local, 3)                    # the deck parsed the USB track
    set_preview(d, 3)
    d.call(d.ent["snap"])
    chk("next track (no new Link open) is local: USB sections again",
        slot_ok(d, RO.SLOT5, b"PWV5", local) and d.v("NLOCAL") == 2)
    # 5. paint long after the open: treated as local
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    fake_ctx(d, local, 5)
    d.load("N:" + audio)
    d.mem.w32(L.SYS_MS, d.v("TOPEN") + L.WINDOW_MS + 1)
    d.set_len(local)
    set_preview(d, 5)
    d.call(d.ent["snap"])
    chk(f"new track > {L.WINDOW_MS} ms after the open: local", d.v("NLINK") == 0
        and slot_ok(d, RO.SLOT5, b"PWV5", local))


def test_verify(img, root, audio, blob):
    print("colours only ever belong to the loaded track (deck A, SEARCH)")
    s1 = make_ext(random.Random(31), n5=69304)       # 7:42 track, overview 11
    s2 = make_ext(random.Random(32), n5=62000)       # 6:53 track, overview 12
    s3 = make_ext(random.Random(33), n5=69304)       # ANOTHER 7:42 track (13)
    d = Deck(img, {})
    fake_ctx(d, s1, 11)
    d.set_len(s1)
    set_preview(d, 11)
    d.call(d.ent["snap"])
    chk("track 1 loaded: its colours, verified", slot_ok(d, RO.SLOT5, b"PWV5", s1)
        and d.v("OKLEN") == ent5(s1))
    fake_ctx(d, s2, 12)
    d.set_len(s2)
    set_preview(d, 12)
    d.call(d.ent["snap"])
    chk("SEARCH forward: track 2's colours", slot_ok(d, RO.SLOT5, b"PWV5", s2)
        and slot_ok(d, RO.SLOT4, b"PWV4", s2))
    d.set_len(s1)
    set_preview(d, 11)
    d.call(d.ent["snap"])                             # buffer still track 2
    chk("SEARCH back, buffer still track 2: NOT copied, stock blue",
        d.mem.r32(RO.SLOT5) == 0 and d.mem.r32(RO.SLOT4) == 0 and d.v("NBLANK") == 1)
    d.col()
    dc = [x for x in d.calls if x[0] == "dcave"]
    chk("painter call goes on to DCAVE once", len(dc) == 1)
    fake_ctx(d, s1, 11)                               # the deck refills it
    d.col()
    chk("... checked < 100 ms ago: no work yet", d.mem.r32(RO.SLOT5) == 0)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + L.FIX_MS)
    d.col()
    chk("buffer refilled with track 1: copied on the next painter call",
        slot_ok(d, RO.SLOT5, b"PWV5", s1) and slot_ok(d, RO.SLOT4, b"PWV4", s1)
        and d.v("NFIX") == 3 and d.v("OKLEN") == ent5(s1))
    dc = [x for x in d.calls if x[0] == "dcave"]
    regs = dc[0][1] if dc else [None] * 16
    chk("DCAVE sees the painter's r1 r2 r5 r8-r15, MAC and pr",
        dc and regs[1] == d.before[1] and regs[2] == 5 and regs[5] == d.before[5]
        and regs[8] == 0x1238 and regs[9:15] == d.before[9:15] and regs[15] == STACK
        and dc[0][2:5] == (0x4D41434C, 0x4D414348, MAGIC))
    n = d.v("NFIX")
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 1000)
    fake_ctx(d, s2, 12)                               # BROWSE fills the buffer
    d.col()
    chk("verified track + BROWSE refilling the buffer: untouched",
        slot_ok(d, RO.SLOT5, b"PWV5", s1) and d.v("NFIX") == n)
    # the question that started this: another song of EXACTLY the same length
    d = Deck(img, {})
    fake_ctx(d, s3, 13)                               # buffer: the 7:42 twin
    d.set_len(s1)                                     # loaded: track 1 (7:42)
    set_preview(d, 11)
    d.call(d.ent["snap"])
    chk("buffer holds another song of the same length: NOT copied",
        ent5(s1) == ent5(s3) and d.mem.r32(RO.SLOT5) == 0)
    fake_ctx(d, s1, 11)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 1000)
    d.col()
    chk("... the right one arrives: copied", slot_ok(d, RO.SLOT5, b"PWV5", s1))
    # BROWSE: the overview the deck keeps AND the buffer both show the
    # browsed track (if the deck did that) -- the player's length says no
    d = Deck(img, {})
    fake_ctx(d, s2, 12)
    set_preview(d, 12)                                # both: track 2 (browsed)
    d.set_len(s1)                                     # player: track 1 loaded
    d.call(d.ent["snap"])
    chk("buffer + overview of a browsed track, player on track 1: NOT copied",
        d.mem.r32(RO.SLOT5) == 0)
    # the deck rescales some overviews (+2 / +1 / 0 on hardware)
    def preview_from(d, seed, dh=None, dw=None):
        ov = overview(seed)
        out = []
        for i, (h_, w_) in enumerate(ov):
            h2 = h_ + (dh(i, h_) if dh else 0)
            w2 = (w_ + dw(i)) & 7 if dw else w_
            out += [max(0, h2), w2]
        d.mem.wbytes(RD.PREVIEW, bytes(out))
    for name, dh, dw, want in (
            ("heights +2 / +1 / 0 (as on the deck): copied",
             lambda i, h_: 0 if h_ < 8 else (1 if i % 10 == 3 else 2), None, True),
            ("heights +3 everywhere: copied", lambda i, h_: 3, None, True),
            ("one height +4: refused", lambda i, h_: 4 if i == 200 else 0, None, False),
            ("one whiteness different: refused", None, lambda i: 1 if i == 77 else 0, False)):
        d = Deck(img, {})
        fake_ctx(d, s1, 11)
        d.set_len(s1)
        preview_from(d, 11, dh, dw)
        d.call(d.ent["snap"])
        chk(name, (d.mem.r32(RO.SLOT5) != 0) == want
            and (not want or slot_ok(d, RO.SLOT5, b"PWV5", s1)))
    # one column off is enough to refuse
    d = Deck(img, {})
    fake_ctx(d, s1, 11)
    ctx = d.mem.r32(RO.CTX_TAB)
    b0 = d.mem.r8(ctx + L.CTX_PWAV + 20 + 399)
    d.mem.w8(ctx + L.CTX_PWAV + 20 + 399, b0 ^ 0x20)      # its whiteness
    d.set_len(s1)
    set_preview(d, 11)
    d.call(d.ent["snap"])
    chk("399 of 400 columns equal (one whiteness off): refused", d.mem.r32(RO.SLOT5) == 0)
    # a deck without a stick: the previous track's colours never linger
    d = Deck(img, {})
    put_slots(d, s2)
    d.set_len(s1)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 1000)
    d.col()
    chk("no buffer, slots of another track: blanked", d.mem.r32(RO.SLOT5) == 0)
    # Link: the fetch is kept even if the length word disagrees
    d = Deck(img, {"N": root})
    d.mem.w32(L.V["EN"], 3)
    fake_ctx(d, s2, 12)
    d.load("N:" + audio)
    d.media_read()
    d.mem.w32(L.TRACK_LEN, ent5(blob) + 500)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 1000)
    d.col()
    chk("Link fetch of the current track: kept", slot_ok(d, RO.SLOT5, b"PWV5", blob))
    set_preview(d, 9)
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + L.WINDOW_MS + 100)
    d.call(d.ent["snap"])                             # a new (local) track
    chk("... until another track loads: then blanked, not left on it",
        d.mem.r32(RO.SLOT5) == 0)
    d.set_len(blob)
    d.load("N:" + audio)
    d.media_read()
    d.mem.w32(L.SYS_MS, d.mem.r32(L.SYS_MS) + 1000)
    d.col()
    chk("Link track again: its fetch shown and verified",
        slot_ok(d, RO.SLOT5, b"PWV5", blob) and d.v("OKLEN") == ent5(blob))


def test_real(img, stick):
    print(f"real stick {stick}: analysis paths and a full fetch")
    import glob
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    dats = sorted(glob.glob(os.path.join(stick, "PIONEER", "USBANLZ", "P*", "*", "ANLZ0000.DAT")))
    if not dats:
        print("  (no ANLZ files there -- skipped)")
        return
    d = Deck(img, {"N": stick})
    d.mem.w32(L.V["EN"], 3)
    rng = random.Random(5)
    pick = rng.sample(dats, min(12, len(dats)))
    ok_n = 0
    for dat in pick:
        raw = open(dat, "rb").read()
        off = struct.unpack(">I", raw[4:8])[0]
        path = None
        while off + 12 <= len(raw):
            tag = raw[off:off + 4]
            hl, tl = struct.unpack(">II", raw[off + 4:off + 12])
            if tag == b"PPTH":
                ln = struct.unpack(">I", raw[off + 12:off + 16])[0]
                path = raw[off + hl:off + hl + ln].decode("utf-16-be").rstrip("\0")
                break
            off += tl
        ext = dat[:-3] + "EXT"
        d.load("N:" + path)
        d.media_read()
        blob = open(ext, "rb").read()
        good = (d.v("DONE") == d.v("GEN") and slot_ok(d, RO.SLOT5, b"PWV5", blob)
                and slot_ok(d, RO.SLOT4, b"PWV4", blob))
        ok_n += good
        if not good:
            print(f"    miss: {path!r} why={L.WHY.get(d.v('WHY'))}")
    chk(f"{ok_n}/{len(pick)} real tracks fetched byte-exact", ok_n == len(pick))
    print(f"    (last fetch read {sum(x[3] for x in d.calls if x[0] == 'fread')} B)")


def main():
    img = open(blk1_path(), "rb").read()
    print(f"firmware {blk1_path()} ({len(img)} B); module {L.LBASE:#x}")
    tmp = tempfile.mkdtemp(prefix="linkrgb_")
    rng = random.Random(1)
    audio = "/Contents/Artist Ñ/Album/01 - Track (Extended Mix).mp3"
    blob = make_ext(rng)
    put_track(tmp, audio, blob)
    test_hash(img)
    test_open(img, tmp)
    test_dedup(img, tmp, audio, blob)
    test_abort(img, tmp)
    test_fetch(img, tmp, audio, blob)
    test_sites(img, tmp, audio, blob)
    a6 = "/Contents/no6.wav"
    b6 = make_ext(random.Random(2), with6=False)
    put_track(tmp, a6, b6)
    test_fetch(img, tmp, a6, b6)
    test_failures(img, tmp, audio, blob)
    test_snap(img, tmp, audio, blob)
    test_verify(img, tmp, audio, blob)
    stick = sys.argv[1] if len(sys.argv) > 1 else None
    if stick:
        test_real(img, stick)
    print(f"\nRESULT: {'PASS' if not FAILS else 'FAIL'} "
          f"({len(FAILS)} failed{': ' + ', '.join(FAILS) if FAILS else ''})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
