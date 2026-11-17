#!/usr/bin/env python3
"""usbloader_qemu_test.py -- the USB-stick loader, second opinion.

Every earlier loader proof ran in OUR emulator. This one boots the loader
update in QEMU (cdj-nxs2-qemu's XDJ-700 machine: an independent model of the
same hardware, real-time, real audio chip), so a bug shared by our emulator
and our loader cannot hide.

  1. build the loader update from the official 1.15 file exactly as the app
     does (permanent_build.build(stick_loader=True), all its own checks),
     then decode the program the deck would decompress from it
  2. QEMU boots that program with a stick that carries XDJMODS/PACK.BIN
     -> the loader is in RAM, the pack is read at mount, every record lands
     (ld_state status 4, mask == record count), and the mods it carries are
     live (hook words changed from stock)
  3. QEMU boots it with a stick WITHOUT a pack -> loader armed, status 1
     (no file), every hook word still stock, the deck runs normally

    .venv/bin/python tools/usbloader_qemu_test.py [PACK] [UPD]
"""
import os
import shutil
import struct
import sys
import tempfile
import threading
import time

sys.path[:0] = [".", "tools"]
PACK = sys.argv[1] if len(sys.argv) > 1 else "build/stick/XDJMODS/PACK.BIN"
UPD = sys.argv[2] if len(sys.argv) > 2 else "FIRMWARE/XDJ700_stock_v1.15.UPD"
SHOTS = os.environ.get("SHOTS", "build/usbloader_qemu")
WAIT = float(os.environ.get("WAIT", "120"))     # boot + mount budget (s)
os.makedirs(SHOTS, exist_ok=True)

import permanent_build as PB   # noqa: E402
from emu import fw as F        # noqa: E402

# hook words a full pack re-points (stock values from the firmware image)
HOOKS = {"overview paint": (0x08DBC5C8, 0x08C2C984),
         "2DG flush (pads/meter)": (0x08C92E20, 0x08E7D278)}


def build_kernel(tmp):
    upd = os.path.join(tmp, "XDJ700.UPD")
    PB.build(UPD, upd, stick_loader=True, log=lambda m: print("  build:", m))
    raw, main, panl = F.load_upd(upd)
    dec = F.decode_block1(main)[2]
    stock = F.decode_block1(F.load_upd(UPD)[1])[2]
    k = os.path.join(tmp, "kernel_loader.bin")
    open(k, "wb").write(dec)
    return k, dec, stock


def stick(tmp, name, pack):
    d = os.path.join(tmp, name)
    os.makedirs(os.path.join(d, "PIONEER"))
    open(os.path.join(d, "PIONEER", "MYSETTING.DAT"), "wb").write(b"\0" * 160)
    if pack:
        os.makedirs(os.path.join(d, "XDJMODS"))
        shutil.copyfile(pack, os.path.join(d, "XDJMODS", "PACK.BIN"))
    return d


def run(kernel, stick_dir, label):
    os.environ["XDJ_QEMU_KERNEL"] = kernel
    from emu import qemu_app
    app = qemu_app.QemuApp(headless=True)
    app.view = "screen"
    app.usb_path = stick_dir
    alive = [True]

    def ticker():
        t = time.time()
        while alive[0]:
            now = time.time()
            try:
                app.tick(now - t)
            except Exception as e:             # noqa: BLE001
                print("tick:", e, flush=True)
            t = now
            time.sleep(1 / 60)

    threading.Thread(target=ticker, daemon=True).start()
    s = PB._syms("build/usbloader/ld.syms")
    st = s["ld_state"]
    app._boot("Original", "qemu", None)
    t0, out = time.time(), None
    while time.time() - t0 < WAIT:
        time.sleep(2)
        if app.read32(st) != 0x5453444C:          # 'LDST' not there yet
            continue
        status = app.read32(st + 8)
        if status in (1, 2, 3, 4) and time.time() - t0 > 20:
            time.sleep(4)                         # let a pack finish
            break
    words = {k: app.read32(st + 4 * i) for i, k in enumerate(
        ("magic", "calls", "status", "tries", "mask", "bad", "why", "bytes", "done"))}
    words["pool"] = app.read32(PB.LD_POOL)
    words["head"] = app.read32(s["ld_head"])
    words["hooks"] = {k: app.read32(a) for k, (a, _) in HOOKS.items()}
    words["mode"] = app.read32(0x0B6C4264)
    words["secs"] = round(time.time() - t0)
    try:
        r = app.rfb
        if r is not None:
            from PIL import Image
            with r.lock:
                Image.fromarray(r.fb[:480, :800].copy()).save(
                    os.path.join(SHOTS, label + ".png"))
    except Exception as e:                      # noqa: BLE001
        print("shot:", e)
    alive[0] = False
    app.close()
    time.sleep(2)
    print(label, words, flush=True)
    return words


def main():
    tmp = tempfile.mkdtemp(prefix="ldq_")
    ok = True

    def check(name, good):
        nonlocal ok
        print("%s %s" % ("ok  " if good else "FAIL", name), flush=True)
        ok &= bool(good)

    try:
        kernel, dec, stock = build_kernel(tmp)
        n = struct.unpack_from("<H", open(PACK, "rb").read(), 10)[0]
        s = PB._syms("build/usbloader/ld.syms")

        w = run(kernel, stick(tmp, "with_pack", PACK), "1_with_pack")
        check("QEMU booted the loader update (loader header in RAM)",
              w["head"] == struct.unpack_from("<I", dec, s["ld_head"] - PB.BASE)[0]
              if s["ld_head"] - PB.BASE < len(dec) else w["magic"] == 0x5453444C)
        check("open pool armed -> ld_open", w["pool"] == s["ld_open"])
        check("pack read at mount (status 4)", w["status"] == 4)
        check(f"every record applied ({w['mask']}/{n})", w["mask"] == n)
        check("no payload rejected", w["bad"] == 0)
        for k, (a, stock_v) in HOOKS.items():
            check(f"{k} hook re-pointed by the pack", w["hooks"][k] not in (None, stock_v))

        w = run(kernel, stick(tmp, "no_pack", None), "2_no_pack")
        check("no pack: loader armed", w["pool"] == s["ld_open"])
        check("no pack: status 1 (no file), nothing applied",
              w["status"] == 1 and not w["mask"])
        for k, (a, stock_v) in HOOKS.items():
            check(f"no pack: {k} hook still stock", w["hooks"][k] == stock_v)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("screens in", SHOTS)
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
