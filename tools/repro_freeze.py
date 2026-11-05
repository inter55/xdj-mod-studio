#!/usr/bin/env python3
"""repro_freeze.py -- recreate deck session 5 in the emulator: ALL mods as
installed on the deck (beat jump 16, RGB overview + snapshot, centre,
versions, MY SETTINGS rows, PERFORM pads, hot cue pages @0x09790000 chained
on the flush hook, theme switching @0x09770000 + relinked pointers), a track
loaded, then PLAY -- and look for a hang: where the CPU spends its time,
whether the screen keeps updating, and whether the centre waveform draws.

  .venv/bin/python tools/repro_freeze.py [--only core|pads|hc|themes|touchfx ...]
  WMODE=3band ...   waveforms in 3-band (a PWV7 section is added to the
                    loaded track's section index so the centre draws it)
"""
import collections
import os
import struct
import subprocess
import tempfile
import sys

os.environ.setdefault("XDJ_HLE_DSP", "1")
os.environ.setdefault("HOTCUE8_HOME_PROVEN", "1")      # install like the deck had it
os.environ.setdefault("THEMESW_HOME_PROVEN", "1")
sys.path[:0] = [".", "tools", "mods"]
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402

CPU = 500_000_000
WMODE = os.environ.get("WMODE", "rgb")
P7_EXT = os.path.expanduser("~/Downloads/Rekordbox stuff/PIONEER/USBANLZ/"
                            "P000/0002D83A/ANLZ0000.2EX")


def install(m, parts):
    mem = m.mem
    import rgb_overview as RO
    import rgb_detail as RD
    import vredirect as VR
    import mysettings as MS
    import perform_pads as PP
    import theme_switch as TS
    if "core" in parts:
        parts |= {"ov", "centre", "vr", "ms"}
    if "ov" in parts:
        for a, b in RO.plan(RO.MODES[WMODE])[1]:
            mem.w8(a, b)
    if "centre" in parts:
        RD.install(mem)
        if WMODE == "3band":
            sec = RD.section_raw(P7_EXT, b"PWV7")
            for i, b in enumerate(sec):
                mem.w8(0x0F100000 + i, b)
            ix = (mem.r32(RO.CTX_TAB) & 0x1FFFFFFF) + RO.IDX_OFF
            n = mem.r32(ix)
            for o, v in ((0, 0), (4, RD.TAG_PWV7), (8, len(sec)), (12, 0x0F100000)):
                mem.w32(ix + 4 + 16 * n + o, v)
            mem.w32(ix, n + 1)
    if "vr" in parts:
        for a, b in VR.plan():
            mem.w8(a, b)
    if "ms" in parts:
        MS.install(mem)
    if "pads" in parts:
        PP.install(mem)
    if "hc" in parts:
        env = dict(os.environ, HC_BASE=hex(PP.HC_BASE), HCPAGE=hex(PP.HCPAGE),
                   PMODE=hex(PP.PST + 2), MSOFF=hex(MS.MSOFF))
        import shbuild
        shbuild.build("tools/hotcue8/build.sh",
                      ["build/hotcue8/hc.bin", "build/hotcue8/hc.syms"], env)
        syms = {}
        for ln in open("build/hotcue8/hc.syms"):
            a, _, n = ln.split()
            syms[n.lstrip("_")] = int(a, 16)
        img = open("build/hotcue8/hc.bin", "rb").read()
        for a in range(syms["hc_table"], syms.get("end", syms["hc_count"] + 4), 4):
            mem.w32(a, 0)
        for i, b in enumerate(img):
            mem.w8(syms["hc_table"] + i, b)
        mem.w32(syms["hc_prev"], mem.r32(0x08C92E20))
        mem.w32(0x08C92E20, syms["hc_entry"])
    if "stems" in parts:
        env = dict(os.environ, PMASK=hex(PP.PMASK))
        import shbuild
        shbuild.build("tools/stems/build.sh",
                      ["build/stems/sm.bin", "build/stems/sm.syms"], env)
        syms = {}
        for ln in open("build/stems/sm.syms"):
            a, _, n = ln.split()
            syms[n.lstrip("_")] = int(a, 16)
        img = open("build/stems/sm.bin", "rb").read()
        for a in range(syms["sm_table"], syms["end"], 4):
            mem.w32(a, 0)
        for i, b in enumerate(img):
            mem.w8(syms["sm_table"] + i, b)
        mem.w32(syms["sm_prev"], mem.r32(0x08D5FFA8))
        mem.w32(0x08D5FFA8, syms["sm_fread"])
        if mem.r32(0x08DDFFD4) == 0x08C61A6C:   # deck installer routes it too
            mem.w32(0x08DDFFD4, syms["sm_fread"])
        if "sm_poll_entry" in syms and mem.r32(0x08C1D56C) == 0x08BF2BCC:
            mem.w32(0x08C1D56C, syms["sm_poll_entry"])   # fast mute trigger (as the deck)
        mem.w8(PP.PMASK, 0b0101)            # some stems muted while playing
    if "flac" in parts:
        import shbuild
        shbuild.build("tools/xflac/build_vfile.sh",
                      ["build/xflac/vfile.bin", "build/xflac/vfile.syms"])
        import vfile_emu_test as VE
        VE.install(mem, VE.syms())
    if "touchfx" in parts:                 # TOUCH FX screen (flush + touch chain)
        import touchfx as TF
        TF.install(mem, engine=False)
    if "themes" in parts:
        (cb, code), (db, data) = TS.plan(TS.default_names())
        for a in range(cb, db, 4):
            mem.w32(a, 0)
        for i, b in enumerate(code):
            mem.w8(cb + i, b)
        for i, b in enumerate(data):
            mem.w8(db + i, b)
        for i, b in enumerate(b"OFF\0"):
            mem.w8(MS.MSTNAME + i, b)


