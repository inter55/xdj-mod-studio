#!/usr/bin/env python3
"""usbloader_stick_test.py -- milestone 2b: the pack loader reading
/XDJMODS/PACK.BIN off a USB stick at mount, in the emulated XDJ-700.
Nothing here touches a deck.

The FAT filesystem is fake but complete: emu.usbdisk.FolderDisk builds a
FAT32 volume on the fly from a scratch folder and the emulated USB
mass-storage device serves it, so the REAL firmware USB host + FAT
driver mounts it and our loader reads the pack through the deck's own
open/fread (build/usbloader/ld.bin on the surveyed open pool 0x08DE39A4).

From cache/emu_realusb.pkl (booted, HLE, no mods): write ld.bin at its
link address, arm the pool word LAST, mount a stick, check ld_state and
the RAM homes:

  good      pm+stems+hotcue8 -> status DONE, mask 0b111, homes byte-exact
  badbody   pm body flipped  -> pm not in mask, others exact
  truncated file cut inside the stems body -> pm only; nothing past
  badmagic  -> status HEADER, homes all zero, retry stays allowed
  nofile    -> status NOFILE, homes zero
  live      pm home already holds code (e.g. cable install) -> pm
            skipped and untouched, others exact
  every case: the mount completes (Browse opens the stick)

Usage: .venv/bin/python tools/usbloader_stick_test.py [case|all]
"""
import os
import shutil
import sys
import tempfile
os.environ.setdefault("XDJ_HLE_DSP", "1")
sys.path[:0] = [".", "tools"]
from emu.machine import Machine
import pack_builder as P

OUT = "build/usbloader"
POOL, STOCK_OPEN = 0x08DE39A4, 0x08DDF53C
ST = ["IDLE", "NOFILE", "BADDEV", "HEADER", "DONE"]
HOMES = {"pm": (0x09798000, "build/phasemeter/pm.bin"),
         "stems": (0x09836000, "build/stems/sm.bin"),
         "hotcue8": (0x09790000, "build/hotcue8/hc.bin")}
ORDER = ["pm", "stems", "hotcue8"]


def syms():
    s = {}
    for line in open(os.path.join(OUT, "ld.syms")):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def pack():
    """rev-2 pack (tools/stick_pack.py): the format the on-deck recorder
    writes and build/usbloader/lc.c accepts. rev-1 (pack_builder.py) is
    refused at the header now -- an older draft of this test used it and
    stopped passing when the loader moved to rev 2."""
    import stick_pack as SP
    runs = []
    for n in ORDER:
        data = open(HOMES[n][1], "rb").read()
        runs.append((HOMES[n][0], bytes(len(data)), data))   # empty cave
    return SP.build(runs)


