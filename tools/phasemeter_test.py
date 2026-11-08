#!/usr/bin/env python3
"""phasemeter_test.py -- Type 1 phase meter cave, emulator check.

Snapshot cache/emu_track_loaded.pkl (deck_session's play-test lineage: a
track is loaded and the HLE play clock chain is alive). Installs
build/phasemeter/pm.bin (tools/phasemeter/build.sh) at its link address,
chains the 2DG flush hook, snapshots the beat grid, then plays/-seeks the
clock to a few positions and checks the drawn red/grey tick columns on
the blue row against a Python model built from pm's OWN cave arrays.

Position: pm.c reads POSL [0x0C0E6D1C] (150/s) = the time digits' source.
On a real deck the DSP data drives it (the elapsed clock visibly moves,
CLAUDE.md sec. 103) while its 75 Hz twin [0x09D246E0] -- the old source,
which made the meter stand still on hardware -- freezes at the load
position. In the emulator the HLE advances POSL with the play clock and
adopts SH_POS writes > 1 s as seeks, so the test jumps the playhead by
writing SH_POS while playing and compares at the POSL the deck holds.

Also: Browse open -> nothing drawn over the list.
Screens: screenshots/phasemeter_*.png
"""
import os
os.environ.setdefault("XDJ_HLE_DSP", "1")   # before emu.machine import: the
                                            # model that advances POSL while
                                            # the emulated deck plays
import struct
import subprocess
import sys
sys.path[:0] = [".", "tools"]
from emu.machine import Machine
from emu import panl
from ghosttap_emu import fb_png

OUT = "build/phasemeter"
# PM_BIN: run this gate against a SAVED pm.bin instead of the tree's (A/B a
# fix against the binary it replaced: same symbols, same test, one flag).
BIN = os.environ.get("PM_BIN") or f"{OUT}/pm.bin"
HOOK = 0x08C92E20
POSL = 0x0C0E6D18    # 150/s track position (deck-read 2026-10-25), what pm.c reads
SH_POS = 0x09D246E0  # 75/s; a write > 1 s off seeks the HLE play clock
MODE = 0x0B6C4264    # 5 = playing, 3 = paused (HLE play clock gate)
PREVIEW = 0x0BA3BCCC  # pm snapshots the grid when this changes
X0, X1, CX, BY = 260, 471, 366, 150


def build():
    """Rebuild pm.bin when any source is newer than it.

    The gate installs build/phasemeter/pm.bin byte for byte, so without this
    it silently tests whatever binary is on disk: a run of the mirror legs
    passed against the pre-fix binary until the module was rebuilt by hand.
    """
    if os.environ.get("PM_BIN"):
        return                      # A/B run: never rebuild over the subject
    srcs = ["tools/phasemeter/pm.c", "tools/phasemeter/pmhook.S",
            "tools/phasemeter/build.sh"]
    out = f"{OUT}/pm.bin"
    if os.path.exists(out) and all(
            os.path.getmtime(s) <= os.path.getmtime(out) for s in srcs):
        return
    print(f"{out} older than its sources -> {OUT}/build.sh")
    subprocess.run(["sh", "tools/phasemeter/build.sh"], check=True,
                   stdout=subprocess.DEVNULL)


def syms():
    out = {}
    for line in open(f"{OUT}/pm.syms"):
        a, _, n = line.split()
        out[n.lstrip("_")] = int(a, 16)
    return out


def model(g, ms):
    """expected {x: 'red'|'tick'} on the blue row (g = [(bar, ms)])"""
    ts = [t for _, t in g]
    if ms < ts[0]:
        return {}
    lo = max(i for i, t in enumerate(ts) if t <= ms)
    frac = 0 if lo == len(ts) - 1 else ((ms - ts[lo]) << 8) // (ts[lo + 1] - ts[lo])
    out = {}
    for k in range(-5, 6):
        j = lo + k
        if not 0 <= j < len(ts):
            continue
        num = ((k << 8) - frac) * (X1 - X0)
        q = abs(num) // (8 << 8)            # C integer division: truncates
        x = CX + (q if num >= 0 else -q)    # toward zero, not floor
        if not X0 <= x <= X1 or abs(x - CX) <= 1:
            continue                          # off-ruler / under the white line
        out[x] = "red" if g[j][0] == 1 else "tick"
    return out


