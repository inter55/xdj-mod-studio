#!/usr/bin/env python3
"""usbloader_test.py -- the USB-stick mods loader (tools/usbloader) in the
emulated XDJ-700. Nothing here touches a deck.

From cache/emu_realusb.pkl (booted, HLE, C core): write build/usbloader/
ul.bin at its link address, point the MYSETTING.DAT mount reader's open
pool word 0x08DE39A4 at ul_open, then mount a scratch stick that carries
PIONEER/MODS/MODS.BIN and check the loader's state block:

  good   payload = 'write 0x600DCAFE to info+8, rts' -> status CALLED,
         mark set, loads 1, payload called exactly once
  badsum same payload, wrong checksum -> status SUM, mark 0, not called
  nofile stick without MODS.BIN -> status NOFILE, nothing called
  every case: the mount still completes (Browse opens the stick)

Usage: .venv/bin/python tools/usbloader_ul_test.py [good|badsum|nofile|all]
"""
import os
import shutil
import struct
import sys
import tempfile
os.environ.setdefault("XDJ_HLE_DSP", "1")
sys.path[:0] = [".", "tools"]
from emu.machine import Machine

OUT = "build/usbloader"
POOL, STOCK_OPEN = 0x08DE39A4, 0x08DDF53C
ST = ["IDLE", "NOFILE", "BADDEV", "SHORT", "MAGIC", "SIZE", "SUM", "CALLED"]
# position-independent marker payload, r4 = &ul_state:
#   mov.l @(4,pc),r1 ; mov.l r1,@(8,r4) ; rts ; nop ; .long 0x600DCAFE
PAYLOAD = struct.pack("<4HI", 0xD101, 0x1412, 0x000B, 0x0009, 0x600DCAFE)


def syms():
    s = {}
    for line in open(os.path.join(OUT, "ul.syms")):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def modsbin(payload, bad=False):
    sm = sum(payload) & 0xFFFFFFFF
    if bad:
        sm ^= 1
    hdr = struct.pack("<8I", 0x444F4D58, 1, len(payload), 0, sm, 0, 0, 0)
    return hdr + payload


def make_stick(case):
    d = tempfile.mkdtemp(prefix="ulstick_")
    os.makedirs(os.path.join(d, "PIONEER", "MODS"))
    # an ordinary settings file so the mount reader has real work too
    open(os.path.join(d, "PIONEER", "MYSETTING.DAT"), "wb").write(b"\0" * 160)
    if case != "nofile":
        open(os.path.join(d, "PIONEER", "MODS", "MODS.BIN"), "wb").write(
            modsbin(PAYLOAD, bad=(case == "badsum")))
    return d


def run_case(case):
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    from emu import panl
    s = syms()
    m = Machine.load(os.environ.get("CKPT", "cache/emu_realusb.pkl"))
    mem, dev = m.mem, m.mem.dev
    base, st = s["ul_head"], s["ul_state"]
    img = open(os.path.join(OUT, "ul.bin"), "rb").read()
    assert mem.r32(POOL) == STOCK_OPEN, f"pool not stock: {mem.r32(POOL):#x}"
    assert not any(mem.r8(base + i) for i in range(0, 0x10000, 4)), "cave not empty"
    for i, b in enumerate(img):
        mem.w8(base + i, b)
    mem.w32(POOL, s["ul_open"])                       # arm LAST
    stick = make_stick(case)
    u = dev._usb()
    u.unplug()
    m.cpu.run(limit=20_000_000)
    disk = open_disk(stick)
    dev.usb_disk = disk
    u.stick = MassStorage(disk)
    for _ in range(6):
        m.cpu.run(limit=250_000_000)
    w = [mem.r32(st + 4 * i) for i in range(9)]
    names = ["magic", "calls", "mark", "status", "tries", "loads", "len", "sum", "done"]
    v = dict(zip(names, w))
    print(f"[{case}] " + " ".join(f"{k}={v[k]:#x}" for k in names),
          "->", ST[v["status"]] if v["status"] < len(ST) else v["status"])
    # mount completed? Browse opens the stick (screen changes a lot)
    fb0 = bytes(mem.fb[0]) + bytes(mem.fb[1])
    dev.panel_frame = panl.build_frame(keys=["Browse"])
    m.cpu.run(limit=15_000_000)
    dev.panel_frame = panl.build_frame(keys=[])
    m.cpu.run(limit=60_000_000)
    fb1 = bytes(mem.fb[0]) + bytes(mem.fb[1])
    changed = sum(a != b for a, b in zip(fb0[::7], fb1[::7]))
    print(f"[{case}] browse after mount: {changed} sampled bytes changed")
    shutil.rmtree(stick, ignore_errors=True)
    ok = v["magic"] == 0x54534C55 and v["calls"] > 0 and changed > 1000
    if case == "good":
        ok &= v["status"] == 7 and v["mark"] == 0x600DCAFE and v["loads"] == 1 \
              and v["done"] == 1 and v["len"] == len(PAYLOAD)
    elif case == "badsum":
        ok &= v["status"] == 6 and v["mark"] == 0 and v["loads"] == 0
    else:
        ok &= v["status"] == 1 and v["mark"] == 0 and v["loads"] == 0
    print(f"[{case}] {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    cases = ["good", "badsum", "nofile"] if arg == "all" else [arg]
    ok = all([run_case(c) for c in cases])
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
