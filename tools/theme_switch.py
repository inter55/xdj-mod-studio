#!/usr/bin/env python3
"""theme_switch.py -- on-deck theme SWITCHING (MY SETTINGS [THEME] cycles
OFF -> theme 1 -> ... -> OFF, live, any order, exact).

Tables are built here with the SAME colour maths as the Studio preview
(themes.map_colours, incl. vivid/lift), then made one-to-one: each theme
maps the artwork source colours S onto distinct colours D_t that never
occur elsewhere in the artwork (nudged 1-4 LSB when two would collide).
So stock -> A -> B -> stock returns every pixel exactly. Deck code:
tools/themesw/ts.c (built with tools/themesw/build.sh).

  theme_switch.py check            Python model: exactness + preview match
                                   (NAME@#rrggbb = NAME with lit buttons)
  theme_switch.py plan [themes..]  sizes / addresses for the deck
  theme_switch.py emu  [themes..]  emulator: real MY SETTINGS pushes
  theme_switch.py export [themes..] build/themesw/ files for the emu app
"""
import os
import struct
import subprocess
import sys

import numpy as np

sys.path[:0] = [".", "tools", "mods"]
import theme_rev as TR

IMG_BASE = 0x08000000
TS_BASE = int(os.environ.get("TS_BASE", "0x09770000"), 0)
TS_DATA = int(os.environ.get("TS_DATA", "0x09779000"), 0)
TS_END = TS_BASE + 0x20000                 # 0x09790000: hot cues follow
# never recolour our own code/data caves (waveform, versions, settings,
# PERFORM pads): CAVE .. end of the PERFORM zero run
NO_TOUCH = (0x081D8900, 0x081E0400)
MAGIC = 0x57535458                         # "XTSW"
CHUNK = 1024   # px per safety fingerprint (2026-10-26: 256 -> 1024, light makes
               # almost every chunk fingerprinted; 75 KB -> 19 KB of hashes)
# The firmware writes a few words INSIDE real artwork at runtime
# (emulator: 8 KB-stride headers + play-HISTORY text at 0x087E0000..
# 0x087FC00C, in the middle of the LINK/USB button art). No blanket
# exclusion (that left LINK unthemed): the 128-px stock fingerprint skips
# exactly the touched blocks, on the deck too.
LIVE_DATA = []
# 2026-10-26 (user: "light mode has some black at the bottom"): the track
# position bar under the overview is white/light-grey stripes; flipped it
# became a solid black bar. Its two 504x5 pictures keep their stock colours
# in every theme (colour themes never touched them: neutral only).
KEEP_STOCK = [(0x0840AAF0, 0x0840AAF0 + 504 * 5 * 2), (0x083B78A0, 0x083B78A0 + 504 * 5 * 2)]


def _steps(radius=8):
    """neighbour offsets, nearest first (green has 6 bits = half weight)"""
    out = []
    for dr in range(-radius, radius + 1):
        for dg in range(-2 * radius, 2 * radius + 1):
            for db in range(-radius, radius + 1):
                if dr or dg or db:
                    out.append((max(abs(dr), abs(dg) / 2, abs(db)),
                                abs(dr) + abs(dg) / 2 + abs(db), dr, dg, db))
    out.sort()
    return [o[2:] for o in out]


STEPS = _steps()


# Our own PERFORM pads (tools/perform_pads.py) carry a copy of the stock
# hot-cue pad art and four blue pool colours: added as extra regions so they
# follow the theme too (2026-10-13). On the deck the per-chunk stock
# fingerprint skips them when the pads are not installed (RAM = zero there).
OWN_COLOURS = ("DIM", "KEDGE", "KFILL", "KPRESS")


def own_art():
    """-> ([(addr, bytes)] pad blobs, [(addr, n_px)] extra regions)"""
    import perform_pads as PP
    blobs = PP.blobs()
    regs = [(PP.PADART, PP.PADART_DATA.size)]
    want = {getattr(PP, n) for n in OWN_COLOURS}
    for base, b in blobs:
        if base in (PP.PADART,) or base >= PP.FONTP:
            continue
        for o in range(0, len(b) - 3, 4):
            if (base + o) % 4:
                continue
            v = struct.unpack_from("<I", b, o)[0]
            if v in want:                   # pool literal 0x0000CCCC
                regs.append((base + o, 1))  # low halfword = the colour
    return blobs, regs


