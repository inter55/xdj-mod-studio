"""XDJ-700 screen themes.

One engine, several palettes. Works on RGB565 pixels inside windows that are
provably bitmap data; code, strings, tables, neutrals and the 0xF81F / 0x07E0
transparency keys are never touched.

Colour is remapped in HSV. Only pixels whose hue falls in the stock UI's blue
band and whose saturation is high enough to be "coloured" are moved, so white
text, grey chrome, the red/orange cue markers and the green USB LED survive.
"""
import json
import re
import os

import numpy as np

WIN  = 1024
KEYS = (0xF81F, 0x07E0, 0x0000, 0xFFFF)
CODE = (0x4F22, 0x4F26, 0x2FE6, 0x6EF6, 0x2FD6, 0x6DF6, 0x000B)

# stock UI blues live around hue 200-240 deg
HUE_LO, HUE_HI = 190.0, 260.0
# the PERFORM "active" purple (button + screen outline, ~58k px, hue
# 262..298): a second family, mapped onto the theme's own hue (+ optional
# spec['accent'] degrees) -- added 2026-10-13, it used to stay purple in
# every theme
ACC_LO, ACC_HI = 262.0, 298.0
SAT_MIN = 0.16
DISTINCT_MAX = 64      # code windows never go below 76
LUT_MAX = 0.30         # reject index/lookup tables (adjacent values +/-1)

THEMES = {
    # hue = target hue in degrees; sat/val = multipliers; spread = how much of
    # the source hue variation to carry through (keeps gradients from flattening)
    'stock':         None,
    # 2026-10-18 (user): 8 clearly different hues (were 15 with near-
    # duplicates). Only the PERFORM accent was too saturated -> see
    # ACCENT_CALM in _map_all; the main colours keep their full look.
    'ice-cyan':      dict(hue=189.0, sat=1.35, val=1.10, spread=0.45, vivid=0.5, lift=0.1),
    'forest-green':  dict(hue=132.0, sat=1.15, val=0.94, spread=0.50, vivid=0.5, lift=0.1),
    'amber-gold':    dict(hue= 44.0, sat=1.30, val=1.06, spread=0.50, vivid=0.5, lift=0.1),
    'sunset-orange': dict(hue= 20.0, sat=1.30, val=1.02, spread=0.50, vivid=0.5, lift=0.1),
    'crimson-red':   dict(hue=352.0, sat=1.35, val=0.98, spread=0.40, vivid=0.5, lift=0.1),
    'hot-pink':      dict(hue=328.0, sat=1.30, val=1.04, spread=0.45, vivid=0.5, lift=0.1),
    'neon-purple':   dict(hue=286.0, sat=1.45, val=1.08, spread=0.45, vivid=0.5, lift=0.1),
    'slate-gray':    dict(hue=215.0, sat=0.18, val=1.02, spread=0.30),
    # 2026-10-26: light mode -- every colour's lightness flips (whites and
    # greys with extra contrast so dim grey text stays readable on white),
    # hues kept, the 0xF81F transparency key untouched. On the deck it also
    # needs tools/lighttext (text, waveform window, screen clears).
    'light':         dict(light=True),
}


def _to_hsv(r, g, b):
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = (mx - mn).astype(np.float32)
    v = mx.astype(np.float32) / 255.0
    s = np.where(mx > 0, d / np.maximum(mx, 1), 0.0)
    h = np.zeros_like(d)
    nz = d > 0
    rm = nz & (mx == r); gm = nz & (mx == g) & ~rm; bm = nz & ~rm & ~gm
    h[rm] = ((g[rm] - b[rm]) / d[rm]) % 6.0
    h[gm] = ((b[gm] - r[gm]) / d[gm]) + 2.0
    h[bm] = ((r[bm] - g[bm]) / d[bm]) + 4.0
    return h * 60.0, s, v