def read_grid(mem, s):
    """[(bar, ms)] from pm's own cave arrays (what pm.c draws against)"""
    n = mem.r32(s["pm_n"])
    if not 2 <= n <= 4000:
        return []
    ms = mem.r_block(s["pm_ms"], 4 * n)
    bar = mem.r_block(s["pm_bar"], n)
    return [(bar[i], struct.unpack_from("<I", ms, 4 * i)[0]) for i in range(n)]


def install_grid(mem, bpm=128.0, n=1000):
    """Write a synthetic beat grid into the ANLZ ctx (the emu_track_loaded
    track was loaded without one: PQTZ tag present, n=0). 8 B per beat:
    [u8 bar 1..4][pad][u32 ms LE] -- the same records rekordbox writes."""
    CTX_TAB, PQTZ_OFF = 0x09826BE4, 0xD7C
    GRID = 0x0BC00000                      # high DDR scratch
    assert mem.r_block(GRID, 64) == b"\0" * 64, "grid scratch not free"
    step = 60000 / bpm                     # ms per beat
    blob = bytearray()
    for i in range(n):
        blob += bytes([(i % 4) + 1, 0, 0, 0]) + struct.pack("<I", int(i * step))
    for i, b in enumerate(blob):
        mem.w8(GRID + i, b)
    ctx = mem.r32(CTX_TAB)
    mem.w32(ctx + PQTZ_OFF + 0x14, n)
    mem.w32(ctx + PQTZ_OFF + 0x18, GRID)
    return [((i % 4) + 1, int(i * step)) for i in range(n)]


