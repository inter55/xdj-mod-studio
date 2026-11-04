"""fwhome.py -- everything derived from Pioneer's firmware lives HERE, on
the user's own Mac, generated from the user's own official update file.

The open-source project ships NO firmware and nothing derived from it.
On first run Mod Studio asks for the official XDJ-700 v1.15 update
(XDJ700*.UPD from Pioneer DJ's support site), checks it is exactly that
file (SHA-256), and builds the local firmware home:

  MAIN.img         firmware image (from the .UPD container)
  blk1.bin         decoded application block (theme tables, tools)
  mainscreen.pkl.gz  emulator snapshot: booted, demo stick mounted
  screens.npz      main + PERFORM framebuffers (theme previews)
  pad_edges.npy    hot-cue pad edge columns (PERFORM pads art)
  pad_digits.npz   BEAT LOOP pad digits 1 2 4 8 16 (pad font)

Location: $XDJ_FW_DIR, else ~/Library/Application Support/XDJ Mod Studio/
firmware. A developer checkout falls back to the old repo paths (work/,
cache/, studio/assets/, tools/data/) so nothing changes for development.

  python -m emu.fwhome setup PATH/TO/XDJ700.UPD     (also used by Studio)
  python -m emu.fwhome status
"""
import gzip
import hashlib
import json
import os
import sys

from .paths import repo_root

KNOWN = {
    # official XDJ-700 firmware 1.15 update file
    "73edec9802da51672257c2599efc04209dc92478fcbaa1a0425b3b122e33f99c": "1.15",
}
FILES = ("MAIN.img", "blk1.bin", "mainscreen.pkl.gz", "dspsound.pkl.gz",
         "screens.npz",
         "pad_edges.npy", "pad_digits.npz")
# dspsound (real-DSP snapshot) is built by setup() alongside mainscreen
# (a second stickless boot, ~5 min once): both snapshots must exist.
READY_FILES = FILES
# where a developer checkout keeps the same things (fallbacks)
DEV = {
    "MAIN.img": "work/MAIN.img",
    "blk1.bin": "cache/blk1.bin",
    "mainscreen.pkl.gz": "cache/emu_mainscreen.pkl.gz",
    "dspsound.pkl.gz": "cache/emu_dsp_sound.pkl.gz",
    "screens.npz": "studio/assets/screens.npz",
    "pad_edges.npy": "tools/data/hotcue_pad_edges.npy",
    "pad_digits.npz": "tools/data/stock_pad_digits.npz",
}
# crop positions on the emulated deck's own screens (facts, not pixels)
EDGE_ROWS = (232, 295)                 # hot-cue row
EDGE_COLS = ((116, 126), (203, 215))   # stock pad A: left edge, right edge + dither
DIGITS = {"1": 201, "2": 285, "4": 369, "8": 453, "16": 537}
DIGIT_Y, DIGIT_H, DIGIT_W = 154, 53, 79
PERFORM_TAP = (709, 37)


def home():
    from .paths import app_support_dir
    d = os.environ.get("XDJ_FW_DIR") or os.path.join(app_support_dir(),
                                                      "firmware")
    return d


def path(name):
    """Best existing location of a firmware-derived file, or the firmware
    home path (missing) so callers can say what to do."""
    h = os.path.join(home(), name)
    if os.path.exists(h):
        return h
    dev = os.path.join(repo_root(), DEV.get(name, name))
    if name == "mainscreen.pkl.gz" and not os.path.exists(dev):
        raw = dev[:-3]
        if os.path.exists(raw):
            return raw
    return dev if os.path.exists(dev) else h


def ready():
    return all(os.path.exists(path(n)) for n in READY_FILES)


def status():
    return {n: path(n) if os.path.exists(path(n)) else None for n in FILES}


def check_upd(p):
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return KNOWN.get(h), h


def _fb(m):
    """current composed framebuffer as uint16 RGB565 (800x480)"""
    import numpy as np
    sys.path.insert(0, os.path.join(repo_root(), "tools"))
    from ghosttap_emu import fb_png
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "fb.png")
        fb_png(m, p)
        a = np.array(Image.open(p).convert("RGB")).astype(np.uint16)
    return ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)