def _to_rgb(h, s, v):
    h = np.mod(h, 360.0) / 60.0
    i = np.floor(h).astype(np.int32)
    f = h - i
    p = v * (1 - s); q = v * (1 - s * f); t = v * (1 - s * (1 - f))
    i = i % 6
    r = np.select([i==0,i==1,i==2,i==3,i==4,i==5], [v,q,p,p,t,v])
    g = np.select([i==0,i==1,i==2,i==3,i==4,i==5], [t,v,v,q,p,p])
    b = np.select([i==0,i==1,i==2,i==3,i==4,i==5], [p,p,t,v,v,q])
    f8 = lambda x: np.clip(x * 255.0, 0, 255)
    return f8(r), f8(g), f8(b)


def art_windows(w):
    """Boolean mask over WIN-sized windows: is this bitmap data?

    Measured against 28 windows of visually confirmed artwork and 1,722 windows
    containing SH-4 prologue opcodes:

        distinct colours / 1024 px   art max 75   code min 76    SEPARABLE
        near-neighbour smoothness    art min 0.63 code max 0.88  OVERLAPS

    An earlier version ANDed the two. That was wrong: crisp white button text
    produces hard edges, so real sprite sheets score as "rough" as code does,
    and the whole USB/LINK button sheet was silently excluded from theming.

    So the test is `distinct` (which separates) plus two guards against data
    that is low-entropy without being pictures: any SH-4 prologue/epilogue
    opcode disqualifies a window, and so does a high proportion of adjacent
    values differing by exactly 1, which is the signature of an index table.

    The threshold sits at 64, twelve below the observed code floor of 76,
    rather than at the boundary.
    """
    nb = len(w) // WIN
    w2 = w[:nb * WIN].reshape(nb, WIN)

    ws = np.sort(w2, axis=1)
    distinct = (ws[:, 1:] != ws[:, :-1]).sum(1) + 1

    step1 = (np.abs(np.diff(w2.astype(np.int32), axis=1)) == 1).mean(1)

    code = np.zeros(nb, bool)
    for op in CODE:
        code |= (w2 == op).any(1)

    return (distinct <= DISTINCT_MAX) & (step1 < LUT_MAX) & ~code, nb


_WORD16 = re.compile(rb'(?:[\x20-\x7e\n]\x00){5,}')


def text_windows(w, nb):
    """Windows that hold UTF-16 text -- never recoloured by the heuristic.

    The UI message table (English, Russian, CJK...) is UTF-16LE, and ASCII
    characters are small numbers that read as saturated dark-blue RGB565
    pixels ('U' = 0x0055). The window test accepted the table as artwork,
    so every theme built before this guard rewrote 1,388 of the 2,393
    English messages (found by booting a theme in the emulator: "USB Error"
    came out as CJK glyphs). A window is text if >= 40% of it is printable
    ASCII (pixel data: ~2%; the table: 70-95%) or it holds a real word
    string; gaps of up to 8 windows between text windows (the CJK and
    Cyrillic blocks inside the table) and one window either side are
    included."""
    w2 = w[:nb * WIN].reshape(nb, WIN)
    txt = ((w2 >= 0x20) & (w2 < 0x7F)).mean(1) >= 0.40
    buf = w[:nb * WIN].tobytes()
    for m in _WORD16.finditer(buf):
        if m.start() % 2:
            continue
        t = m.group().decode('utf-16-le')
        if re.search(r'[a-z]{2}', t) and len(set(t)) >= 5:
            txt[m.start() // (2 * WIN):(m.end() - 1) // (2 * WIN) + 1] = True
    idx = np.flatnonzero(txt)
    for a, b in zip(idx[:-1], idx[1:]):
        if 1 < b - a <= 9:
            txt[a:b] = True
    grown = txt.copy()
    grown[1:] |= txt[:-1]
    grown[:-1] |= txt[1:]
    return grown


SPRITE_MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'sprite_map.json')


def proven_sprites(n_words):
    """Pixel mask of block1 rectangles the firmware itself blits as images
    (measured in the emulator by tools/sprite_map.py). Empty if the map is
    missing or was measured on a block of a different size."""
    mask = np.zeros(n_words, bool)
    try:
        m = json.load(open(SPRITE_MAP))
    except (OSError, ValueError):
        return mask
    if m.get('blk1_len', 0) // 2 != n_words:
        return mask
    for off, st, sx, sy, w, h in m['rects']:
        for j in range(h):
            a = off // 2 + (sy + j) * st + sx
            mask[a:a + w] = True
    return mask


