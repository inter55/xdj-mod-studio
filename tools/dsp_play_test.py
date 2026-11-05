#!/usr/bin/env python3
"""dsp_play_test.py [SNAPSHOT] -- load + play a track with Pioneer's REAL
DSP running in the emulator (snapshot from tools/dsp_boot_full.py).

Mounts STICK (default /tmp/flacstick), opens [FOLDER] > Contents >
FlacTest, loads the row ROW (default 0 = control.wav), presses PLAY and
runs PLAY_STEPS. Reports: E-8302 on screen (red text), the SH position
word, DSP health, and writes what the DSP sent to the DAC (McASP1) as
build/dsp_out.wav. Screens: screenshots/dsp_play_*.png.
"""
import os
import struct
import sys
import time
import wave
sys.path[:0] = [".", "tools"]
import numpy as np                       # noqa: E402
from PIL import Image                    # noqa: E402
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
from ghosttap_emu import fb_png          # noqa: E402

SNAP = sys.argv[1] if len(sys.argv) > 1 else "cache/emu_dsp_idle.pkl"
STICK = os.environ.get("STICK", "/tmp/flacstick")
ROW = int(os.environ.get("ROW", "0"))
PLAY_STEPS = int(float(os.environ.get("PLAY_STEPS", "1.5e9")))
ROT = 0x0B6C7B48
SH_POS = 0x09D246E0


def red_text(m, name):
    p = f"screenshots/dsp_play_{name}.png"
    fb_png(m, p)
    a = np.array(Image.open(p).convert("RGB")).astype(int)
    r = a[398:428, 170:600]
    return int(((r[..., 0] > 180) & (r[..., 1] < 90) & (r[..., 2] < 90)).sum())


