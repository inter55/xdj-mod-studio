#!/usr/bin/env python3
"""usbpack_test.py -- the whole USB-stick install path in the emulated
XDJ-700: stage-1 loader (ul.bin on its hook) + stage-2 installer (s2.bin
as MODS.BIN) + a full mods pack (tools/usbpack.py) on a scratch stick.
Nothing here touches a deck.

From the shipped main-screen snapshot (booted, no stick): install the
loader, mount a stick with PIONEER/MODS/MODS.BIN + PACK.BIN, let the deck
mount it, then check
  * the stage-2 state block: DONE, every record streamed, all arms written
  * every pack record against RAM: zero regions zero, data byte-exact,
    arm words = their new values (so the full flush / read hook chains)
  * the deck still works: Browse opens the stick; PERFORM opens by touch
    and its tabs draw; a theme push through the installed switcher
    recolours the screen and comes back to stock
  * negative: a pack whose last arm expects the wrong stock value installs
    its data but arms NOTHING (status ARMSTOCK)
  * flac: the pack also carries the FLAC + Opus layer; the same stick holds
    generated FLAC and Opus files; after the install the deck lists them
    with their durations (the stock WAV parser read the layer's header),
    opens each through the layer (vf_stats) and LOADS each one (the deck's
    mode word goes to "at cue" / "playing"). This case runs on the real-DSP
    snapshot (firmware home dspsound.pkl.gz): the instant main-screen
    snapshot has no DSP and cannot finish ANY track load (E-8302 300E/300A
    for WAV, Opus and FLAC alike -- 2026-11-04, a wasted afternoon), so it
    is only used for the good/badarm cases.
Screenshots: screenshots/usbpack/.
Usage: .venv/bin/python tools/usbpack_test.py [good|badarm|flac|all]
"""
import os
import shutil
import struct
import sys
import tempfile

os.environ.setdefault("XDJ_HLE_DSP", "1")
sys.path[:0] = [".", "tools", "mods"]
from emu.machine import Machine
import usbloader_test as UT
import usbpack as UP

OUT = "build/usbloader"
S2ST = ["IDLE", "NOFILE", "SHORT", "MAGIC", "COUNT", "SURVEY", "EXPSUM", "BADREC",
        "SUM", "ARMSTOCK", "DONE"]
SHOTS = "screenshots/usbpack"


def checkpoint_dsp():
    """the real-DSP snapshot (tracks load and play), else the plain one"""
    if os.environ.get("CKPT"):
        return os.environ["CKPT"]
    from emu import fwhome
    p = fwhome.path("dspsound.pkl.gz")
    return p if os.path.exists(p) else UT.checkpoint()


MODE = 0x0B6C4264                 # deck mode word: 3 at cue, 5 playing (hledsp)


def names():
    """the stock 8 themes, one of them with lit buttons (EXPSUM records)"""
    import theme_switch as TS
    lit = TS.theme_names(["crimson-red@#20d060"])
    return lit + [n for n in TS.default_names() if n != "crimson-red"]


def s2syms():
    s = {}
    for line in open(os.path.join(OUT, "s2.syms")):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16) & 0x1FFFFFFF
    return s


def make_stick(pack, audio=False):
    d = tempfile.mkdtemp(prefix="packstick_")
    os.makedirs(os.path.join(d, "PIONEER", "MODS"))
    if audio:                      # Contents/FlacTest/{a.flac, b.opus}: sines
        import subprocess
        f = os.path.join(d, "Contents", "FlacTest")
        os.makedirs(f)
        for name, src, rate, codec in (
                ("a.flac", "sine=frequency=440:duration=8", "44100", ["-c:a", "flac"]),
                ("b.opus", "sine=frequency=440:duration=8", "48000",
                 ["-c:a", "libopus", "-b:a", "96k"]),
                ("c.flac", "anoisesrc=d=8:c=pink:a=0.5", "44100", ["-c:a", "flac"])):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src,
                            "-ac", "2", "-ar", rate] + codec + [os.path.join(f, name)],
                           check=True)
    open(os.path.join(d, "PIONEER", "MYSETTING.DAT"), "wb").write(b"\0" * 160)
    s2 = open(os.path.join(OUT, "s2.bin"), "rb").read()
    open(os.path.join(d, "PIONEER", "MODS", "MODS.BIN"), "wb").write(UT.modsbin(s2))
    open(os.path.join(d, "PIONEER", "MODS", "PACK.BIN"), "wb").write(pack)
    return d