def make_stick(case, good):
    d = tempfile.mkdtemp(prefix="ldstick_")
    os.makedirs(os.path.join(d, "PIONEER"))
    open(os.path.join(d, "PIONEER", "MYSETTING.DAT"), "wb").write(b"\0" * 160)
    if case == "nofile":
        return d, None
    data = bytearray(good)
    if case in ("badbody", "truncated"):
        ents, _ = P.parse(good)          # rev-1 only; those cases stay rev-1
    if case == "badbody":
        data[0x70 + 5] ^= 0xFF                      # inside pm's body
    elif case == "badmagic":
        data[0:4] = b"XXXX"
    elif case == "truncated":
        body = ents[1]["body"]                      # stems
        at = bytes(data).find(body)
        assert at > 0
        data = data[:at + len(body) // 2]
    os.makedirs(os.path.join(d, "XDJMODS"))
    open(os.path.join(d, "XDJMODS", "PACK.BIN"), "wb").write(bytes(data))
    return d, data


def run_case(case, good, expect):
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    from emu import panl
    s = syms()
    m = Machine.load(os.environ.get("CKPT", "cache/emu_realusb.pkl"))
    mem, dev = m.mem, m.mem.dev
    base, st = s["ld_head"], s["ld_state"]
    img = open(os.path.join(OUT, "ld.bin"), "rb").read()
    assert mem.r32(POOL) == STOCK_OPEN, f"pool not stock: {mem.r32(POOL):#x}"
    assert not any(mem.r8(base + i) for i in range(0, 0x10000)), "cave not empty"
    for n, (h, _f) in HOMES.items():
        assert not any(mem.r8(h + i) for i in range(len(expect[n]))), f"{n} home not empty"
    for i, b in enumerate(img):
        mem.w8(base + i, b)
    if case == "live":
        mem.w32(HOMES["pm"][0] + 0x40, 0xDEADBEEF)
    mem.w32(POOL, s["ld_open"])                       # arm LAST
    stick, _ = make_stick(case, good)
    u = dev._usb()
    u.unplug()
    m.cpu.run(limit=20_000_000)
    disk = open_disk(stick)
    dev.usb_disk = disk
    u.stick = MassStorage(disk)
    for _ in range(6):
        m.cpu.run(limit=250_000_000)
    names = ["magic", "calls", "status", "tries", "mask", "bad", "why",
             "bytes", "done"]
    v = dict(zip(names, [mem.r32(st + 4 * i) for i in range(len(names))]))
    print(f"[{case}] " + " ".join(f"{k}={v[k]:#x}" for k in names),
          "->", ST[v["status"]] if v["status"] < len(ST) else v["status"])
    exact = {n: bytes(mem.r8(HOMES[n][0] + i) for i in range(len(expect[n])))
             == expect[n] for n in HOMES}
    empty = {n: not any(mem.r8(HOMES[n][0] + i) for i in range(len(expect[n])))
             for n in HOMES}
    print(f"[{case}] exact={exact} empty={empty}")
    fb0 = bytes(mem.fb[0]) + bytes(mem.fb[1])
    dev.panel_frame = panl.build_frame(keys=["Browse"])
    m.cpu.run(limit=15_000_000)
    dev.panel_frame = panl.build_frame(keys=[])
    m.cpu.run(limit=60_000_000)
    fb1 = bytes(mem.fb[0]) + bytes(mem.fb[1])
    changed = sum(a != b for a, b in zip(fb0[::7], fb1[::7]))
    print(f"[{case}] browse after mount: {changed} sampled bytes changed")
    shutil.rmtree(stick, ignore_errors=True)
    ok = v["magic"] == 0x5453444C and v["calls"] > 0 and changed > 1000
    if case == "good":
        ok &= v["status"] == 4 and v["mask"] == 0b111 and v["done"] == 1 \
            and v["bytes"] == len(good) and all(exact.values())
    elif case == "badbody":
        ok &= v["status"] == 4 and v["mask"] == 0b110 \
            and exact["stems"] and exact["hotcue8"]
    elif case == "truncated":
        ok &= v["status"] == 4 and v["mask"] == 0b001 and exact["pm"] \
            and empty["hotcue8"]
    elif case == "badmagic":
        ok &= v["status"] == 3 and v["mask"] == 0 and v["done"] == 0 \
            and all(empty.values())
    elif case == "nofile":
        ok &= v["status"] == 1 and v["done"] == 0 and all(empty.values())
    elif case == "live":
        kept = mem.r32(HOMES["pm"][0] + 0x40) == 0xDEADBEEF
        ok &= v["status"] == 4 and v["mask"] == 0b110 and v["why"] == 3 \
            and kept and exact["stems"] and exact["hotcue8"]
    print(f"[{case}] {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    good = pack()
    expect = {n: open(HOMES[n][1], "rb").read() for n in ORDER}
    # the fault cases (bad body / truncated / old byte) are rev-2 native in
    # tools/usbloader_pack2_test.py; they are not re-run here on a rev-2 pack
    allc = ["good"]
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    cases = allc if arg == "all" else [arg]
    ok = all([run_case(c, good, expect) for c in cases])
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