def main():
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    t0 = time.time()
    if SNAP == "boot":                         # fresh boot from flash, DSP slowed
        os.environ["XDJ_DSP_EMU"] = "1"
        from emu import fw as F
        flash, _ = F.load_flash(None)
        m = Machine(flash)
        m.mem.dev.dspemu.ratio = float(os.environ.get("BOOT_RATIO", "0.1"))
        for _ in range(int(float(os.environ.get("BOOT_STEPS", "1.6e9")) // 2e8)):
            m.cpu.run(limit=200_000_000)
            print(f"  boot {m.mem.dev.dspemu.cycles/1e6:.0f}M DSP cycles, "
                  f"err={m.mem.dev.dspemu.error} [{time.time()-t0:.0f}s]", flush=True)
    else:
        m = Machine.load(SNAP)
    mem, dev = m.mem, m.mem.dev
    d = dev.dspemu
    assert d is not None, "snapshot has no real DSP"
    if os.environ.get("WATCH"):                # DSP store watch (C core only)
        from emu.c6x import cfast
        lo, hi = (int(x, 16) for x in os.environ["WATCH"].split(":"))
        cfast.watch(d.cpu, lo, hi)

        def wsum(tag):
            import collections
            n, log = cfast.watch_log(d.cpu)
            c = collections.Counter((hex(p), hex(a)) for p, a, v, cy in log)
            print(f"  watch[{tag}] total {n}; last pcs {c.most_common(6)}; "
                  f"last {[(hex(a), hex(v)) for p, a, v, cy in log[-4:]]}", flush=True)
    else:
        def wsum(tag):
            pass
    if os.environ.get("TRACE_CMD"):            # every change of the SH command words
        cmdlog = []
        ost = d._store

        def stw(a, v, ost=ost):
            if 0x11837BC0 <= a < 0x11837C64:
                cmdlog.append((m.cpu.cycles if hasattr(m.cpu, "cycles") else 0, hex(a), hex(v)))
            if 0x11825000 <= a < 0x11829000 and not getattr(d, "_seen_play_feed", 0):
                d._seen_play_feed = 1
                cmdlog.append(("PLAYFEED", hex(a)))
            return ost(a, v)
        d._store = stw
        import atexit

        def dump():
            last, out = {}, []
            for e in cmdlog:
                if len(e) == 3 and last.get(e[1]) == e[2]:
                    continue
                if len(e) == 3:
                    last[e[1]] = e[2]
                out.append(str(e))
            open("build/cmd_trace.txt", "w").write("\n".join(out))
        atexit.register(dump)
    if os.environ.get("TRACE_HPI"):            # log the SH<->DSP audio hand-off
        tl = TRACE = []
        ob, orr = d.bulk, d.read

        def bk(data, ob=ob):
            tl.append(("bulk", m.cpu.cycles if hasattr(m.cpu, "cycles") else 0,
                       hex(d.hpia), len(data)))
            return ob(data)

        def rd(a, n, orr=orr):
            h = d.hpia
            v = orr(a, n)
            if a >= 0x18080000 and (0x11838180 <= h < 0x118381C0 or h in (
                    0x11837BF8, 0x11837CC8, 0x11837CCC, 0x11837CD0, 0x11837CD4, 0x11838140)):
                if not tl or tl[-1][2:4] != (hex(h), v):
                    tl.append(("rd", 0, hex(h), v))
            return v
        d.bulk, d.read = bk, rd
        import atexit
        atexit.register(lambda: open("build/hpi_trace.txt", "w").write(
            "\n".join(map(str, tl))))
    d.ratio = float(os.environ.get("BOOT_RATIO", "0.1"))       # fast while mounting

    def run(n):
        m.cpu.run(limit=int(n))

    def key(k, n=15e6):
        dev.panel_frame = panl.build_frame(keys=[k])
        run(3e6)
        dev.panel_frame = panl.build_frame()
        run(n)

    if not os.environ.get("MOUNTED"):          # snapshot already has the stick in
        u = dev._usb()
        u.unplug()
        run(20e6)
        disk = open_disk(STICK)
        dev.usb_disk = disk
        u.stick = MassStorage(disk)
        for _ in range(6):
            run(250e6)
        if not u.stick.addr:                   # boot-attach quirk (CLAUDE.md §32):
            u.unplug()                         # a second insert enumerates
            run(100e6)
            for _ in range(5):
                run(200e6)
        print(f"stick addr {u.stick.addr}", flush=True)
    else:
        run(float(os.environ.get("SETTLE", "0")))
    print(f"stick mounted [{time.time()-t0:.0f}s]", flush=True)
    if os.environ.get("SAVE_MOUNTED") and SNAP == "boot":
        m.save(os.environ["SAVE_MOUNTED"])    # iterate the browse steps from here
        print("saved", os.environ["SAVE_MOUNTED"], flush=True)
    from emu.c6x.dspdev import DSP_PER_SH
    pushes = int(os.environ.get("PUSHES", "0"))
    if pushes:                                 # rekordbox stick: USB + n pushes
        key("DevUSB", 120e6)
        for k in range(pushes - 1):
            key("RotaryPush", 80e6)
            red_text(m, f"push{k + 1}")
        red_text(m, "list")
        d.ratio = DSP_PER_SH
        key("RotaryPush", 300e6)               # the last push loads
    else:
        key("DevUSB", 60e6)    # selects USB AND opens the browser (no BROWSE press)
        mem.w16(ROT, (mem.r16(ROT) + 1) & 0xFFFF)
        run(15e6)
        for _ in range(3):
            key("RotaryPush", 30e6)
        for _ in range(ROW):
            mem.w16(ROT, (mem.r16(ROT) + 1) & 0xFFFF)
            run(20e6)
        run(40e6)
        red_text(m, "list")
        d.ratio = DSP_PER_SH                   # real speed from the load on
        key("RotaryPush", 300e6)               # load
    wsum("loaded")
    print(f"loaded: red={red_text(m, 'loaded')} pos={mem.r32(SH_POS)} "
          f"dsp err={d.error} [{time.time()-t0:.0f}s]", flush=True)
    n0 = len(d.asp1.out)
    # 0x11837BF8 is the DSP's own state: 1 loading, 2 playing, 3 paused
    # (CLAUDE.md §121). In this line a LOAD AUTO-PLAYS (SH mode 3, DSP 2) and
    # PlayPause then PAUSES it (mode 5, DSP 3) -- so pressing it blindly made
    # every "play" run measure a paused deck (frozen counter, no reads, a
    # 13-block output loop). Only press when the DSP is not already playing.
    st0 = d.bus.read(0x11837BF8, 4)
    pressed = False
    if not os.environ.get("NOPLAY") and st0 != 2:
        dev.panel_frame = panl.build_frame(keys=("PlayPause",), tempo=250)
        run(3e6)
        pressed = True
    print(f"play: dsp state before={st0} pressed={pressed} "
          f"state after={d.bus.read(0x11837BF8, 4)}", flush=True)
    dev.panel_frame = panl.build_frame(tempo=250)
    done = 0
    while done < PLAY_STEPS:
        run(100e6)
        done += 100e6
        nz = sum(1 for v in d.asp1.out[n0:] if v)
        wsum(f"+{done/1e6:.0f}M")
        if os.environ.get("SAVE_EARLY") and done == 100e6:
            m.save(os.environ["SAVE_EARLY"])   # feed still running here
            print("saved", os.environ["SAVE_EARLY"], flush=True)
        print(f"  play +{done/1e6:.0f}M pos={mem.r32(SH_POS)} mode={mem.r8(0x0B6C4264)} "
              f"dsp state={d.bus.read(0x11837BF8, 4)} ctr={d.bus.read(0x11837CCC, 4)} "
              f"fpos={d.bus.read(0x118381B0, 4):#x} "
              f"dac words {len(d.asp1.out) - n0} nonzero {nz} dsp err={d.error} "
              f"[{time.time()-t0:.0f}s]", flush=True)
        if d.error:
            break
    if os.environ.get("SAVE_PLAYING"):
        m.save(os.environ["SAVE_PLAYING"])
        print("saved", os.environ["SAVE_PLAYING"], flush=True)
    red = red_text(m, "playing")
    out = d.asp1.out[n0:]
    os.makedirs("build", exist_ok=True)
    if out:
        a = np.array(out, dtype=np.uint32).view(np.int32)
        a = a[: len(a) // 2 * 2].reshape(-1, 2)
        with wave.open("build/dsp_out.wav", "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(int(os.environ.get("XDJ_DSP_FS", "44100")))
            w.writeframes((a >> 16).astype("<i2").tobytes())
    nz = sum(1 for v in out if v)
    ii = getattr(dev, "iic", None)
    print("iic seen", {hex(k): v for k, v in (ii.seen.items() if ii else [])})
    print(f"RESULT: E-8302 red={red} (0 = gone), DAC words {len(out)}, nonzero {nz}, "
          f"pos {mem.r32(SH_POS)}, dsp err {d.error} -> build/dsp_out.wav")


if __name__ == "__main__":
    main()