def map_hsv(h, s, val, spec):
    """The theme colour transform (shared by recolour = the deck tables and
    the Mod Studio preview, so they can never disagree).
    vivid (0..1, optional): pushes saturation toward full -- the stock UI is
      low-saturation slate, so multiplying alone gives muddy colours.
    lift  (0..1, optional): raises dark fills toward bright (glow)."""
    mid = (HUE_LO + HUE_HI) / 2.0
    nh = spec['hue'] + (h - mid) * spec['spread']          # keep relative shading
    ns = np.clip(s * spec['sat'], 0, 1)
    if spec.get('vivid'):
        ns = ns + spec['vivid'] * (1.0 - ns) * 0.8
    nv = np.clip(val * spec['val'], 0, 1)
    if spec.get('lift'):
        nv = nv + spec['lift'] * (1.0 - nv) * 0.5
    return nh, ns, nv


def _select(h, s, v16):
    """-> (main family, accent family) masks; keys never touched"""
    main = (h >= HUE_LO) & (h <= HUE_HI) & (s >= SAT_MIN)
    acc = (h >= ACC_LO) & (h <= ACC_HI) & (s >= SAT_MIN)
    for k in KEYS:
        main &= (v16 != k)
        acc &= (v16 != k)
    return main, acc


def _map_all(h, s, val, spec, acc):
    """map_hsv for both families (the accent is re-centred on its own band
    and lands on the theme hue + spec['accent'])"""
    h2 = np.where(acc, h - (ACC_LO + ACC_HI) / 2.0 + (HUE_LO + HUE_HI) / 2.0
                  + spec.get('accent', 0.0), h)
    nh, ns, nv = map_hsv(h2, s, val, spec)
    # 2026-10-18 (user photo): the accent = the lit PERFORM button + PERFORM
    # screen outline; at full vivid it glared. Calm the accent only.
    calm = dict(spec, sat=spec['sat'] * ACCENT_CALM[0],
                vivid=spec.get('vivid', 0.0) * ACCENT_CALM[1],
                val=spec['val'] * ACCENT_CALM[2])
    ch, cs, cv = map_hsv(h2, s, val, calm)
    return nh, np.where(acc, cs, ns), np.where(acc, cv, nv)


ACCENT_CALM = (0.55, 0.0, 0.85)   # sat, vivid, val factors for the accent


def light_map(c, bg=None):
    """uint16 RGB565 array -> light-mode colours: neutrals l' = (1-l)^3
    ("ink": dim grey labels stay readable), colours l' = 1 - 0.88 l with
    sat x0.8, key 0xF81F kept. bg (bool per colour, optional): neutral
    BACKGROUND greys (used across huge areas: row stripes, panels) get the
    plain flip l' = 1 - 0.88 l instead, so they come out light
    (2026-10-26: BROWSE / UTILITY rows were mid grey)."""
    c = np.asarray(c, np.uint16)
    v = c.astype(np.int64)
    r = ((v >> 11) & 31) / 31.0
    g = ((v >> 5) & 63) / 63.0
    b = (v & 31) / 31.0
    mx, mn = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b)
    l = (mx + mn) / 2
    d = mx - mn
    with np.errstate(divide='ignore', invalid='ignore'):
        s = np.where(d == 0, 0.0, d / np.where(l > 0.5, 2 - mx - mn, mx + mn + 1e-12))
    neutral = s < 0.25
    ink = neutral if bg is None else neutral & ~np.asarray(bg, bool)
    l2 = np.clip(np.where(ink, (1 - l) ** 4.5, 1 - 0.88 * l), 0, 1)
    s2 = s * 0.8
    # hls -> rgb (vectorised colorsys)
    rc = np.where(d == 0, 0.0, (mx - r) / np.where(d == 0, 1, d))
    gc = np.where(d == 0, 0.0, (mx - g) / np.where(d == 0, 1, d))
    bc = np.where(d == 0, 0.0, (mx - b) / np.where(d == 0, 1, d))
    h = np.where(r == mx, bc - gc, np.where(g == mx, 2.0 + rc - bc, 4.0 + gc - rc))
    h = np.where(d == 0, 0.0, (h / 6.0) % 1.0)
    m2 = np.where(l2 <= 0.5, l2 * (1 + s2), l2 + s2 - l2 * s2)
    m1 = 2 * l2 - m2

    def _v(hue):
        hue = hue % 1.0
        return np.where(hue < 1 / 6, m1 + (m2 - m1) * hue * 6,
               np.where(hue < 0.5, m2,
               np.where(hue < 2 / 3, m1 + (m2 - m1) * (2 / 3 - hue) * 6, m1)))
    rr = np.where(s2 == 0, l2, _v(h + 1 / 3))
    gg = np.where(s2 == 0, l2, _v(h))
    bb = np.where(s2 == 0, l2, _v(h - 1 / 3))
    out = (np.round(rr * 31).astype(np.int64) << 11) | (np.round(gg * 63).astype(np.int64) << 5) \
        | np.round(bb * 31).astype(np.int64)
    return np.where(c == 0xF81F, 0xF81F, out).astype(np.uint16)


