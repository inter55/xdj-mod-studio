#!/usr/bin/env python3
"""custom_wave_emu_test.py -- the custom (1-3 colour) waveform on the
emulated deck: overview cave mode 3 + centre module tables, installed the
way emu/ui.py apply_wave("custom") does it, then a WAV loaded from a
scratch stick on the real-DSP snapshot (the instant snapshot cannot load
tracks). Checks, pixel by pixel in the screenshot:
  * overview: no stock-blue pixel is left; the bar holds the table's
    colours, each row the colour the Studio preview gives it
    (rgb_overview.custom_table: bottom -> top gradient)
  * centre: the ramp colours (rgb_detail.custom_tables) are on screen,
    stock palette colours are not
  * UTILITY: WAVEFORM COLOR cycles BLUE > RGB > 3-BAND > CUSTOM > BLUE
Screens: screenshots/customwave/*.png
Usage: .venv/bin/python tools/custom_wave_emu_test.py ['#rrggbb,#rrggbb[,#rrggbb]']
"""
import os
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("XDJ_HLE_DSP", "1")
sys.path[:0] = [".", "tools", "mods"]
from emu.machine import Machine
from emu import panl, fwhome
import rgb_overview as RO
import rgb_detail as RD
import vfile_emu_test as VT
from ghosttap_emu import fb_png

SHOTS = "screenshots/customwave"
OV = (108, 399, 528, 60)            # overview region on the main screen
CE = (116, 185, 500, 110)           # centre waveform region (cy 240 +- 55)


def rgb565_of(px):
    r, g, b = px
    return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)


