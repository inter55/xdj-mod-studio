"""Virtual USB-stick disks for the emulated deck.

`FolderDisk(path)` presents a host folder (e.g. a rekordbox export: the
folder that holds PIONEER/ and Contents/) as a FAT32 stick, built lazily:
no image file is written and file data is read from the host only when the
deck asks for those sectors, so a many-GB music export costs nothing up
front. `ImageDisk(path)` serves a raw disk image (dd of a real stick).

Writes (the deck keeps history/settings on the stick) go to an in-memory
overlay and are never written back to the host files.
"""
import os
import struct

SECTOR = 512


class _Overlay:
    def __init__(self):
        self.written = {}
        # Monotonic IO-op counter: the access ring trims (last 20000),
        # so len(access) goes FLAT during long reads and any growth-based
        # burst/fast-forward check sees zero. Count ops instead.
        self.ops = 0

    def read(self, lba, count):
        # Fast path (XDJ_USB_FAST=1): coalesced host reads, same bytes.
        # Falls back to the slow per-sector loop on any miss.
        try:
            from emu.usb_fast import is_fast, coalesced_read
            if is_fast() and count >= 2:
                try:
                    return coalesced_read(self, lba, count)
                except Exception:
                    pass
        except ImportError:
            pass
        out = bytearray()
        for i in range(count):
            s = self.written.get(lba + i)
            out += s if s is not None else self._read1(lba + i)
        return bytes(out)

    def write(self, lba, data):
        n = len(data) // SECTOR
        for i in range(n):
            self.written[lba + i] = bytes(data[i * SECTOR:(i + 1) * SECTOR])
        tail = len(data) % SECTOR
        if tail:
            # read-modify-write the trailing partial sector instead of
            # dropping it (a dropped FAT/dir tail corrupts the disk and
            # the firmware answers with USB Error much later)
            base = lba + n
            cur = bytearray(self._read1(base))
            cur[:tail] = data[n * SECTOR:]
            self.written[base] = bytes(cur)


class ImageDisk(_Overlay):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.f = open(path, "rb")
        self.sectors = os.path.getsize(path) // SECTOR

    def _read1(self, lba):
        self.ops += 1
        self.f.seek(lba * SECTOR)
        return self.f.read(SECTOR).ljust(SECTOR, b"\0")


# -- FAT32 builder --------------------------------------------------------------
SPC = 8                       # sectors per cluster (4 KiB clusters)
CLUS = SPC * SECTOR
PART_LBA = 2048               # partition start (1 MiB aligned, like a real stick)
RESERVED = 32
MIN_CLUSTERS = 70000          # FAT32 needs >= 65525 clusters


def _lfn_checksum(short11):
    s = 0
    for c in short11:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def _short_name(name, used):
    import unicodedata
    flat = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base, ext = os.path.splitext(flat)
    # every character FAT allows in a short name (not just letters/digits):
    # "Cheers!" / "Slammin'" keep their plain short name like on a stick
    # the Mac or Windows formatted; dropping the "!" made it "CHEERS~1" and
    # the deck could not open /Contents/Cheers!/... (E-8302, 2026-11-02)
    clean = lambda t: "".join(c for c in t.upper()
                              if (c.isalnum() and ord(c) < 128) or c in "_-!#$%&'()@^`{}~")
    b, e = clean(base)[:8], clean(ext[1:])[:3]
    # 2026-10-18: like Windows/macOS FAT drivers, an all-lowercase (or all-
    # upper) 8.3 name keeps its plain short name ("export.pdb" ->
    # "EXPORT  PDB" + lowercase flags), not "EXPORT~1.PDB". The deck looks
    # up 8.3-sized names by their SHORT name: with the ~1 alias it never
    # found PIONEER/rekordbox/export.pdb -> folder view only, no library.
    # Windows/macOS rule: a name that fits 8.3 once uppercased (nothing
    # dropped or shortened) keeps that plain short name ("Contents" ->
    # "CONTENTS" + an LFN for the case); "~n" only for lossy names or
    # collisions. The deck looks 8.3-sized path parts up by SHORT name, so
    # "CONTEN~1" broke every library-view load (2026-10-18).
    exact = (b == base.upper() and e == ext[1:].upper() and len(base) <= 8
             and len(ext) <= 4 and base != "")
    if exact and (b.ljust(8) + e.ljust(3)) not in used:
        s = b.ljust(8) + e.ljust(3)
    else:
        n = 1
        while True:
            tail = f"~{n}"
            s = (b[:8 - len(tail)] + tail).ljust(8) + e.ljust(3)
            if s not in used:
                break
            n += 1
    used.add(s)
    return s.encode("ascii")