def with_own_art(w):
    """stock words + our pad blobs written into the (zero) cave"""
    w = w.copy()
    for base, b in own_art()[0]:
        s = (base - IMG_BASE) // 2
        w[s:s + len(b) // 2] = np.frombuffer(b[:len(b) // 2 * 2], "<u2")
    return w


def regions(w, lit=False):
    import theme_bij as TB
    out = []
    spans = [(IMG_BASE + 2 * s, IMG_BASE + 2 * (s + n))
             for s, n in TB.regions_of(w)]
    for lo, hi in [NO_TOUCH] + LIVE_DATA:
        cut = []
        for a, b in spans:
            for x, y in ((a, min(b, lo)), (max(a, hi), b)):
                if y > x:
                    cut.append((x, y))
        spans = cut
    spans = _fill_gaps(w, spans)
    # 2026-11-04: the lit pictures only join the art when a theme actually
    # uses a lit colour -- they cost ~82 colours x every theme's table, and
    # the deck's table area is tight (9 themes fit without them)
    sprites = _sprite_spans() + (_lit_spans() if lit else [])
    spans = _no_zero_runs(w, _cut(_union(spans + sprites), [NO_TOUCH] + LIVE_DATA + KEEP_STOCK),
                          keep=sprites)
    return [(a, (b - a) // 2) for a, b in spans] + own_art()[1]


SPRITE_MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mods",
                          "sprite_map_light.json")


def _sprite_spans():
    """2026-10-26 light theme: every artwork picture the firmware copies to
    the screen (tools/sprite_log.py; white/grey-only digits and labels the
    colour detector never sees). Colour themes leave neutral pixels alone,
    so for them this changes nothing."""
    import json
    try:
        rects = json.load(open(SPRITE_MAP))["rects"]
    except (OSError, ValueError, KeyError):
        return []
    out = []
    for off, st, sx, sy, w, h in rects:
        for j in range(h):
            a = IMG_BASE + off + 2 * ((sy + j) * st + sx)
            out.append((a, a + 2 * w))
    return out


def _lit_spans():
    """2026-11-04: every lit touch-button picture (mods/litbuttons.py) is
    art: QUANTIZE / MASTER lit+pressed were partly outside the regions."""
    import litbuttons as LB
    return [(a, a + 2 * LB.W * LB.H) for a in LB.addresses()]


def _union(spans):
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _cut(spans, holes):
    for lo, hi in holes:
        cut = []
        for a, b in spans:
            for x, y in ((a, min(b, lo)), (max(a, hi), b)):
                if y > x:
                    cut.append((x, y))
        spans = cut
    return spans


ZERO_RUN = 256          # px
LIGHT_BG_PX = 50000     # a neutral used on >= this many px = background (light)


def _split_keep(a, b, keep):
    """[(lo, hi, inside)] covering [a, b): inside-keep pieces flagged"""
    segs, cur = [], a
    for k0, k1 in keep:
        if k1 <= cur or k0 >= b:
            continue
        if k0 > cur:
            segs.append((cur, k0, 0))
        segs.append((max(cur, k0), min(b, k1), 1))
        cur = min(b, k1)
        if cur >= b:
            break
    if cur < b:
        segs.append((cur, b, 0))
    return segs


def _no_zero_runs(w, spans, keep=()):
    """2026-10-26 light theme: black (0x0000) is a source colour now, and an
    all-zero stretch may be a working buffer the firmware fills later rather
    than artwork. Runs of >= ZERO_RUN zero pixels are taken out of the
    regions (they stay as they are in every theme) -- EXCEPT inside `keep`
    spans (the sprite rects the firmware demonstrably blits to the screen:
    artwork by construction, even when a sprite is all black -- 2026-10-26
    night: the UTILITY popup's black patches)."""
    out = []
    keep = _union(sorted(keep))
    for a, b in spans:
        for lo, hi, inside in _split_keep(a, b, keep):
            if inside:
                out.append((lo, hi))
                continue
            s0, s1 = (lo - IMG_BASE) // 2, (hi - IMG_BASE) // 2
            z = w[s0:s1] == 0
            if not z.any():
                out.append((lo, hi))
                continue
            edges = np.flatnonzero(np.diff(np.concatenate(([0], z.astype(np.int8), [0]))))
            cur = s0
            for i in range(0, len(edges), 2):
                zs, ze = s0 + edges[i], s0 + edges[i + 1]
                if ze - zs >= ZERO_RUN:
                    if zs > cur:
                        out.append((IMG_BASE + 2 * cur, IMG_BASE + 2 * zs))
                    cur = ze
            if s1 > cur:
                out.append((IMG_BASE + 2 * cur, IMG_BASE + 2 * s1))
    return out


GAP_WIN = 1024          # px
GAP_DENSITY = 0.90      # share of themable colours that marks a window as art


def _fill_gaps(w, spans):
    """2026-10-18: the artwork detector leaves holes inside sprite sheets
    (deck photo: the 'hot cue set' band on pad A stayed stock blue; source
    sprite at 0x0840C000.., between detected regions). Add every GAP_WIN-px
    window that is >= 90 % themable colours and not already covered (never
    inside NO_TOUCH). The deck's per-chunk stock fingerprint still guards."""
    import themes as T
    lo_img, hi_img = min(a for a, _ in spans), max(b for _, b in spans)
    allc = np.arange(65536, dtype=np.uint16)
    themable = T.map_colours(allc, T.THEMES["crimson-red"]) != allc
    cov = np.zeros((hi_img - lo_img) // 2, bool)
    for a, b in spans:
        cov[(a - lo_img) // 2:(b - lo_img) // 2] = True
    nt0, nt1 = NO_TOUCH
    add = []
    s0 = (lo_img - IMG_BASE) // 2
    for k in range(0, len(cov) - GAP_WIN + 1, GAP_WIN):
        if cov[k:k + GAP_WIN].all():
            continue
        a = lo_img + 2 * k
        if a + 2 * GAP_WIN > nt0 and a < nt1:
            continue
        seg = w[s0 + k:s0 + k + GAP_WIN]
        if themable[seg].mean() >= GAP_DENSITY:
            add.append((a, a + 2 * GAP_WIN))
    out = sorted(spans + add)
    merged = []
    for a, b in out:                       # union (keeps every pixel once)
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return _bridge(w, merged)


BRIDGE_MAX = 0x4000     # bytes
BRIDGE_DENSITY = 0.60


def _bridge(w, spans):
    """2026-10-25: lit-button sprites (VINYL BRAKE / SYNC lit state, deck
    photo: stock stripes) sit in small holes BETWEEN detected regions whose
    1024-px windows fall under GAP_DENSITY because of white text pixels
    (0x0887F000..0x08880000 = top half of lit VINYL BRAKE). A hole of at most
    BRIDGE_MAX bytes with art on BOTH sides and >= 60 % themable colours is
    art too. Unthemable pixels are never changed by the maps anyway."""
    import themes as T
    allc = np.arange(65536, dtype=np.uint16)
    themable = T.map_colours(allc, T.THEMES["crimson-red"]) != allc
    nt0, nt1 = NO_TOUCH
    out = [spans[0]]
    for a, b in spans[1:]:
        pa, pb = out[-1]
        g = a - pb
        s = (pb - IMG_BASE) // 2
        if (0 < g <= BRIDGE_MAX and not (a > nt0 and pb < nt1)
                and themable[w[s:s + g // 2]].mean() >= BRIDGE_DENSITY):
            out[-1] = (pa, b)
        else:
            out.append((a, b))
    return out


def fingerprints(w, regs, S):
    """-> (themable-chunk bitmap, u32 FNV-1a per themable chunk) over CHUNK
    px of every region in stock colours -- identical to ts.c recolour()"""
    inS = np.zeros(65536, bool)
    inS[S] = True
    bits, out = [], []
    for a, n in regs:
        s = (a - IMG_BASE) // 2
        for k in range(0, n, CHUNK):
            v = w[s + k:s + min(n, k + CHUNK)]
            if not inS[v].any():
                bits.append(0)
                continue
            bits.append(1)
            h = 0x811C9DC5
            for x in v.tolist():
                h = ((h ^ x) * 0x01000193) & 0xFFFFFFFF
            out.append(h)
    return (np.packbits(np.array(bits, np.uint8), bitorder="little").tobytes(),
            np.array(out, "<u4").tobytes())


def present_colours(w, regs):
    """-> (present mask, pixel count per colour) inside the regions"""
    cnt = np.zeros(65536, np.int64)
    for a, n in regs:
        s = (a - IMG_BASE) // 2
        cnt += np.bincount(w[s:s + n], minlength=65536)
    return cnt > 0, cnt


# 2026-11-04 lit touch buttons (mods/litbuttons.py): a theme saved with
# spec['lit'] = '#rrggbb' lights QUANTIZE, MASTER, SYNC, SLIP and VINYL
# (BRAKE) in that colour, exactly like the Studio preview. ts.c has ONE
# colour table per theme and the lit pictures share most of their colours
# with other art, so when some theme has a lit colour the installer first
# moves the lit pictures' shared colours to PRIVATE colours 1-2 LSB away
# (invisible: OFF looks stock). The tables then map a private colour like
# its stock colour in a normal theme and to the lit colour in a lit theme.
# Only the lit pictures' pixels are rewritten, only while they are stock.

def lit_private(w, regs):
    """-> (w_mod, grp): w with the lit pictures' shared colours moved to
    private ones; grp {colour in w_mod: (stock colour, in stripe rows)} for
    every lit-picture colour the lit maths may change"""
    import litbuttons as LB
    import themes as T
    inreg = np.zeros(len(w), bool)
    for a, n in regs:
        o = (a - IMG_BASE) // 2
        inreg[o:o + n] = True
    idx = np.concatenate([np.arange((a - IMG_BASE) // 2, (a - IMG_BASE) // 2 + LB.W * LB.H)
                          for a in LB.addresses()])
    top = np.tile(LB.top_rows().ravel(), len(LB.addresses()))
    if not inreg[idx].all():
        raise RuntimeError("lit button pictures outside the theme regions")
    c = w[idx]
    _h, sat, val = LB._hsv(c)
    lab = (sat < 0.25) & (val > 0.6) & (c != LB.KEY)          # labels, stripe
    chg = ((sat >= 0.25) & (c != LB.KEY)) | lab              # lit maths may change
    # coloured pixels map by colour alone; labels also by stripe/body
    role = np.where(lab, 1 + top.astype(np.int64), 0)[chg]
    idx, c = idx[chg], c[chg]
    keys, inv, kc = np.unique(c.astype(np.int64) * 4 + role,
                              return_inverse=True, return_counts=True)
    kcol, ktop = (keys // 4).astype(np.int64), keys % 4 == 2
    cnt = np.bincount(w[inreg], minlength=65536)
    litcnt = np.bincount(kcol, weights=kc, minlength=65536).astype(np.int64)
    keep = np.zeros(len(keys), bool)
    for col in np.unique(kcol):           # a colour ONLY on lit pictures keeps
        ks = np.flatnonzero(kcol == col)  # itself for its biggest group
        if cnt[col] == litcnt[col]:
            keep[ks[np.argmax(kc[ks])]] = True
    used = cnt > 0
    used[list(T.KEYS)] = True
    newc = kcol.copy()
    for k in np.argsort(-kc, kind="stable").tolist():   # most pixels nearest
        if keep[k]:
            continue
        c0 = int(kcol[k])
        r, g, b = (c0 >> 11) & 31, (c0 >> 5) & 63, c0 & 31
        for dr, dg, db in STEPS:
            rr, gg, bb = r + dr, g + dg, b + db
            if 0 <= rr < 32 and 0 <= gg < 64 and 0 <= bb < 32:
                c2 = (rr << 11) | (gg << 5) | bb
                if not used[c2]:
                    break
        else:
            raise RuntimeError("lit buttons: no free private colour")
        used[c2] = True
        newc[k] = c2
    w_mod = w.copy()
    w_mod[idx] = newc[inv].astype(np.uint16)
    grp = {int(newc[k]): (int(kcol[k]), bool(ktop[k])) for k in range(len(keys))}
    return w_mod, grp


def theme_names(args):
    """names from the command line; NAME@#rrggbb = theme NAME with that lit
    colour (a test theme, e.g. check crimson-red@#20d060)"""
    import themes as T
    out = []
    for n in args:
        if "@" in n:
            base, lit = n.split("@", 1)
            n = (base[:11] + "-lit").lower()
            T.THEMES[n] = dict(T.THEMES[base], lit=lit)
        out.append(n)
    return out


def tables_full(names, w=None):
    """-> w_mod (= w unless a theme has a lit colour), regs, S (sorted),
    [D_t], forbidden mask, grp (see lit_private)"""
    import themes as T
    import theme_deck as TD
    import litbuttons as LB
    w0 = with_own_art(TD.stock_words()) if w is None else w
    lits = [T.THEMES[n]["lit"] for n in names if T.THEMES[n].get("lit")]
    regs = regions(w0, lit=bool(lits))
    w, grp = lit_private(w0, regs) if lits else (w0, {})
    ref = LB.reference_from_words(w0) if lits else None
    pb, cnt = present_colours(w, regs)
    cnt0 = present_colours(w0, regs)[1] if lits else cnt
    cols = np.nonzero(pb)[0].astype(np.uint16)
    src = cols.copy()                      # the stock colour each one stands for
    ing = np.zeros(len(cols), bool)
    gtop = np.zeros(len(cols), bool)
    if grp:
        pos = np.searchsorted(cols, np.array(list(grp), np.uint16))
        assert np.array_equal(cols[pos], np.array(list(grp), np.uint16))
        src[pos] = [g[0] for g in grp.values()]
        gtop[pos] = [g[1] for g in grp.values()]
        ing[pos] = True
    changed = np.zeros(len(cols), bool)
    maps = {}
    for n in names:
        spec = T.THEMES[n]
        if spec.get("light"):
            # greys covering huge areas are backgrounds (light), rare ones ink
            m = T.light_map(src, bg=cnt0[src] >= LIGHT_BG_PX)
        else:
            m = T.map_colours(src, spec)
        if spec.get("lit") and ing.any():
            m[ing] = LB.map_keys(src[ing], gtop[ing], spec["lit"], ref)
        m = np.where(m == src, cols, m)    # a private colour left alone stays
        maps[n] = m
        changed |= m != cols
    S = cols[changed]
    F = pb.copy()
    F[S] = False
    # most-used colours claim their exact target first; rarer ones get
    # nudged (keeps the visible difference to the preview tiny)
    order = np.argsort(-cnt[S], kind="stable")
    Ds = []
    for n in names:
        want = maps[n][changed]
        used = F.copy()
        D = np.zeros(len(S), np.uint16)
        for k in order.tolist():
            c = int(want[k])
            if used[c]:
                r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
                for dr, dg, db in STEPS:
                    rr, gg, bb = r + dr, g + dg, b + db
                    if 0 <= rr < 32 and 0 <= gg < 64 and 0 <= bb < 32:
                        c2 = (rr << 11) | (gg << 5) | bb
                        if not used[c2]:
                            c = c2
                            break
                else:
                    raise RuntimeError(f"{n}: no free colour")
            used[c] = True
            D[k] = c
        Ds.append(D)
    return w, regs, S, Ds, F, grp


def tables(names, w=None):
    """-> regs, S (sorted), [D_t], forbidden mask"""
    return tables_full(names, w)[1:5]


MAXS = 4096                                # ts.c: more source colours = no-op


_BUILT = {}


def build(names, w=None):
    """-> (deck data blob, lit writes [(addr, stock bytes, private bytes)])
    -- the lit writes go into RAM BEFORE the switcher is armed"""
    import theme_deck as TD
    key = tuple(names) if w is None else None     # cache the default build
    if key in _BUILT:
        return _BUILT[key]
    w0 = with_own_art(TD.stock_words()) if w is None else w
    w, regs, S, Ds, _, grp = tables_full(names, w0)
    if len(S) > MAXS:
        raise RuntimeError(f"{len(S)} theme colours > {MAXS}: fewer themes")
    ns, nt = len(S), len(names)
    reg_off = 56
    s_off = reg_off + 8 * len(regs)
    d_off = s_off + 2 * ns
    name_off = d_off + 2 * ns * nt
    name_off += (-name_off) % 4
    tbits, hashes = fingerprints(w, regs, S)
    tbits_off = name_off + 16 * nt
    hash_off = tbits_off + len(tbits)
    hash_off += (-hash_off) % 4
    out = struct.pack("<8I4B5I", MAGIC, len(regs), ns, nt, reg_off, s_off,
                      d_off, name_off, 0, 0, 0, 0, 0, hash_off, CHUNK, 0,
                      tbits_off)
    out += bytes(reg_off - len(out))
    for a, n in regs:
        out += struct.pack("<II", a, n)
    out += S.astype("<u2").tobytes()
    for D in Ds:
        out += D.astype("<u2").tobytes()
    out += bytes(name_off - len(out))
    for n in names:
        out += n.upper().encode()[:15].ljust(16, b"\0")
    assert len(out) == tbits_off
    out += tbits
    out = out + bytes(hash_off - len(out)) + hashes
    writes = []
    if grp:
        import litbuttons as LB
        for a in LB.addresses():
            o = (a - IMG_BASE) // 2
            st, pv = w0[o:o + LB.W * LB.H], w[o:o + LB.W * LB.H]
            if not np.array_equal(st, pv):
                writes.append((a, st.astype("<u2").tobytes(), pv.astype("<u2").tobytes()))
    if key is not None:
        _BUILT.clear()
        _BUILT[key] = (out, writes)
    return out, writes


def blob(names, w=None):
    return build(names, w)[0]


def lit_writes(names, w=None):
    return build(names, w)[1]


def model_switch(img, regs, frm, to):
    """exactly what ts.c does (per-pixel FROM -> TO inside the regions)"""
    lut = np.arange(65536, dtype=np.uint16)
    lut[np.asarray(frm)] = np.asarray(to, np.uint16)
    out = img.copy()
    for a, n in regs:
        s = (a - IMG_BASE) // 2
        out[s:s + n] = lut[img[s:s + n]]
    return out


def default_names():
    import themes as T
    return [n for n, s in T.THEMES.items() if s is not None]


def check(names):
    import themes as T
    import theme_deck as TD
    import litbuttons as LB
    w0 = with_own_art(TD.stock_words())
    w, regs, S, Ds, F, grp = tables_full(names, w0)
    own = np.zeros(len(w), bool)                 # our pads: not in the preview
    for a, n in own_art()[1]:
        own[(a - IMG_BASE) // 2:(a - IMG_BASE) // 2 + n] = True
    litpx = np.zeros(len(w), bool)
    for a in LB.addresses():
        litpx[(a - IMG_BASE) // 2:(a - IMG_BASE) // 2 + LB.W * LB.H] = True
    b, writes = build(names, w0)
    npx = sum(n for _, n in regs)
    print(f"{len(names)} themes, S {len(S)} colours (max {MAXS}), {len(regs)} regions, "
          f"{npx / 1e6:.2f} M px, blob {len(b) / 1024:.1f} KB "
          f"(ends {TS_DATA + len(b):#x}, limit {TS_END:#x})")
    if grp:
        npriv = sum(1 for k, (c0, _t) in grp.items() if k != c0)
        moved = int((w != w0).sum())
        e = _lsb(w[w != w0], w0[w != w0])
        print(f"  lit buttons: {len(grp)} colours, {npriv} private, {moved:,} px moved "
              f"(max {int(e.max()) if moved else 0} LSB), {len(writes)} pictures to write")
        ok = moved == int(((w != w0) & litpx).sum())     # only lit pixels moved
    else:
        ok = True
    ok &= TS_DATA + len(b) <= TS_END and len(S) <= MAXS
    for D in Ds:
        ok &= len(set(D.tolist())) == len(D) and not F[D].any()
    # cycle stock -> every theme -> stock, in order and some jumps
    cur, img = 0, w.copy()
    seq = list(range(1, len(names) + 1)) + [0, len(names), 2 if len(names) > 1 else 1, 0]
    for t in seq:
        frm = S if cur == 0 else Ds[cur - 1]
        to = S if t == 0 else Ds[t - 1]
        img = model_switch(img, regs, frm, to)
        direct = model_switch(w, regs, S, to)
        ok &= np.array_equal(img, direct)
        cur = t
    ok &= np.array_equal(img, w)
    print("stock -> all themes -> jumps -> stock exact:", np.array_equal(img, w))
    # how close each theme is to the Studio preview (themes.recolour)
    worst = 0.0
    for n, D in zip(names, Ds):
        spec = T.THEMES[n]
        if spec.get("light"):
            got = model_switch(w, regs, S, D)
            print(f"  {n:14s} {int((got != w).sum()):>9,} px changed (light: no Studio-preview "
                  f"comparison; tools/light_theme_proto.py is its reference)")
            continue
        ref = T.recolour(w0.copy(), spec)[0]
        if spec.get("lit"):
            LB.apply_words(ref, w0, spec["lit"])
        got = model_switch(w, regs, S, D)
        ch = (ref != w0) & ~own
        bad = (got != ref) & ch & ~own     # where the reference themes too
        extra = (got != w0) & ~ch & ~own & ~litpx   # gap-filled art (2026-10-18)
        e = _lsb(got[bad], ref[bad]) if bad.any() else np.zeros(1, int)
        # 2026-10-18: calmer themes = closer colours = more bijection nudges;
        # judge what is VISIBLE: share of themed pixels off by > 3/31
        far = int((e > 3).sum()) if bad.any() else 0
        frac = far / max(1, int(ch.sum()))
        worst = max(worst, frac)
        msg = ""
        if spec.get("lit"):
            lb = (got != ref) & litpx & (ref != w0)
            el = _lsb(got[lb], ref[lb]) if lb.any() else np.zeros(1, int)
            msg = (f"; lit {spec['lit']}: {int((litpx & (ref != w0)).sum()):,} px, "
                   f"{int((el > 3).sum())} > 3 LSB (max {int(el.max())})")
            ok &= int((el > 3).sum()) <= 0.002 * max(1, int((litpx & (ref != w0)).sum()))
        print(f"  {n:14s} {int(ch.sum()):>9,} px themed; vs Studio preview: "
              f"{int(bad.sum()):>7,} px off (max {int(e.max())} LSB), "
              f"{far:,} px > 3 LSB ({100 * frac:.3f} %), mean {e.sum() / max(1, ch.sum()):.2f}; "
              f"+{int(extra.sum()):,} px gap-filled art{msg}")
    ok &= worst <= 0.002   # <= 0.2 % of themed pixels visibly different
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _lsb(g, r):
    """per pixel: biggest channel difference in 5-bit steps"""
    g, r = np.asarray(g).astype(int), np.asarray(r).astype(int)
    return np.maximum(np.maximum(abs((g >> 11) - (r >> 11)),
                                 abs(((g >> 5) & 63) - ((r >> 5) & 63)) // 2),
                      abs((g & 31) - (r & 31)))


def build_code():
    import mysettings as MS
    env = dict(os.environ, TS_BASE=hex(TS_BASE), TS_DATA=hex(TS_DATA),
               MSTNAME=hex(MS.MSTNAME), MSTAPP=hex(MS.MSTAPP))
    import shbuild
    # absolute, from this file's place in the repo: the emulator app calls
    # this from whatever cwd it was launched with ("/" under Finder)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ts_bin = os.path.join(root, "build", "themesw", "ts.bin")
    ts_syms = os.path.join(root, "build", "themesw", "ts.syms")
    shbuild.build(os.path.join(root, "tools", "themesw", "build.sh"),
                  [ts_bin, ts_syms], env, cwd=root)
    code = open(ts_bin, "rb").read()
    syms = {}
    for ln in open(ts_syms):
        a, _, s = ln.split()
        syms[s] = int(a, 16)
    assert syms["_ts_table"] == TS_BASE
    return code, syms


def plan(names):
    """[(addr, bytes)] for the deck (code + data), table at TS_BASE"""
    code, _ = build_code()
    data = blob(names)
    assert TS_BASE + len(code) <= TS_DATA
    if TS_DATA + len(data) > TS_END:     # 2026-11-04: lit colours ~5 KB per theme
        raise RuntimeError(f"{len(names)} themes need {len(data) // 1024} KB of deck tables, "
                           f"room for {(TS_END - TS_DATA) // 1024} KB: delete a few saved themes")
    return [(TS_BASE, code), (TS_DATA, data)]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    names = theme_names(sys.argv[2:]) or default_names()
    if cmd == "check":
        return check(names)
    if cmd == "plan":
        for a, b in plan(names):
            print(f"{a:#010x} {len(b):6d} B")
        print(f"survey window {TS_BASE:#x}..{TS_END:#x}")
        return 0
    if cmd == "export":                  # prebuilt files for the emulator app
        (cb, code), (db, data) = plan(names)
        open("build/themesw/tables.bin", "wb").write(data)
        open("build/themesw/layout.txt", "w").write(
            f"{cb:#x} {db:#x} {' '.join(names)}\n")
        print(f"build/themesw/ts.bin {len(code)} B @ {cb:#x}, tables.bin "
              f"{len(data)} B @ {db:#x}, {len(names)} themes")
        return 0
    if cmd == "emu":
        import mysettings as MS
        return MS.emu_theme_switch(names)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