def map_colours(c, spec):
    """uint16 RGB565 array -> themed colours (same selection as recolour:
    blue-ish UI colours only, transparency keys untouched)."""
    if spec and spec.get('light'):
        return light_map(c)
    c = np.asarray(c, np.uint16)
    v16 = c.astype(np.uint32)
    r = (((v16 >> 11) & 0x1F) * 255 // 31).astype(np.float32)
    g = (((v16 >> 5) & 0x3F) * 255 // 63).astype(np.float32)
    b = ((v16 & 0x1F) * 255 // 31).astype(np.float32)
    h, s, val = _to_hsv(r, g, b)
    main, acc = _select(h, s, v16)
    sel = main | acc
    nh, ns, nv = _map_all(h, s, val, spec, acc)
    nr, ng, nb_ = _to_rgb(nh, ns, nv)
    enc = ((nr.astype(np.uint32) >> 3) << 11) | ((ng.astype(np.uint32) >> 2) << 5) | \
        (nb_.astype(np.uint32) >> 3)
    return np.where(sel, enc, v16).astype(np.uint16)


def recolour(w, spec):
    """w: uint16 RGB565 array. Returns (new array, n pixels changed)."""
    if spec is None:
        return w.copy(), 0
    art, nb = art_windows(w)
    out = w.copy()
    v16 = w[:nb * WIN].astype(np.uint32)
    r = (((v16 >> 11) & 0x1F) * 255 // 31).astype(np.float32)
    g = (((v16 >>  5) & 0x3F) * 255 // 63).astype(np.float32)
    b = (( v16        & 0x1F) * 255 // 31).astype(np.float32)
    h, s, val = _to_hsv(r, g, b)

    # the window heuristic (minus anything that holds text) OR rectangles
    # the firmware is seen blitting: smooth gradients trip the lookup-table
    # guard, so without the map a button sprite was themed in bands
    heur = art & ~text_windows(w, nb)
    pic = np.repeat(heur, WIN) | proven_sprites(len(w))[:nb * WIN]
    main, acc = _select(h, s, v16)
    sel = pic & (main | acc)
    acc &= pic
    if not sel.any():
        return out, 0

    nh, ns, nv = _map_all(h, s, val, spec, acc)
    nr, ng, nb_ = _to_rgb(nh, ns, nv)

    r2 = np.where(sel, nr, r); g2 = np.where(sel, ng, g); b2 = np.where(sel, nb_, b)
    enc = ((r2.astype(np.uint32) >> 3) << 11) | \
          ((g2.astype(np.uint32) >> 2) <<  5) | \
           (b2.astype(np.uint32) >> 3)
    out[:nb * WIN] = enc.astype(np.uint16)
    return out, int(sel.sum())


def apply_to_block(dec, name):
    n = len(dec) // 2 * 2
    w = np.frombuffer(bytes(dec[:n]), dtype='<u2').copy()
    new, cnt = recolour(w, THEMES[name])
    res = bytearray(dec)
    res[:n] = new.tobytes()
    return res, cnt