def mount(m, stick):
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    dev = m.mem.dev
    u = dev._usb()
    u.unplug()
    m.cpu.run(limit=20_000_000)
    disk = open_disk(stick)
    dev.usb_disk = disk
    u.stick = MassStorage(disk)
    for _ in range(8):
        m.cpu.run(limit=250_000_000)


def verify(mem, recs, log, live=()):
    """RAM must hold what the records leave in file order (a zero region
    gets data later; a later run overrides an earlier byte) -> bad byte runs"""
    want = {}
    for dst, ln, fl, exp, data in recs:
        src = bytes(ln) if fl & UP.F_ZERO else data
        want.update(zip(range(dst, dst + ln), src))
    bad, state, runs = 0, 0, []
    for a in sorted(want):
        if mem.r8(a) != want[a]:
            if any(lo <= a < hi for lo, hi in live):
                state += 1              # a running mod's own counters / state
                continue
            bad += 1
            if runs and a - runs[-1][1] <= 16:
                runs[-1][1] = a + 1
            else:
                runs.append([a, a + 1])
    for lo, hi in runs[:8]:
        log(f"  RAM differs at {lo:#x}..{hi:#x} (want {want[lo]:#04x} got {mem.r8(lo):#04x})")
    if state:
        log(f"  ({state} bytes of live mod state changed since the install: expected)")
    return bad


def flac_loads(m, stick, log):
    """browse to Contents/FlacTest, load a.flac then b.opus through the
    installed layer (vfile_emu_test's row seek by image)"""
    import vfile_emu_test as VT
    from ghosttap_emu import fb_png
    mem = m.mem
    s = PB_syms("build/xflac/vfile.syms")
    st = lambda: [mem.r32(s["vf_stats"] + 4 * i) for i in range(7)]
    ok = True

    def press(k, hold=15_000_000, after=60_000_000):
        m.mem.dev.panel_frame = VT.panl.build_frame(keys=[k])
        m.cpu.run(limit=hold)
        m.mem.dev.panel_frame = VT.panl.build_frame()
        m.cpu.run(limit=after)

    def browse_open():
        for _ in range(2):
            press("Browse")
            fb_png(m, f"{SHOTS}/_band.png")
            if VT.highlighted(f"{SHOTS}/_band.png") is not None:
                return True
        return False

    def browse_to(band, tag):
        if not browse_open():
            log("[flac] browse screen did not open")
            return None
        mem.w16(VT.ROT, (mem.r16(VT.ROT) + 1) & 0xFFFF)
        m.cpu.run(limit=15_000_000)
        for _ in range(3):                          # [FOLDER] -> Contents -> FlacTest
            VT.key(m, "RotaryPush", 30_000_000)
        for _ in range(6):
            m.cpu.run(limit=40_000_000)
            fb_png(m, f"{SHOTS}/_band.png")
            b = VT.highlighted(f"{SHOTS}/_band.png")
            if b is None or b == band:
                break
            mem.w16(VT.ROT, (mem.r16(VT.ROT) + 1) & 0xFFFF)
            m.cpu.run(limit=20_000_000)
        fb_png(m, f"{SHOTS}/flac_{tag}_list.png")
        return b

    OPS = {1: "read", 2: "seek", 3: "tell", 4: "eof", 5: "size", 6: "close", 7: "dread"}

    def oplog(start=None):
        n = mem.r32(s["vf_logi"])
        lo = max(0, n - 64) if start is None else max(start, n - 64)
        for k in range(lo, n):
            e = [mem.r32(s["vf_log"] + ((k % 64) * 5 + j) * 4) for j in range(5)]
            log(f"      {OPS.get(e[0], e[0])}: fp {e[1]:#x} a {e[2]:#x} b {e[3]:#x} -> {e[4]:#x}")

    for band, tag, idx in ((0, "a_flac", 1), (1, "b_opus", 4), (2, "c_flac", 1)):
        before = st()
        logi0 = mem.r32(s["vf_logi"])
        b = browse_to(band, tag)
        if b is None and tag == "a_flac":
            # the deck sorts files into types when the stick mounts; if that
            # ran before the type patch, a re-insert lists the FLAC
            log("[flac] no row: re-inserting the stick (types sorted at mount)")
            press("Browse")
            mount(m, stick)
            b = browse_to(band, tag)
        VT.key(m, "RotaryPush", 100_000_000)        # load
        waited = 100
        while mem.r32(MODE) not in (3, 5) and waited < 2500:   # FLAC decode on the
            m.cpu.run(limit=100_000_000)                        # emulated SH is slow
            waited += 100
        fb_png(m, f"{SHOTS}/flac_{tag}_loaded.png")
        after = st()
        opened = after[idx] - before[idx]
        mode = mem.r32(MODE)
        log(f"[flac] {tag}: cursor band {b}, layer opens +{opened}, vreads "
            f"+{after[2] - before[2]}, decodes +{after[3] - before[3]}, "
            f"mode {mode} after {waited}M steps ({'LOADED' if mode in (3, 5) else 'not loaded'})")
        ok &= opened > 0 and mode in (3, 5)
        if os.environ.get("OPLOG"):
            oplog(logi0)
        VT.key(m, "Browse", 60_000_000)             # back to the main screen
    return ok