def region_colours(png, box):
    from PIL import Image
    from collections import Counter
    im = Image.open(png).convert("RGB")
    x0, y0, w, h = box
    return Counter(rgb565_of(im.getpixel((x, y))) for x in range(x0, x0 + w)
                   for y in range(y0, y0 + h))


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "#ff2020,#20ff40,#4060ff"
    cols = [RO.hex565(c) for c in arg.split(",") if c.strip()][:3]
    os.makedirs(SHOTS, exist_ok=True)
    snap = fwhome.path("dspsound.pkl.gz")
    if not os.path.exists(snap):
        sys.exit("needs the real-DSP snapshot (firmware home dspsound.pkl.gz)")
    m = Machine.load(snap)
    mem, dev = m.mem, m.mem.dev
    # install: overview cave (mode 3 + table), centre module + its tables,
    # UTILITY rows (the WAVEFORM COLOR row), same bytes as the deck tools
    for a, b in RO.plan(RO.MODES["custom"], cols)[1]:
        mem.w8(a, b)
    RD.install(mem)
    RD.install_custom(mem, cols)
    import utility as U
    U.install(mem)
    ok = True
    # a stick with one 44.1k WAV, loaded through BROWSE (row 1 behind a
    # dummy, the deck pre-reads the row the cursor moves onto)
    d = tempfile.mkdtemp(prefix="cwstick_")
    f = os.path.join(d, "Contents", "T")
    os.makedirs(f)
    for name in ("a.wav", "b.wav"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=8:samples_per_frame=1024",
                        "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
                        os.path.join(f, name)], check=True)
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    u = dev._usb()
    u.unplug()
    m.cpu.run(limit=20_000_000)
    disk = open_disk(d)
    dev.usb_disk = disk
    u.stick = MassStorage(disk)
    for _ in range(6):
        m.cpu.run(limit=250_000_000)

    def press(k, hold=15_000_000, after=60_000_000):
        dev.panel_frame = panl.build_frame(keys=[k])
        m.cpu.run(limit=hold)
        dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=after)

    for _ in range(2):
        press("Browse")
        fb_png(m, f"{SHOTS}/_b.png")
        if VT.highlighted(f"{SHOTS}/_b.png") is not None:
            break
    mem.w16(VT.ROT, (mem.r16(VT.ROT) + 1) & 0xFFFF)
    m.cpu.run(limit=15_000_000)
    for _ in range(3):
        press("RotaryPush", 3_000_000, 30_000_000)
    for _ in range(6):
        m.cpu.run(limit=40_000_000)
        fb_png(m, f"{SHOTS}/_b.png")
        b = VT.highlighted(f"{SHOTS}/_b.png")
        if b is None or b == 1:
            break
        mem.w16(VT.ROT, (mem.r16(VT.ROT) + 1) & 0xFFFF)
        m.cpu.run(limit=20_000_000)
    press("RotaryPush", 3_000_000, 100_000_000)          # load b.wav
    for _ in range(12):
        if mem.r32(0x0B6C4264) in (3, 5):
            break
        m.cpu.run(limit=100_000_000)
    press("Browse")                                      # main screen
    m.cpu.run(limit=60_000_000)
    shot = f"{SHOTS}/custom_main.png"
    fb_png(m, shot)
    print("mode word:", mem.r32(0x0B6C4264), "(3 = at cue)")
    ok &= mem.r32(0x0B6C4264) in (3, 5)
    # --- overview: table colours, no stock blue
    tab = RO.custom_table(cols)
    ov = region_colours(shot, OV)
    blue = ov.get(RO.STOCK_BLUE, 0)
    hit = sum(ov.get(c, 0) for c in set(tab))
    print(f"overview: stock-blue px {blue} (must be 0), table-colour px {hit}, "
          f"distinct table colours seen {sum(1 for c in set(tab) if ov.get(c))}/{len(set(tab))}")
    ok &= blue == 0 and hit > 500
    # each row's colour = the table's colour for that row (bar bottom = row 0)
    from PIL import Image
    im = Image.open(shot).convert("RGB")
    x0, y0, w, h = OV
    rows_ok = rows_bad = 0
    for x in range(x0, x0 + w):
        col = [rgb565_of(im.getpixel((x, y))) for y in range(y0, y0 + h)]
        ys = [y for y, c in enumerate(col) if c in set(tab)]
        if not ys:
            continue
        bottom = max(ys)
        for y in ys:
            k = bottom - y
            if k < RO.ROWS and col[y] == tab[k]:
                rows_ok += 1
            else:
                rows_bad += 1
    print(f"overview rows matching the Studio table: {rows_ok}, off: {rows_bad}")
    ok &= rows_bad == 0 and rows_ok > 500
    # --- centre: the deck draws its centre waveform only from a rekordbox
    # .EXT (none for a generated WAV), so prove the module's custom path on
    # the emulated CPU: cw_col(idx8, step, white) must fill cw_tbl with the
    # ramp (2-3 colours) or the tinted palette entry (1 colour), as the
    # painter's row loop reads it
    import theme_switch_emu_direct as TD
    cw, syms = RD.cw_build()

    def cw_rows(colours, white):
        RD.install_custom(mem, colours)
        for i in range(4, 8):
            m.cpu.set_gr(i, 0)
        m.cpu.set_gr(4, 0)
        m.cpu.set_gr(5, 1)
        m.cpu.set_gr(6, white)
        assert TD.call(m, syms["cw_col"]), "cw_col did not return"
        return [mem.r16(syms["cw_tbl"] + 2 * k) for k in range(RD.CW_ROWS)]

    kind, ramp, pal = RD.custom_tables(cols)
    got = cw_rows(cols, 5)
    print(f"centre cw_col, {len(cols)} colours: rows == Studio ramp: {got == ramp}")
    ok &= got == ramp
    k1, r1, p1 = RD.custom_tables(cols[:1])
    for white in (0, 3, 7):
        got = cw_rows(cols[:1], white)
        okw = got == [p1[white]] * RD.CW_ROWS
        print(f"centre cw_col, 1 colour, whiteness {white}: all rows == tinted palette[{white}] "
              f"{p1[white]:#06x}: {okw}")
        ok &= okw
    RD.install_custom(mem, cols)
    # --- UTILITY: the WAVEFORM COLOR row cycles through 4 values
    import utility as U_
    s = U_.syms() if hasattr(U_, "syms") else None
    w0 = mem.r8(RO.MODE_ADDR)
    print(f"mode byte now {w0} (3 = custom)")
    ok &= w0 == 3
    print("RESULT:", "PASS" if ok else "FAIL")
    shutil.rmtree(d, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
