"""Lit touch buttons: the colour maths shared by the Studio preview
(studio/app.py), the emulator's theme apply (emu/ui.py) and the deck's
theme tables (tools/theme_switch.py), so all three agree.

Each touch button is four 72x53 RGB565 pictures in block1 (firmware 1.15):
lit, lit+pressed, unlit, unlit+pressed; 0xF81F is the clear key. Found
2026-11-04 by logging the 2D engine's blits while tapping each button in
the emulator; x, y = where the deck draws it on the player screen (the
unlit picture matches the stock screenshot exactly there).

A theme may carry spec['lit'] = '#rrggbb': the lit pictures then take that
colour -- coloured pixels get its hue, saturation and brightness scaled by
one shared reference (so SLIP keeps its dark face and bright stripe, VINYL
BRAKE its lit face), and with a light colour the white labels turn dark.
The rule depends on a pixel's colour and on whether it sits in the top
TOP rows (the lit stripe), nothing else -- so the deck can do it with its
per-colour tables (see theme_switch.lit_private).
"""
import numpy as np

IMG_BASE = 0x08000000
W, H = 72, 53
KEY = 0xF81F
TOP = 10                # rows of the lit stripe: labels below it

# name: (lit picture, lit+pressed picture, x, y on the player screen)
BUTTONS = {
    "quantize": (0x0826AAF0, 0x0826C8C0, 642, 232),   # red
    "master":   (0x088647E0, 0x088665B0, 715, 341),   # orange
    "sync":     (0x08873660, 0x08875430, 642, 341),   # grey
    "slip":     (0x0886BF20, 0x0886DCF0, 18, 403),    # blue
    "vinyl":    (0x0887E940, 0x08880710, 18, 341),    # blue
    "brake":    (0x088824E0, 0x088842B0, None, None), # VINYL BRAKE, brake mode: red
}
SHOWN = ("quantize", "master", "sync", "slip", "vinyl")   # lit on the preview


def addresses():
    """every lit picture (lit and lit+pressed of every button)"""
    return [a for v in BUTTONS.values() for a in v[:2]]


def sprite(words, addr):
    """(53, 72) uint16 picture from block1 words (index 0 = 0x08000000)"""
    o = (addr - IMG_BASE) // 2
    return np.asarray(words[o:o + W * H], np.uint16).reshape(H, W)


def hex565(h):
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _hsv(c):
    import themes as T
    v = np.asarray(c, np.uint32)
    r = (((v >> 11) & 0x1F) * 255 // 31).astype(np.float32)
    g = (((v >> 5) & 0x3F) * 255 // 63).astype(np.float32)
    b = ((v & 0x1F) * 255 // 31).astype(np.float32)
    return T._to_hsv(r, g, b)


def reference(pictures):
    """(saturation, brightness) median over the coloured pixels of the lit
    pictures SHOWN on the player screen: one reference for every button."""
    allpx = np.concatenate([np.asarray(p, np.uint16).ravel() for p in pictures] or
                           [np.zeros(1, np.uint16)])
    _h, s, v = _hsv(allpx)
    col = (s >= 0.25) & (allpx != KEY)
    if not col.any():
        return 0.8, 0.5
    return float(np.median(s[col])), float(np.median(v[col]))


def reference_from_words(words):
    return reference([sprite(words, BUTTONS[k][0]) for k in SHOWN])


# measured 2026-11-04 (theme_switch.py check NAME@#rrggbb: white, grey, pure
# red/green, pastel, yellow, pink): the knee and the brightness ceiling move
# with the picked colour's saturation (grey 0.5 / 0.92 .. full 0.7 / 1.0),
# saturation never above CAP_S -- every pick then stays within 3 LSB of the
# preview on the deck (a handful of px at 4). Higher = more vivid, but white
# and pure colours speckle on the deck.
KNEE_GREY, KNEE_COLOUR, CAP_GREY, CAP_COLOUR, CAP_S = 0.5, 0.7, 0.92, 1.0, 0.92


def _roll(x, knee, cap):
    """saturation / brightness above the knee roll off towards CAP instead of
    clipping: a bright or saturated lit colour keeps the button's shading,
    and different stock colours stay different (the deck's one-to-one
    colour tables cannot merge them: clipped, hundreds of colours wanted
    the same white and the deck had to scatter them as pink/green specks;
    CAP < 1 keeps white off the corner of the colour cube, so the deck has
    room on every side for its one-to-one nudges)"""
    return np.where(x <= knee, x, knee + (cap - knee) * (1 - np.exp(-(x - knee) / (cap - knee))))


def map_keys(cols, top, colour, ref):
    """Lit colour for pixels of stock colour `cols` (RGB565 array) that sit
    in the stripe rows (`top` True) or below them. Neutrals and the clear
    key keep their colour, except bright labels under a light colour."""
    import themes as T
    cols = np.asarray(cols, np.uint16)
    top = np.broadcast_to(np.asarray(top, bool), cols.shape)
    h, s, v = _hsv(cols)
    coloured = (s >= 0.25) & (cols != KEY)
    c = hex565(colour)
    tr, tg, tb = (((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31)
    th, ts, tv = (float(x[0]) for x in T._to_hsv(*(np.array([x], np.float32) for x in (tr, tg, tb))))
    rs, rv = ref
    knee = KNEE_GREY + (KNEE_COLOUR - KNEE_GREY) * ts
    cap = CAP_GREY + (CAP_COLOUR - CAP_GREY) * ts
    ns = np.clip(_roll(s * ts / max(rs, 1e-3), knee, CAP_S), 0, 1)
    nv = np.clip(_roll(v * tv / max(rv, 1e-3), knee, cap), 0, 1)
    nr, ng, nb = T._to_rgb(np.full_like(h, th), ns, nv)
    enc = ((nr.astype(np.uint32) >> 3) << 11) | ((ng.astype(np.uint32) >> 2) << 5) | (nb.astype(np.uint32) >> 3)
    out = np.where(coloured, enc, cols.astype(np.uint32))
    # a light colour (yellow, white, light green...) under the stock white
    # label is unreadable: the label turns dark; the stripe stays light
    if (0.299 * tr + 0.587 * tg + 0.114 * tb) / 255.0 > 0.6:
        lab = (s < 0.25) & (v > 0.6) & (cols != KEY) & ~top
        k = np.clip((1.0 - v) * 0.5 * 255.0, 0, 255).astype(np.uint32)
        out = np.where(lab, ((k >> 3) << 11) | ((k >> 2) << 5) | (k >> 3), out)
    return out.astype(np.uint16)


def top_rows():
    """(53, 72) bool: the stripe rows of a picture"""
    t = np.zeros((H, W), bool)
    t[:TOP] = True
    return t


def recolour(px, colour, ref):
    """one stock lit picture (53, 72) -> in the lit colour"""
    px = np.asarray(px, np.uint16)
    return map_keys(px.ravel(), top_rows().ravel(), colour, ref).reshape(px.shape)


def apply_words(new, stock, colour, ref=None):
    """Write the lit pictures in `colour` into block1 words `new` (a themed
    copy of `stock`), from their stock pixels. In place; returns new."""
    ref = ref or reference_from_words(stock)
    for a in addresses():
        o = (a - IMG_BASE) // 2
        new[o:o + W * H] = recolour(sprite(stock, a), colour, ref).ravel()
    return new