def PB_syms(rel):
    import permanent_build as PB
    return PB._syms(rel)


def run_case(case, log=print):
    from emu import panl
    from ghosttap_emu import fb_png
    import theme_switch as TS
    import theme_switch_emu_direct as TD
    os.makedirs(SHOTS, exist_ok=True)
    mem_, recs = UP.build_ops(names=names(), log=lambda *a: None,
                              flac="opus" if case == "flac" else None)
    if case == "badarm":
        d, l, f, e, x = recs[-1]
        recs = recs[:-1] + [(d, l, f, e ^ 0x10, x)]
    pack = UP.encode(recs)
    log(f"[{case}] pack: {len(pack):,} B, {len(recs)} records")
    s = UT.syms()
    ck = checkpoint_dsp() if case == "flac" else UT.checkpoint()
    log(f"[{case}] snapshot: {ck}")
    m = Machine.load(ck)
    mem, dev = m.mem, m.mem.dev
    base = s["ul_head"]
    img = open(os.path.join(OUT, "ul.bin"), "rb").read()
    assert mem.r32(UT.POOL) == UT.STOCK_OPEN
    assert not any(mem.r8(base + i) for i in range(0, 0x10000, 4)), "cave not empty"
    for i, b in enumerate(img):
        mem.w8(base + i, b)
    mem.w32(UT.POOL, s["ul_open"])
    stick = make_stick(pack, audio=(case == "flac"))
    mount(m, stick)
    ul = {k: mem.r32(s["ul_state"] + 4 * i) for i, k in enumerate(
        ["magic", "calls", "mark", "status", "tries", "loads", "len", "sum", "done",
         "drive", "mode", "ctx"])}
    log(f"[{case}] loader: status {UT.ST[ul['status']]} loads={ul['loads']} "
        f"drive={chr(ul['drive']) if ul['drive'] else '?'} mark={ul['mark']:#x}")
    ok = ul["status"] == 7 and ul["loads"] == 1
    st = s2syms()["s2_state"]
    ok &= ul["mark"] & 0x1FFFFFFF == st
    s2 = {k: mem.r32(st + 4 * i) for i, k in enumerate(
        ["magic", "status", "nrec", "done", "bytes", "armed", "err_rec", "err_addr",
         "sum", "want"])}
    name = S2ST[s2["status"]] if s2["status"] < len(S2ST) else s2["status"]
    log(f"[{case}] stage-2: {name} records {s2['done']}/{s2['nrec']} "
        f"bytes={s2['bytes']:,} arms={s2['armed']} err@{s2['err_addr']:#x} "
        f"(record {s2['err_rec']})")
    ok &= s2["magic"] == 0x54533253
    arms = [r for r in recs if r[2] & UP.F_ARM]
    if case == "badarm":
        ok &= s2["status"] == 9 and s2["done"] == len(recs)
        armed = sum(1 for d, l, f, e, x in arms if mem.r32(d) == struct.unpack("<I", x)[0])
        log(f"[{case}] arm words written: {armed} (must be 0)")
        ok &= armed == 0
        bad = verify(mem, [r for r in recs if not r[2] & UP.F_ARM], log)
        log(f"[{case}] data in RAM, nothing armed: {bad} bytes differ (must be 0)")
        ok &= bad == 0
    else:
        ok &= s2["status"] == 10 and s2["done"] == len(recs) and s2["armed"] == len(arms)
        # the mods are LIVE now: their homes hold counters / state; code,
        # tables, pictures and every arm word outside them must be exact
        # (byte-exactness of every record is what the badarm case proves)
        import rgb_overview as RO
        import perform_pads as PP
        live = [(lo, hi) for lo, hi, _, _ in mem_.regions] + [(RO.CAVE, PP.PEND)]
        bad = verify(mem, recs, log, live)
        log(f"[{case}] RAM vs pack outside the mods' own state: {bad} bytes differ (must be 0)")
        ok &= bad == 0
    # the mount finished: Browse shows the stick
    fb0 = bytes(mem.fb[0]) + bytes(mem.fb[1])
    dev.panel_frame = panl.build_frame(keys=["Browse"])
    m.cpu.run(limit=15_000_000)
    dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=60_000_000)
    fb1 = bytes(mem.fb[0]) + bytes(mem.fb[1])
    changed = sum(a != b for a, b in zip(fb0[::7], fb1[::7]))
    log(f"[{case}] browse after mount: {changed} sampled bytes changed")
    ok &= changed > 1000
    fb_png(m, f"{SHOTS}/{case}_browse.png")
    dev.panel_frame = panl.build_frame(keys=["Browse"])
    m.cpu.run(limit=15_000_000)
    dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=60_000_000)
    if case == "flac":
        ok &= flac_loads(m, stick, log)
        shutil.rmtree(stick, ignore_errors=True)
        log(f"[{case}] {'PASS' if ok else 'FAIL'}")
        return ok
    if case != "good":
        shutil.rmtree(stick, ignore_errors=True)
        log(f"[{case}] {'PASS' if ok else 'FAIL'}")
        return ok
    # --- the installed mods work: PERFORM by touch, tabs, nothing faults
    import perform_pads as PP
    e0 = m.exc_total

    def touch(p, hold=3_000_000, after=3_000_000):
        for _ in range(40):
            if mem.r32(PP.ZSTATE) == 0x1E:
                break
            m.cpu.run(limit=500_000)
        dev.panel_frame = panl.build_frame(touch=p)
        m.cpu.run(limit=hold)
        dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=after)
        for _ in range(40):
            if mem.r8(PP.PST) == 0 and mem.r32(PP.ZSTATE) == 0x1E:
                break
            m.cpu.run(limit=500_000)

    touch((710, 38))
    perf = mem.r32(0x0BA371D4) == 7
    for t in range(len(PP.TAB_Y)):
        touch((PP.TAB_X + PP.TAB_W // 2, PP.TAB_Y[t] + PP.TAB_H // 2))
        m.cpu.run(limit=20_000_000)
    fb_png(m, f"{SHOTS}/{case}_perform.png")
    touch((710, 38))
    log(f"[{case}] PERFORM opened by touch: {perf}, exceptions during taps: "
        f"{m.exc_total - e0}")
    ok &= perf and m.exc_total - e0 == 0
    # --- theme push through the switcher the pack installed (real deck code)
    (cb, code), (db, data) = TS.plan(names())
    syms = TS.build_code()[1]
    ok &= mem.r32(cb) == struct.unpack("<I", code[:4])[0]
    before = bytes(mem.fb[0]) + bytes(mem.fb[1])
    px0 = bytes(mem.r_block(0x08500000, 0x20000))
    ret = TD.call(m, syms["_ts_push"])
    px1 = bytes(mem.r_block(0x08500000, 0x20000))
    m.cpu.run(limit=60_000_000)
    fb_png(m, f"{SHOTS}/{case}_theme1.png")
    cur = mem.r8(db + 32)
    changed = sum(a != b for a, b in zip(px0, px1))
    log(f"[{case}] theme push: returned {ret}, cur={cur}, artwork bytes changed "
        f"in a 128 KB sample: {changed:,}")
    ok &= ret and cur == 1 and changed > 1000
    n = len(names())
    for _ in range(n):
        ret &= TD.call(m, syms["_ts_push"])
    px2 = bytes(mem.r_block(0x08500000, 0x20000))
    log(f"[{case}] back to OFF after {n} more pushes: cur={mem.r8(db + 32)}, "
        f"sample back to stock: {px2 == px0}")
    ok &= ret and mem.r8(db + 32) == 0 and px2 == px0
    m.cpu.run(limit=60_000_000)
    fb_png(m, f"{SHOTS}/{case}_theme_off.png")
    log(f"[{case}] exceptions total since mount: {m.exc_total - e0}")
    ok &= m.exc_total - e0 == 0
    if os.environ.get("SAVE"):
        m.save(os.environ["SAVE"])
    shutil.rmtree(stick, ignore_errors=True)
    log(f"[{case}] {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    cases = ["good", "badarm", "flac"] if arg == "all" else [arg]
    ok = all([run_case(c) for c in cases])
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
