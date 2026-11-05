#!/usr/bin/env python3
"""Serve a library FOLDER to QEMU as a USB stick, over NBD on a unix socket.

The QEMU emulator (docs/EMU_REBUILD_PLAN.md) only takes disk images; its own
stick builder makes a FAT16 image of at most 2 GB by copying files. This
serves our FolderDisk (emu/usbdisk.py: FAT32 synthesised over the folder,
any size, nothing copied) instead:

    qemu ... -drive if=none,id=usbstk,file=nbd:unix:/tmp/stick.sock,format=raw \\
             -device usb-storage,drive=usbstk,port=1

The deck's own writes (settings, history) land in the FolderDisk overlay in
memory -- never in the user's folder.

    usage: qemu_stick_nbd.py <folder> <socket> [label]

Minimal NBD server: fixed-newstyle handshake, OPT_GO / OPT_INFO /
OPT_EXPORT_NAME, simple replies, READ / WRITE / FLUSH / DISC.
"""
import os
import socket
import struct
import sys
import threading

sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
from emu.usbdisk import FolderDisk, SECTOR  # noqa: E402

NBDMAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
REPMAGIC = 0x3E889045565A9
REQMAGIC = 0x25609513
SIMPLE = 0x67446698
OPT_EXPORT_NAME, OPT_ABORT, OPT_INFO, OPT_GO = 1, 2, 6, 7
REP_ACK, REP_INFO = 1, 3
REP_ERR_UNSUP = 0x80000001
FLAG_FIXED, FLAG_NOZEROES = 1, 2
TF = 1 | 4                                      # HAS_FLAGS | SEND_FLUSH
CMD_READ, CMD_WRITE, CMD_DISC, CMD_FLUSH = 0, 1, 2, 3
EIO, EINVAL = 5, 22


def recvall(c, n):
    b = bytearray()
    while len(b) < n:
        part = c.recv(n - len(b))
        if not part:
            raise ConnectionError("client gone")
        b += part
    return bytes(b)


class Server:
    def __init__(self, disk):
        self.disk = disk
        self.size = disk.sectors * SECTOR
        self.lock = threading.Lock()

    def _rep(self, c, opt, typ, data=b""):
        c.sendall(struct.pack(">QIII", REPMAGIC, opt, typ, len(data)) + data)

    def handshake(self, c):
        c.sendall(NBDMAGIC + struct.pack(">QH", IHAVEOPT, FLAG_FIXED | FLAG_NOZEROES))
        cflags = struct.unpack(">I", recvall(c, 4))[0]
        while True:
            magic, opt, ln = struct.unpack(">QII", recvall(c, 16))
            data = recvall(c, ln)
            if magic != IHAVEOPT:
                raise ConnectionError("bad option magic")
            if opt == OPT_EXPORT_NAME:
                tail = b"" if cflags & FLAG_NOZEROES else bytes(124)
                c.sendall(struct.pack(">QH", self.size, TF) + tail)
                return
            if opt in (OPT_GO, OPT_INFO):
                self._rep(c, opt, REP_INFO, struct.pack(">HQH", 0, self.size, TF))
                self._rep(c, opt, REP_ACK)
                if opt == OPT_GO:
                    return
                continue
            if opt == OPT_ABORT:
                self._rep(c, opt, REP_ACK)
                raise ConnectionError("client aborted")
            self._rep(c, opt, REP_ERR_UNSUP)

    def serve(self, c):
        self.handshake(c)
        while True:
            magic, _flags, typ, handle, off, ln = struct.unpack(">IHHQQI", recvall(c, 28))
            if magic != REQMAGIC:
                return
            if typ == CMD_DISC:
                return
            if typ == CMD_WRITE:
                data = recvall(c, ln)
            err, out = 0, b""
            if off % SECTOR or ln % SECTOR or off + ln > self.size:
                err = EINVAL
            elif typ == CMD_READ:
                with self.lock:
                    out = self.disk.read(off // SECTOR, ln // SECTOR)
            elif typ == CMD_WRITE:
                with self.lock:
                    self.disk.write(off // SECTOR, data)
            elif typ != CMD_FLUSH:
                err = EINVAL
            c.sendall(struct.pack(">IIQ", SIMPLE, err, handle) + (out if not err else b""))


class _Background:
    """A listening Server on a unix socket in daemon threads; close() stops
    accepting and drops the socket file (live connections end with QEMU)."""

    def __init__(self, srv, path):
        self.path = path
        if isinstance(path, tuple):            # ("127.0.0.1", port): Windows
            self.ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass
            self.ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.ls.bind(path)
        self.ls.listen(4)
        self.srv = srv
        threading.Thread(target=self._accept, daemon=True, name="nbd").start()

    def _accept(self):
        while True:
            try:
                c, _ = self.ls.accept()
            except OSError:
                return

            def run(c=c):
                try:
                    self.srv.serve(c)
                except (ConnectionError, OSError):
                    pass
                finally:
                    c.close()
            threading.Thread(target=run, daemon=True).start()

    def close(self):
        try:
            self.ls.close()
        except OSError:
            pass
        if not isinstance(self.path, tuple):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def serve_background(disk, path):
    """Serve `disk` (FolderDisk / ImageDisk) on unix socket `path`."""
    return _Background(Server(disk), path)


def main(argv):
    """<folder|image> <socket path | tcp:PORT> [label]: serve until the
    parent process (the app) goes away."""
    if len(argv) < 2:
        print(__doc__)
        return 2
    src, path = argv[0], argv[1]
    if os.path.isdir(src):
        disk = FolderDisk(src, *(argv[2:3]))
    else:
        from emu.usbdisk import ImageDisk
        disk = ImageDisk(src)
    if path.startswith("tcp:"):
        path = ("127.0.0.1", int(path[4:]))
    bg = serve_background(disk, path)
    print(f"READY stick: {src} -> {path} ({bg.srv.size / 1e9:.1f} GB)", flush=True)
    import time
    parent = os.getppid()
    while os.getppid() == parent:          # the app quit (or was killed)
        time.sleep(1)
    bg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