def main():
    parts = set(" ".join(sys.argv[sys.argv.index("--only") + 1:]).split()) if "--only" in sys.argv \
        else {"core", "pads", "hc", "themes", "stems", "flac", "touchfx"}
    m = Machine.load(os.environ.get("CKPT", "cache/emu_track_loaded.pkl"))
    install(m, parts)
    dev = m.mem.dev
    sys.path.insert(0, "tools")
    from ghosttap_emu import fb_png

    def run(s):
        m.cpu.run(limit=int(s * CPU))

    dev.panel_frame = panl.build_frame(tempo=250)
    run(1.0)
    os.makedirs(os.environ.get("XDJ_SHOTS", "screenshots"), exist_ok=True)
    fb_png(m, os.path.join(os.environ.get("XDJ_SHOTS", "screenshots"), "repro_loaded.png"))
    dev.panel_frame = panl.build_frame(keys=("PlayPause",), tempo=250)
    run(0.2)
    dev.panel_frame = panl.build_frame(tempo=250)
    # watch: PC histogram over slices (py core not needed: sample m.cpu.pc)
    hist = collections.Counter()
    frames, prev = [], None
    for i in range(30):
        run(0.2)
        hist[m.cpu.pc & ~0xFF] += 1
        import hashlib
        fbh = hashlib.md5(bytes(fb_png(m, os.path.join(tempfile.gettempdir(), "_rf.png")))).hexdigest()
        frames.append(fbh)
    fb_png(m, os.path.join(os.environ.get("XDJ_SHOTS", "screenshots"), "repro_playing.png"))
    pos = m.mem.r32(0x09D246E0)
    mode = m.mem.r8(0x0B6C4264)
    print("parts:", sorted(parts), "mode", mode, "pos", pos)
    print("PC pages sampled:", [(hex(k), v) for k, v in hist.most_common(6)])
    print("distinct screens over 6 s of play:", len(set(frames)), "of", len(frames))
    if "centre" in parts or "core" in parts:
        import rgb_detail as RD
        cs = RD.cw_build()[1]
        print(f"centre colour module: {m.mem.r32(cs['cw_calls'])} column calls, "
              f"mode {WMODE}, PWV5 snap {m.mem.r32(RD.RO.SLOT5) == RD.TAG_PWV5}, "
              f"PWV7 snap {m.mem.r32(RD.SLOT7) == RD.TAG_PWV7}")
    ok = len(set(frames)) >= len(frames) // 2 and pos > 1000 and mode == 5
    print("RESULT:", "PASS (plays, screen keeps updating)" if ok else
          "FAIL (freeze / no playback) - do NOT install on a deck")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