def _case_flags(name, short11):
    """NT reserved byte (DIR_NTRes): 0x08 = base lowercase, 0x10 = ext
    lowercase -- only for names stored as their plain short name."""
    base, ext = os.path.splitext(name)
    if b"~" in short11 or base.upper().ljust(8).encode() != short11[:8]:
        return None
    one_case = lambda t: t == t.lower() or t == t.upper()
    if not (one_case(base) and one_case(ext[1:])):
        return None                  # mixed case: keep the LFN for it
    f = 0
    if base and base == base.lower() and base != base.upper():
        f |= 0x08
    if ext[1:] and ext[1:] == ext[1:].lower() and ext[1:] != ext[1:].upper():
        f |= 0x10
    return f


def _entries_for(name, short11, attr, cluster, size):
    """Directory entries (LFN chain + short entry) for one child."""
    out = bytearray()
    flags = _case_flags(name, short11)
    if flags is not None:            # plain 8.3 name: no LFN, case in NTRes
        return _short(short11, attr, cluster, size, flags)
    u = name.encode("utf-16-le") + b"\0\0"
    chunks = [u[i:i + 26] for i in range(0, len(u), 26)]
    chunks[-1] = chunks[-1].ljust(26, b"\xff")
    ck = _lfn_checksum(short11)
    for i in range(len(chunks), 0, -1):
        c = chunks[i - 1]
        seq = i | (0x40 if i == len(chunks) else 0)
        out += bytes([seq]) + c[0:10] + bytes([0x0F, 0, ck]) + c[10:22] + b"\0\0" + c[22:26]
    out += _short(short11, attr, cluster, size)
    return out


def _short(short11, attr, cluster, size, ntres=0):
    # fixed timestamp 2020-01-01 00:00
    t, d = 0, ((2020 - 1980) << 9) | (1 << 5) | 1
    return struct.pack("<11sBBBHHHHHHHI", short11, attr, ntres, 0, t, d, d,
                       cluster >> 16, t, d, cluster & 0xFFFF, size)