def setup(upd, progress=lambda f, note: None, steps=None):
    """Build the firmware home from the user's own update file."""
    import numpy as np
    ver, h = check_upd(upd)
    if not ver:
        raise ValueError(f"not the XDJ-700 v1.15 update file (sha256 {h[:12]}...). "
                         "Download XDJ-700 firmware 1.15 from Pioneer DJ's site.")
    # the second boot (real audio chip) decodes DSP code with the tic6x
    # objdump: check it NOW, not after minutes of booting at 84 %
    from .c6x import decode as C6D
    if not C6D.available():
        raise RuntimeError(C6D.HOWTO)
    d = home()
    os.makedirs(d, exist_ok=True)
    progress(0.02, "unpacking the update file")
    from . import fw as F
    raw, main, panl = F.load_upd(upd)
    flash = F.flash_image(main)
    open(os.path.join(d, "MAIN.img"), "wb").write(flash)
    _, _, dec = F.decode_block1(main)
    open(os.path.join(d, "blk1.bin"), "wb").write(dec)

    progress(0.08, "starting the deck emulator (first boot, a few minutes)")
    from .machine import Machine
    from . import panl as P
    m = Machine(flash)
    dev = m.mem.dev
    dev.panel_frame = P.build_frame()
    total = steps or int(os.environ.get("XDJ_BOOT_STEPS", "6000000000"))
    chunk = 250_000_000
    done = 0
    while done < total:                      # no stick: the user inserts
        m.cpu.run(limit=chunk)               # their own library in Studio
        done += chunk
        progress(0.08 + 0.72 * done / total, "booting the deck emulator")
    progress(0.82, "saving the emulator snapshot")
    snap = os.path.join(d, "mainscreen.pkl")
    m.save(snap)
    with open(snap, "rb") as f, gzip.open(snap + ".gz", "wb", 6) as g:
        g.write(f.read())
    os.unlink(snap)

    progress(0.84, "booting again with the real audio chip (loads must play)")
    os.environ["XDJ_DSP_EMU"] = "1"           # read by SH7724 at construction
    try:
        m2 = Machine(flash)
        m2.mem.dev.panel_frame = P.build_frame()
        done2 = 0
        while done2 < total:                  # stickless, like above
            m2.cpu.run(limit=chunk)
            done2 += chunk
            progress(0.84 + 0.06 * done2 / total,
                     "booting with the real audio chip")
    finally:
        del os.environ["XDJ_DSP_EMU"]
    dsnap = os.path.join(d, "dspsound.pkl")
    m2.save(dsnap)
    with open(dsnap, "rb") as f, gzip.open(dsnap + ".gz", "wb", 6) as g:
        g.write(f.read())
    os.unlink(dsnap)

    progress(0.92, "capturing the deck's screens")
    main_fb = _fb(m)

    def tap(xy, hold=12_000_000, after=40_000_000):
        dev.panel_frame = P.build_frame(touch=xy)
        m.cpu.run(limit=hold)
        dev.panel_frame = P.build_frame()
        m.cpu.run(limit=after)
    tap(PERFORM_TAP)
    perf_fb = _fb(m)
    np.savez_compressed(os.path.join(d, "screens.npz"), main=main_fb, perform=perf_fb)
    y0, y1 = EDGE_ROWS
    edges = np.hstack([perf_fb[y0:y1, a:b] for a, b in EDGE_COLS])
    np.save(os.path.join(d, "pad_edges.npy"), edges.astype(np.uint16))
    np.savez_compressed(os.path.join(d, "pad_digits.npz"), **{
        k: perf_fb[DIGIT_Y:DIGIT_Y + DIGIT_H, x:x + DIGIT_W].astype(np.uint16)
        for k, x in DIGITS.items()})
    progress(0.97, "building the theme tables")
    try:                                     # on-deck theme switching tables
        sys.path.insert(0, os.path.join(repo_root(), "tools"))
        sys.path.insert(0, os.path.join(repo_root(), "mods"))
        import theme_switch as TS
        open(os.path.join(d, "themesw_tables.bin"), "wb").write(TS.blob(TS.default_names()))
    except Exception as e:                   # emulator theme switching only
        print("theme tables skipped:", e)
    json.dump({"version": ver, "sha256": h}, open(os.path.join(d, "firmware.json"), "w"))
    progress(1.0, "firmware ready")
    return d


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        d = setup(sys.argv[2], lambda f, n: print(f"{f * 100:5.1f}%  {n}", flush=True))
        if "--dev" in sys.argv:              # developer checkout: same files
            import shutil                    # at the old repo paths (git-ignored)
            for n, rel in DEV.items():
                src = os.path.join(d, n)
                if not os.path.exists(src):  # e.g. dspsound: not built here
                    continue
                dst = os.path.join(repo_root(), rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            print("developer copies written (work/, cache/, studio/assets/, tools/data/)")
        print("RESULT: ready at", d)
        return 0
    print(json.dumps(status(), indent=1), "\nready:", ready())
    return 0


if __name__ == "__main__":
    sys.exit(main())
