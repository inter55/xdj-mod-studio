#!/usr/bin/env python3
"""hotcue8grid_test.py -- PROTOTYPE emulator test of the 8-pad hot cue view
(tools/hotcue8grid). Emulator only; never touches the deck.

Snapshot: PERFORM screen open on the hotcue8 mini stick (track with hot
cues A-H at 5, 13, .. 61 s), made by tools/hotcue8_emu.py + one PERFORM tap
(argv[1]). Every press scenario restarts from that snapshot because a stock
hot-cue press stalls the emulated panel link (CLAUDE.md section 48).

Checks:
  1. 8 pads drawn (hg_draws, screenshot screenshots/hotcue8grid/row.png)
  2. pad E held: live slot 1 = cue E (5, 37000 ms), key bit KEY+86/0x80 up,
     stock hit-tester never latched a zone (ZSTATE 0x1E: our press)
  3. pad E released: slot 1 restored byte-exact (cue B), key bit down
  4. pad A held: no slot write at all, KEY+86/0x40 up
  5. pad H (sits where stock REC was): slot 1 = H, NOT the REC zone/bit
  6. gap + empty pad (cue count forced to 6 in RAM): swallowed, no write
  7. view OFF: the touch reaches stock (zone 0x17 for A)
  8. HANDLER ENTRY (py core): the stock hot-cue handler 0x8D391E6 is
     entered with r4 = slot index and the live slot holding the pressed
     cue (E/H -> r4 2 with cue 5/8, F -> r4 3 with cue 6, A -> r4 1), the
     same entry a stock B press makes; no stock zone handler (REC!) runs.
"""
import os
import re
import subprocess
import sys

os.environ.setdefault("XDJ_HLE_DSP", "1")
sys.path[:0] = [".", "tools"]
from emu.machine import Machine          # noqa: E402
from emu import panl                     # noqa: E402
from ghosttap_emu import fb_png          # noqa: E402
import perform_pads as PP                # noqa: E402
import mysettings as MS                  # noqa: E402

SLOT, KEYB, ZSTATE, MODEW = 0x0B6C4118, 0x0B6C7A28, 0x0C0E75BC, 0x0BA371D4
FLUSH_POOL, TOUCH_POOL = 0x08C92E20, 0x08D5278C
TIMES = [5000 + 8000 * i for i in range(8)]
OUT = "screenshots/hotcue8grid"
PADX = [116 + 63 * i + 29 for i in range(8)]
PADY = 232 + 31


def build():
    env = dict(os.environ, PST=hex(PP.PST), HCPAGE=hex(PP.HCPAGE),
               MSOFF=hex(MS.MSOFF))
    subprocess.run(["sh", "tools/hotcue8grid/build.sh"], check=True, env=env,
                   capture_output=True)
    code = open("build/hotcue8grid/hg.bin", "rb").read()
    syms = {}
    for ln in open("build/hotcue8grid/hg.syms"):
        a, _, n = ln.split()
        syms[n.lstrip("_")] = int(a, 16)
    return code, syms


DELAYED = re.compile(r"\b(bra|bsr|braf|bsrf|jmp|jsr|rts|rte|bt/s|bf/s)\b")
BAD_SLOT = re.compile(r"\b(bra|bsr|braf|bsrf|jmp|jsr|rts|rte|bt|bf|bt/s|bf/s|"
                      r"mova|trapa)\b|@\(.*pc\)|\.L|<.*\+0x")


def slot_scan():
    """no branch / PC-relative load in any delay slot (CLAUDE.md 119)"""
    lines = [ln.split("\t") for ln in open("build/hotcue8grid/hg.dis")
             if re.match(r"^\s*[0-9a-f]+:\t", ln)]
    ins = [(int(p[0].strip()[:-1], 16), " ".join(x.strip() for x in p[2:]))
           for p in lines]
    bad = []
    for (a, t), (b, u) in zip(ins, ins[1:]):
        tgt = re.match(r"(bra|bsr)\s+([0-9a-f]+)", t)
        if tgt and not 0x09850000 <= int(tgt.group(2), 16) < 0x09851000:
            continue                  # literal-pool word decoded as bra
        if DELAYED.match(t) and b == a + 2:
            if BAD_SLOT.search(u) or re.match(r"mov\.[lw]\s+0x", u):
                bad.append((hex(a), t, u))
    return bad


def cue_entries(mem):
    ctx = mem.r32(0x09826BE4) + 0xFC0
    out = []
    for g in range(ctx + 0x20, ctx + 0x40, 8):
        cnt, e = mem.r32(g), mem.r32(g + 4)
        if not cnt or cnt > 8:
            break
        out += [e + 0x40 * k for k in range(cnt)]
    return out


