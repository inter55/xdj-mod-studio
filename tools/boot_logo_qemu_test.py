#!/usr/bin/env python3
"""boot_logo_qemu_test.py -- the custom boot logo, booted in QEMU.

Builds the update file's program the same way permanent_build does (the
"With mods" image, with --boot-logo baked in), boots it in QEMU and grabs
frames during boot. PASS = a boot frame matches our test picture where the
stock Pioneer DJ logo would be, and the deck still reaches its home screen.

    .venv/bin/python tools/boot_logo_qemu_test.py [PICTURE]

Without PICTURE it draws its own (white text on dark blue, plus a red bar), so
nothing of Pioneer's is used for the comparison.
"""
import os
import sys
import tempfile
import threading
import time

import numpy as np
from PIL import Image, ImageDraw

sys.path[:0] = [".", "tools"]
import permanent_build as PB   # noqa: E402
from emu import fwhome         # noqa: E402

SHOTS = os.environ.get("SHOTS", "build/boot_logo_qemu")
os.makedirs(SHOTS, exist_ok=True)


def test_picture(path):
    im = Image.new("RGB", (900, 150), (10, 30, 90))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 120, 900, 150), fill=(220, 20, 20))
    d.text((40, 30), "XDJ MOD STUDIO  BOOT LOGO TEST", fill=(255, 255, 255))
    im.save(path)


def main():
    tmp = tempfile.mkdtemp(prefix="logoq_")
    pic = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tmp, "logo.png")
    if len(sys.argv) <= 1:
        test_picture(pic)
    want = np.frombuffer(PB.logo_pixels(pic), "<u2").reshape(PB.LOGO_H, PB.LOGO_W)
    dec = open(fwhome.path("blk1.bin"), "rb").read()
    img = PB.mods_image(dec, "rgb", extras=("utility",), boot_logo=pic,
                        log=lambda m: print("  build:", m))
    a = PB.LOGO_ADDR - PB.BASE
    baked = np.frombuffer(img[a:a + PB.LOGO_W * PB.LOGO_H * 2], "<u2").reshape(PB.LOGO_H, PB.LOGO_W)
    ok = True

    def check(name, good):
        nonlocal ok
        print("%s %s" % ("ok  " if good else "FAIL", name), flush=True)
        ok &= bool(good)

    check("picture written into the image", np.array_equal(baked, want))
    k = os.path.join(tmp, "kernel.bin")
    open(k, "wb").write(img)
    os.environ["XDJ_QEMU_KERNEL"] = k
    from emu import qemu_app
    app = qemu_app.QemuApp(headless=True)
    app.view = "screen"
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
    app._boot("Original", "qemu", None)
    # the logo is drawn somewhere on the 800x480 screen: find it by matching
    # our 600x100 picture against each frame (exact RGB565 -> RGB888 compare
    # is too strict after the display path, so use mean abs error)
    wr = (((want >> 11) & 31) * 255 // 31).astype(float)
    wg = (((want >> 5) & 63) * 255 // 63).astype(float)
    wb = ((want & 31) * 255 // 31).astype(float)
    best, seen_at, frames = 1e9, None, 0
    t0 = time.time()
    while time.time() - t0 < 60:
        time.sleep(0.25)
        r = app.rfb
        if r is None:
            continue
        with r.lock:
            fb = r.fb[:480, :800].astype(float)
        frames += 1
        for y in range(0, 480 - PB.LOGO_H + 1, 2):
            row = fb[y:y + PB.LOGO_H]
            for x in (100,):                    # centred: (800 - 600) / 2
                c = row[:, x:x + PB.LOGO_W]
                err = (np.abs(c[..., 0] - wr) + np.abs(c[..., 1] - wg)
                       + np.abs(c[..., 2] - wb)).mean() / 3
                if err < best:
                    best = err
                    if err < 12 and seen_at is None:
                        seen_at = (round(time.time() - t0, 1), y)
                        Image.fromarray(fb.astype("uint8")).save(
                            os.path.join(SHOTS, "boot_logo_frame.png"))
        if seen_at and time.time() - t0 > seen_at[0] + 25:
            break
    with app.rfb.lock:
        last = app.rfb.fb[:480, :800].copy()
    Image.fromarray(last).save(os.path.join(SHOTS, "after_boot.png"))
    mode = app.read32(0x0B6C4264)
    alive[0] = False
    app.close()
    print(f"frames looked at: {frames}, best match error {best:.1f}/255, "
          f"logo seen at {seen_at}")
    check("our logo shown during boot", seen_at is not None)
    check("deck still boots past the logo (screen changed after it)",
          seen_at is not None and np.abs(last.astype(float)[seen_at[1]:seen_at[1] + 100, 100:700, 0]
                                         - wr).mean() > 20)
    print("screens in", SHOTS, "| deck mode word", mode)
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