def main():
    build()
    s = syms()
    base = s["pm_entry"]
    img = open(BIN, "rb").read()
    m = Machine.load("cache/emu_track_loaded.pkl")
    mem, c = m.mem, m.cpu
    end = s["pm_mode"] + 4
    assert all(mem.r8(a) == 0 for a in range(base, end, 4)), "cave not empty"
    for i, b in enumerate(img):
        mem.w8(base + i, b)
    prev = mem.r32(HOOK)
    mem.w32(s["pm_prev"], prev)
    mem.w8(s["pm_mode"], 1)
    mem.w32(HOOK, base)
    print(f"cave {base:#x}..{end:#x}, chained prev {prev:#x}")
    # count the stock meter's 216x20 row pastes that reach the 2DG
    from emu import tdg as TDGM
    pastes = [0]
    _qt = TDGM.TDG.quad_tex

    def quad_tex(self, flags, sx, sy, sw, sh, pts):
        if (sw, sh) == (216, 20) and pts[0][0] == 258:
            pastes[0] += 1
        return _qt(self, flags, sx, sy, sw, sh, pts)
    TDGM.TDG.quad_tex = quad_tex
    mem.w32(MODE, 5)                      # playing
    want_grid = install_grid(mem)
    mem.w32(PREVIEW, mem.r32(PREVIEW) ^ 1)  # force a grid snapshot
    m.cpu.run(limit=30_000_000)
    g = read_grid(mem, s)
    print(f"grid: {len(g)} beats" + (f", first {g[0]}, last {g[-1]}" if g else ""))
    ok = len(g) >= 2
    for i, target_ms in enumerate((20000, 20493, 29333, 53333)):
        mem.w32(MODE, 5)                       # playing again for the seek
        mem.w32(SH_POS, target_ms * 3 // 40)   # 75 Hz seek (HLE adopts)
        m.cpu.run(limit=15_000_000)            # adopt + draw at the position
        mem.w32(MODE, 3)                       # pause: POSL stops moving
        d0 = mem.r32(s["pm_draws"])
        m.cpu.run(limit=20_000_000)            # flushes at the frozen POSL
        d1 = mem.r32(s["pm_draws"])
        posl = mem.r32(POSL)
        ms = posl * 20 // 3
        fb_png(m, f"screenshots/phasemeter_{i}.png")
        # check the COMPOSED screen: the stock painter still repaints its
        # boxes into the layer between flushes; our hook redraws inside
        # every flush, so every frame that reaches the LCD carries ours
        from PIL import Image
        im = Image.open(f"screenshots/phasemeter_{i}.png").convert("RGB")
        y = BY - 5
        got = {}
        for x in range(X0, X1 + 1):
            r, g_, b = im.getpixel((x, y))
            if r > 180 and g_ < 90 and b < 90:
                got[x] = "red"
            elif 130 <= r <= 170 and abs(r - g_) < 12 and abs(r - b) < 12:
                got[x] = "tick"
        want = model(g, ms)
        # red is drawn 2 px wide (x-1, x): compare by the right column
        got_n = {x: v for x, v in got.items() if not (v == "red" and got.get(x + 1) == "red")}
        match = got_n == want
        ok &= match and d1 > d0
        print(f"POSL {posl} ({ms} ms): draws +{d1-d0}, "
              f"{'MATCH' if match else 'DIFF'} got {sorted(got_n.items())} want {sorted(want.items())}")
    # broken / changing track structure (2026-10-25 deck: deck 1 froze twice
    # while deck 2 played a track served from deck 1's stick over LINK -- the
    # shared ANLZ context gets rewritten under the meter). pm must reject
    # wild pointers and half-written grids, never fault, and recover.
    CTX_TAB, PQTZ_OFF = 0x09826BE4, 0xD7C
    q = mem.r32(CTX_TAB) + PQTZ_OFF
    good_ptr, good_n = mem.r32(q + 0x18), mem.r32(q + 0x14)
    e0, r0 = m.exc_total, mem.r32(s["pm_rejects"])
    for k, (tag, ptr, n) in enumerate((("wild pointer", 0xFFFFFF00, good_n),
                                       ("pointer below RAM", 0x00001000, good_n),
                                       ("half-written grid", 0x0BC00000 + 8 * 600, good_n))):
        mem.w32(q + 0x18, ptr)
        mem.w32(q + 0x14, n)
        if tag == "half-written grid":          # times stop rising mid-way
            for i in range(10, 20):
                mem.w32(ptr + 8 * i + 4, 5)
        mem.w32(PREVIEW, mem.r32(PREVIEW) ^ (0x100 << k))  # a new track
        d0 = mem.r32(s["pm_draws"])
        m.cpu.run(limit=20_000_000)
        rj, n_now = mem.r32(s["pm_rejects"]), mem.r32(s["pm_n"])
        print(f"{tag}: rejects +{rj - r0}, beats {n_now}, draws +{mem.r32(s['pm_draws']) - d0}, "
              f"exceptions +{m.exc_total - e0}")
        ok &= rj > r0 and n_now == 0 and m.exc_total == e0
        r0 = rj
    mem.w32(q + 0x18, good_ptr)                # the real grid back
    mem.w32(q + 0x14, good_n)
    for i in range(10, 20):                     # undo the corruption above
        mem.w32(0x0BC00000 + 8 * 600 + 8 * i + 4, int((600 + i) * 60000 / 128.0))
    m.cpu.run(limit=20_000_000)
    rec = mem.r32(s["pm_n"]) == good_n
    print(f"recovered: beats {mem.r32(s['pm_n'])} (want {good_n})")
    ok &= rec
    # 2026-11-08 deck, BOTH decks on 1.0.4 and LINKED: the orange MASTER
    # CLOCK row came in on the linked deck but ITS OWN blue row stayed a
    # flat line. A track served over LINK carries its overview long before
    # its analysis, and the meter used to record the track as settled on the
    # first look, so the blue row stayed empty for the whole track. That is
    # this leg: a NEW track (new overview hash), PQTZ present with 0 beats,
    # then the grid lands WITHOUT the hash changing again. The old code
    # stored the hash on the first look and never asked again -> 0 beats
    # forever (this leg FAILS on it; verified as a positive control).
    mem.w32(q + 0x14, 0)                    # PQTZ, no beats yet
    mem.w32(q + 0x18, 0)
    mem.w32(PREVIEW, mem.r32(PREVIEW) ^ (0x7 << 8))    # the overview lands
    m.cpu.run(limit=20_000_000)
    n_wait = mem.r32(s["pm_n"])
    mem.w32(q + 0x18, good_ptr)             # the analysis lands, late
    mem.w32(q + 0x14, good_n)
    m.cpu.run(limit=20_000_000)
    n_late = mem.r32(s["pm_n"])
    print(f"late grid (LINK-style load): {n_wait} beats while it waits, "
          f"{n_late} once it arrives (want 0 then {good_n})")
    ok &= n_wait == 0 and n_late == good_n
    # PERFORM screen (2026-10-25 tester: the PERFORM screen showed the stock
    # 4-beat boxes): the same meter 103 px higher, at the same POSL
    mem.dev.panel_frame = panl.build_frame(touch=(705, 37))
    m.cpu.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=10_000_000)
    modew = mem.r32(0x0BA371D4)
    d0 = mem.r32(s["pm_draws"])
    m.cpu.run(limit=20_000_000)
    d1 = mem.r32(s["pm_draws"])
    fb_png(m, "screenshots/phasemeter_perform.png")
    from PIL import Image
    im = Image.open("screenshots/phasemeter_perform.png").convert("RGB")
    got = {}
    for x in range(X0, X1 + 1):
        r, g_, b = im.getpixel((x, BY - 103 - 5))
        if r > 180 and g_ < 90 and b < 90:
            got[x] = "red"
        elif 130 <= r <= 170 and abs(r - g_) < 12 and abs(r - b) < 12:
            got[x] = "tick"
    got_n = {x: v for x, v in got.items() if not (v == "red" and got.get(x + 1) == "red")}
    want = model(g, mem.r32(POSL) * 20 // 3)
    match = modew == 7 and got_n == want and d1 > d0
    ok &= match
    print(f"PERFORM (mode {modew}): draws +{d1-d0}, {'MATCH' if match else 'DIFF'} "
          f"got {sorted(got_n.items())} want {sorted(want.items())}")
    mem.dev.panel_frame = panl.build_frame(touch=(705, 37))   # close PERFORM
    m.cpu.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=10_000_000)
    st = mem.r32(s["pm_stripped"])
    print(f"stock meter row pastes reaching the screen: {pastes[0]}, stripped: {st}")
    ok &= pastes[0] == 0 and st > 0
    # MENU (MY SETTINGS popup) over the main screen: covers the meter's right
    # part only -> must not be painted over either (deck, 2026-10-25)
    mem.dev.panel_frame = panl.build_frame(keys=["MenuUtility"])
    m.cpu.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=30_000_000)
    d0 = mem.r32(s["pm_draws"])
    m.cpu.run(limit=30_000_000)
    d1 = mem.r32(s["pm_draws"])
    fb_png(m, "screenshots/phasemeter_menu.png")
    print(f"MENU popup open: draws +{d1 - d0} (want 0)")
    ok &= d1 == d0
    mem.dev.panel_frame = panl.build_frame(keys=["MenuUtility"])   # close
    m.cpu.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=30_000_000)
    # Browse open: the list covers the meter -> must not be painted over
    mem.dev.panel_frame = panl.build_frame(keys=["Browse"])
    m.cpu.run(limit=3_000_000)
    mem.dev.panel_frame = panl.build_frame()
    m.cpu.run(limit=20_000_000)               # let the list finish opening
    d0 = mem.r32(s["pm_draws"])
    m.cpu.run(limit=30_000_000)
    d1 = mem.r32(s["pm_draws"])
    fb_png(m, "screenshots/phasemeter_browse.png")
    print(f"browse open: draws +{d1 - d0} (want 0)")
    ok &= d1 == d0
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