def install_hg(mem, code, syms):
    base, end = syms["hg_table"], syms["hg_view"] + 4
    for a in range(base, end, 4):
        mem.w32(a, 0)
    for i, b in enumerate(code):
        mem.w8(base + i, b)
    mem.w32(syms["hg_bprev"], mem.r32(FLUSH_POOL))
    mem.w32(syms["hg_tprev"], mem.r32(TOUCH_POOL))
    mem.w32(FLUSH_POOL, syms["hg_entry"])
    mem.w32(TOUCH_POOL, syms["hg_tentry"])


def main():
    ck = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "hotcue8_loaded.pkl")
    if not os.path.exists(ck):
        print(f"SKIP: {ck} missing -- build it first with "
              "`.venv/bin/python tools/hotcue8_emu.py`")
        return 0
    os.makedirs(OUT, exist_ok=True)
    code, syms = build()
    ok = True

    def chk(label, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + label)
        ok &= bool(cond)

    bad = slot_scan()
    chk(f"delay slots clean ({len(code)} B)", not bad)
    for b in bad:
        print("    ", b)

    def fresh(view=1, core=None):
        m = Machine.load(ck, core=core) if core else Machine.load(ck)
        mem = m.mem
        PP.install(mem)
        install_hg(mem, code, syms)
        mem.w8(MS.MSOFF, 0)
        mem.w32(syms["hg_view"], view)
        m.cpu.run(limit=6_000_000)
        if mem.r32(MODEW) != 7:                 # open PERFORM if the snapshot
            m.mem.dev.panel_frame = panl.build_frame(touch=(709, 37))
            m.cpu.run(limit=20_000_000)
            m.mem.dev.panel_frame = panl.build_frame()
            m.cpu.run(limit=10_000_000)
        return m, mem, mem.dev

    def slot(mem, k):
        b = SLOT + 0x40 * k
        return bytes(mem.r8(b + i) for i in range(0x40))

    def cue(mem, k):
        b = SLOT + 0x40 * k
        return mem.r16(b + 0x0A), mem.r32(b + 0x0C)

    def model(mem, base, k):                  # the pads read these, not the
        r = base + k * 0x38                    # live player slots
        return (mem.r16(r), mem.r16(r + 2), mem.r32(r + 4))

    def keybit(mem, k):
        off, bit = [(86, 0x40), (86, 0x80), (87, 0x01)][k]
        return bool(mem.r8(KEYB + off) & bit)

    def hold(m, x, y, n=3_000_000):
        m.mem.dev.panel_frame = panl.build_frame(touch=(x, y))
        m.cpu.run(limit=n)

    def release(m, n=20_000_000):
        m.mem.dev.panel_frame = panl.build_frame()
        m.cpu.run(limit=n)

    # 1. drawn
    m, mem, dev = fresh()
    m.cpu.run(limit=20_000_000)
    chk("PERFORM open, HOT CUE mode", mem.r32(MODEW) == 7 and mem.r8(PP.PST + 2) == 0)
    chk("8 pads drawn", mem.r32(syms["hg_draws"]) > 0)
    fb_png(m, f"{OUT}/row.png")
    s_before = [slot(mem, k) for k in range(3)]
    print("slots:", [cue(mem, k) for k in range(3)])

    # 2./3. pad E: borrow slot 1
    hold(m, PADX[4], PADY)
    chk(f"E held: slot 1 = cue E {cue(mem, 1)}", cue(mem, 1) == (5, TIMES[4]))
    chk("E held: KEY+86 bit 0x80 up", keybit(mem, 1))
    chk("E held: stock hit-tester idle (0x1E)", mem.r32(ZSTATE) == 0x1E)
    chk("E held: both pad models hold cue E too (the deck plays from these)",
        model(mem, 0x0BA28800, 1) == (6, 5, TIMES[4]) and
        model(mem, 0x0BA2A52C, 1) == (6, 5, TIMES[4]))
    chk("E held: slots 0/2 untouched", slot(mem, 0) == s_before[0] and slot(mem, 2) == s_before[2])
    fb_png(m, f"{OUT}/held_E.png")
    release(m)
    chk("E released: key bit down", not keybit(mem, 1))
    chk("E released: slot 1 restored byte-exact",
        slot(mem, 1) == s_before[1] and mem.r32(syms["hg_restores"]) == 1)
    chk("E released: pad models back to cue B",
        model(mem, 0x0BA28800, 1) == (6, 2, TIMES[1]) and
        model(mem, 0x0BA2A52C, 1) == (6, 2, TIMES[1]))

    # 4. pad A: its own slot, no write
    m, mem, dev = fresh()
    s0 = [slot(mem, k) for k in range(3)]
    hold(m, PADX[0], PADY)
    chk("A held: KEY+86 bit 0x40 up", keybit(mem, 0))
    chk("A held: no slot written", [slot(mem, k) for k in range(3)] == s0
        and mem.r32(syms["hg_borrow"]) == 0)
    release(m)
    chk("A released: key bit down", not keybit(mem, 0))

    # 5. pad H where stock REC was
    m, mem, dev = fresh()
    hold(m, PADX[7], PADY)
    chk(f"H held: slot 1 = cue H {cue(mem, 1)}", cue(mem, 1) == (8, TIMES[7]))
    chk("H held: REC zone (0x1A) not latched", mem.r32(ZSTATE) == 0x1E)
    release(m)

    # 6. gap + empty pads
    m, mem, dev = fresh()
    s0 = [slot(mem, k) for k in range(3)]
    hold(m, 116 + 63 * 2 + 60, PADY)              # gap between C and D
    chk("gap swallowed", mem.r32(syms["hg_swallowed"]) >= 1 and mem.r32(ZSTATE) == 0x1E)
    release(m)
    for e in cue_entries(mem):                    # test-only: G/H "empty"
        if mem.r16(e + 0x0E) in (7, 8):
            mem.w16(e + 0x0E, 0)
    m.cpu.run(limit=10_000_000)
    fb_png(m, f"{OUT}/empty_GH.png")
    sw = mem.r32(syms["hg_swallowed"])
    hold(m, PADX[7], PADY)
    m.cpu.run(limit=6_000_000)
    mem.dev.panel_frame = panl.build_frame(touch=(PADX[5], PADY))   # finger slides to F
    m.cpu.run(limit=3_000_000)
    chk("empty H swallowed (one press), nothing written, slide-on dead",
        mem.r32(syms["hg_swallowed"]) == sw + 1 and
        [slot(mem, k) for k in range(3)] == s0 and not any(keybit(mem, k) for k in range(3)))
    release(m)

    # 7. view off: stock
    m, mem, dev = fresh(view=0)
    zs = set()
    mem.dev.panel_frame = panl.build_frame(touch=(165, PADY))
    for _ in range(12):
        m.cpu.run(limit=300_000)
        zs.add(mem.r32(ZSTATE))
    chk(f"view off: stock zone A {sorted(map(hex, zs))}", 0x17 in zs)

    # 8. handler entry (py core): the stock hot-cue consumer 0x8D44xxx
    # edge-detects snapshot bytes 34/35 (= KEY+86/87) and tail-jumps to the
    # hot-cue handler 0x8D391E6 with r4 = slot index 1..3. Record every entry
    # (r4 + the live slot's cue number at that instant) and every stock zone
    # handler entry (0x8DD0BA4/BB4/BC4 = A/B/C, 0x8DD0BD4 REC, 0x8DD0C18 CALL).
    HANDLER = 0x08D391E6
    ZONES = {0x08DD0BA4: "A", 0x08DD0BB4: "B", 0x08DD0BC4: "C",
             0x08DD0BD4: "REC", 0x08DD0C18: "CALL"}

    def entries(view, touch, steps=4_000_000):
        m, mem, dev = fresh(view=view, core="py")
        c, ev, zn = m.cpu, [], set()

        def tr(pc, op):
            if pc == HANDLER:
                k = c.gr(4)
                n = mem.r16(SLOT + 0x40 * (k - 1) + 0x0A) if 1 <= k <= 3 else -1
                ev.append((k, n))
            elif pc in ZONES:
                zn.add(ZONES[pc])
        c.trace = tr
        mem.dev.panel_frame = panl.build_frame(touch=touch)
        c.run(limit=steps)
        return ev, zn

    for label, view, touch, want_k, want_n, want_zone in (
            ("stock B (view off)", 0, (280, PADY), 2, 2, {"B"}),
            ("pad E", 1, (PADX[4], PADY), 2, 5, set()),
            ("pad H (old REC spot)", 1, (PADX[7], PADY), 2, 8, set()),
            ("pad A", 1, (PADX[0], PADY), 1, 1, set()),
            ("pad F", 1, (PADX[5], PADY), 3, 6, set())):
        ev, zn = entries(view, touch)
        print(f"  {label}: handler entries (r4, slot cue) {ev[:3]}, zones {sorted(zn)}")
        chk(f"{label}: hot-cue handler entered with r4={want_k}, slot holds cue {want_n}",
            ev and ev[0] == (want_k, want_n))
        chk(f"{label}: zone handlers {sorted(want_zone) or 'none'}", zn == want_zone)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