class FolderDisk(_Overlay):
    def __init__(self, root, label="REKORDBOX"):
        super().__init__()
        self.root = os.path.abspath(root)
        self.label = label.upper()[:11].ljust(11).encode("ascii")
        # walk: every dir/file becomes an item with a cluster extent
        self.items = []      # dicts: kind, path, name, size, children, parent
        top = self._scan(self.root, None)
        # Adaptive cluster size: big exports get 32 KiB clusters (what a
        # real 16 GB FAT32 stick uses). The firmware scans the FAT one
        # sector per USB transaction, so the cluster count sets the wall
        # time of a mount; 8x fewer clusters = 8x faster mounting.
        total = sum(it.get("size", 0) for it in self.items)
        self.spc = 64 if total > (1 << 30) else SPC
        self.clus = self.spc * SECTOR
        # directory byte sizes (entries known before clusters are)
        for it in self.items:
            if it["kind"] == "dir":
                n = 1 if it is top else 2                       # label or ./..
                for ch in it["children"]:
                    n += 1 + (len(ch["name"]) + 1 + 12) // 13
                it["size_alloc"] = max(self.clus,
                                       -(-n * 32 // self.clus) * self.clus)
            else:
                it["size_alloc"] = -(-it["size"] // self.clus) * self.clus
        c = 2
        self.extents = []    # (first cluster, n clusters, item)
        for it in self.items:
            n = it["size_alloc"] // self.clus
            it["cluster"] = c if n else 0
            if n:
                self.extents.append((c, n, it))
            c += n
        used = c - 2
        self.clusters = max(MIN_CLUSTERS, used + used // 16 + 1024)
        self.fat_secs = -(-(self.clusters + 2) * 4 // SECTOR)
        self.data_lba = RESERVED + 2 * self.fat_secs
        self.part_secs = self.data_lba + self.clusters * self.spc
        self.sectors = PART_LBA + self.part_secs
        self._starts = [e[0] for e in self.extents]
        for it in self.items:
            if it["kind"] == "dir":
                it["bytes"] = self._dir_bytes(it, it is top)
        self._fh = {}
        self.access = []      # (host path, file offset) per sector served
                              # (+ ("SECTOR", lba) for non-file sectors;
                              # ring: keeps the LAST 20000)

    def _scan(self, path, parent):
        it = {"kind": "dir", "path": path, "name": os.path.basename(path),
              "children": [], "parent": parent}
        self.items.append(it)
        try:
            names = sorted(os.listdir(path))
        except OSError:
            names = []
        for nm in names:
            if nm.startswith(".") or nm == "System Volume Information":
                continue
            p = os.path.join(path, nm)
            if os.path.isdir(p):
                it["children"].append(self._scan(p, it))
            elif os.path.isfile(p):
                f = {"kind": "file", "path": p, "name": nm,
                     "size": os.path.getsize(p), "parent": it}
                self.items.append(f)
                it["children"].append(f)
        return it

    def _dir_bytes(self, it, is_root):
        out = bytearray()
        if is_root:
            out += _short(self.label, 0x08, 0, 0)
        else:
            par = it["parent"]
            out += _short(b".          ", 0x10, it["cluster"], 0)
            out += _short(b"..         ", 0x10,
                          0 if par["parent"] is None else par["cluster"], 0)
        used = set()
        for ch in it["children"]:
            s11 = _short_name(ch["name"], used)
            if ch["kind"] == "dir":
                out += _entries_for(ch["name"], s11, 0x10, ch["cluster"], 0)
            else:
                out += _entries_for(ch["name"], s11, 0x20, ch["cluster"], ch["size"])
        return bytes(out)

    # -- sector synthesis ---------------------------------------------------------
    def _read1(self, lba):
        self.ops += 1
        if len(self.access) > 21000:
            del self.access[:1000]
        if lba < PART_LBA:
            if True:  # ring-buffer (trimmed in _read1)
                self.access.append(("SECTOR", lba))
            return self._mbr() if lba == 0 else bytes(SECTOR)
        s = lba - PART_LBA
        if s in (0, 6):
            if True:  # ring-buffer (trimmed in _read1)
                self.access.append(("SECTOR", lba))
            return self._boot()
        if s in (1, 7):
            if True:  # ring-buffer (trimmed in _read1)
                self.access.append(("SECTOR", lba))
            return self._fsinfo()
        if s < RESERVED:
            if True:  # ring-buffer (trimmed in _read1)
                self.access.append(("SECTOR", lba))
            return bytes(SECTOR)
        if s < self.data_lba:
            if True:  # ring-buffer (trimmed in _read1)
                self.access.append(("SECTOR", lba))
            return self._fat((s - RESERVED) % self.fat_secs)
        c, off = divmod(s - self.data_lba, self.spc)
        return self._cluster(c + 2, off * SECTOR)

    def _mbr(self):
        b = bytearray(SECTOR)
        b[446:462] = struct.pack("<B3sB3sII", 0x00, b"\xfe\xff\xff", 0x0C,
                                 b"\xfe\xff\xff", PART_LBA, self.part_secs)
        b[510:512] = b"\x55\xaa"
        return bytes(b)

    def _boot(self):
        b = bytearray(SECTOR)
        b[0:3] = b"\xeb\x58\x90"
        b[3:11] = b"MSWIN4.1"
        struct.pack_into("<HBHBHHBHHHII", b, 11, SECTOR, self.spc,
                         RESERVED, 2, 0, 0,
                         0xF8, 0, 63, 255, PART_LBA, self.part_secs)
        struct.pack_into("<IHHIHH", b, 36, self.fat_secs, 0, 0, 2, 1, 6)
        struct.pack_into("<BBBI11s8s", b, 64, 0x80, 0, 0x29, 0x58444A37,
                         self.label, b"FAT32   ")
        b[510:512] = b"\x55\xaa"
        return bytes(b)

    def _fsinfo(self):
        b = bytearray(SECTOR)
        used = sum(e[1] for e in self.extents)
        struct.pack_into("<I", b, 0, 0x41615252)
        struct.pack_into("<III", b, 484, 0x61417272, self.clusters - used, 0xFFFFFFFF)
        struct.pack_into("<I", b, 508, 0xAA550000)
        return bytes(b)

    def _fat(self, sec):
        import bisect
        out = bytearray(SECTOR)
        first = sec * (SECTOR // 4)
        for k in range(SECTOR // 4):
            c = first + k
            if c == 0:
                v = 0x0FFFFFF8
            elif c == 1:
                v = 0x0FFFFFFF
            else:
                i = bisect.bisect_right(self._starts, c) - 1
                v = 0
                if i >= 0:
                    c0, n, _ = self.extents[i]
                    if c < c0 + n:
                        v = 0x0FFFFFFF if c == c0 + n - 1 else c + 1
            struct.pack_into("<I", out, k * 4, v)
        return bytes(out)

    def _cluster(self, c, off):
        import bisect
        i = bisect.bisect_right(self._starts, c) - 1
        if i < 0:
            return bytes(SECTOR)
        c0, n, it = self.extents[i]
        if c >= c0 + n:
            return bytes(SECTOR)
        pos = (c - c0) * self.clus + off
        if it["kind"] == "dir":
            if True:  # ring-buffer (trimmed in _read1)
                self.access.append(("DIR:" + it["path"], pos))
            return it["bytes"][pos:pos + SECTOR].ljust(SECTOR, b"\0")
        if True:  # ring-buffer (trimmed in _read1)
            self.access.append((it["path"], pos))
        f = self._fh.get(it["path"])
        if f is None:
            if len(self._fh) > 32:
                for h in self._fh.values():
                    h.close()
                self._fh.clear()
            f = self._fh[it["path"]] = open(it["path"], "rb")
        f.seek(pos)
        return f.read(SECTOR).ljust(SECTOR, b"\0")


def open_disk(path):
    """A folder becomes a FAT32 stick; a file is a raw image."""
    return FolderDisk(path) if os.path.isdir(path) else ImageDisk(path)
