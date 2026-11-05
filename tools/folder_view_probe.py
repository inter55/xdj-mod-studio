#!/usr/bin/env python3
"""folder_view_probe.py -- does BROWSE > FOLDER show a file the app wrote
AFTER the stick's rekordbox export? (Elk's stems report, 2026-10-27)

Mini stick: the real library DB (export.pdb etc from the dev's stick),
one real track + its ANLZ, and a "(STEMS).wav" next to the audio (what
Track Builder writes). Mount, Browse, walk the category selector, and
screenshot every step. The question: does the folder tree list the
stems file although export.pdb knows nothing about it?

Run: .venv/bin/python tools/folder_view_probe.py
"""
import os
import shutil
import sys

sys.path[:0] = [".", "tools"]
LIB = os.path.expanduser("~/Downloads/Rekordbox stuff")
ANLZ = "PIONEER/USBANLZ/P000/0002D83A"
TRACK = ("Contents/Aaron Carl/The Greatest House Acappellas/"
         "aaron_carl_-_house_music-sq.mp3")
STEMS_SRC = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                         "stemstest", "Probe (STEMS).wav")
SNAP = os.environ.get("SNAP", "cache/emu_dsp_mounted6.pkl")
ROT = 0x0B6C7B48
OUT = "screenshots/folderprobe"


def build(stick):
    if os.path.exists(stick):
        shutil.rmtree(stick)
    os.makedirs(os.path.dirname(os.path.join(stick, TRACK)))
    shutil.copy2(os.path.join(LIB, TRACK), os.path.join(stick, TRACK))
    shutil.copy2(STEMS_SRC,
                 os.path.join(stick, TRACK).replace(".mp3", " (STEMS).wav"))
    for rel in ("PIONEER/rekordbox", "PIONEER/USBANLZ/USBMNG.DAT",
                "PIONEER/USBANLZ/" + ANLZ.split("/", 2)[2],
                "PIONEER/MYSETTING.DAT", "PIONEER/MYSETTING2.DAT",
                "PIONEER/DEVSETTING.DAT", "PIONEER/DJMMYSETTING.DAT",
                "PIONEER/djprofile.nxs"):
        src, dst = os.path.join(LIB, rel), os.path.join(stick, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)


def main():
    from emu.machine import Machine
    from emu import panl
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    from ghosttap_emu import fb_png

    stick = os.path.join(os.environ.get("TMPDIR", "/tmp"), "folderprobe")
    if not os.path.exists(STEMS_SRC):
        print("build the probe stems file first (see tools/centre/cw_unit"
              "test or any stems4 build)")
        return 1
    build(stick)
    print("mini stick:", stick)
    os.makedirs(OUT, exist_ok=True)
    m = Machine.load(SNAP)
    mem, dev = m.mem, m.mem.dev

    def key(k, n=12_000_000, tag=""):
        dev.panel_frame = panl.build_frame(keys=[k])
        m.cpu.run(limit=3_000_000)
        dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=n)
        fb_png(m, f"{OUT}/{tag or k}.png")

    u = dev._usb()
    u.unplug()
    m.cpu.run(limit=20_000_000)
    dev.usb_disk = open_disk(stick)
    u.stick = MassStorage(dev.usb_disk)
    for _ in range(10):
        m.cpu.run(limit=250_000_000)
    fb_png(m, f"{OUT}/mounted.png")
    key("Browse", tag="browse")
    for i in range(8):
        mem.w16(ROT, (mem.r16(ROT) + 1) & 0xFFFF)
        m.cpu.run(limit=12_000_000)
        dev.panel_frame = panl.build_frame(keys=["RotaryPush"])
        m.cpu.run(limit=3_000_000)
        dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=25_000_000)
        fb_png(m, f"{OUT}/push{i}.png")
    print("done; screenshots in", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
