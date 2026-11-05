"""XDJ-700 top panel: geometry and drawing for the emulator window.

Every position is measured from Pioneer's product photo of the deck
(xdj-700_1.png, top view) and scaled by K so the firmware's 800x480 screen
lands at exactly its real share of the panel. P(x, y) converts a photo
coordinate; the deck outline in the photo is x 152-648, y 5-590.

Two regions, as on the real deck: the glossy raised top section (screen,
USB, BACK / TAG TRACK, rotary) and the brushed-metal lower section (loop,
search, CUE / PLAY, jog, tempo).
"""
import functools
import math
import os

import numpy as np
import pygame

K = 800 / 290.0               # photo px -> canvas px (screen is 290 px wide)
X0, Y0 = 152, 5               # deck's top-left corner in the photo


def P(x, y):
    return int(round((x - X0) * K)), int(round((y - Y0) * K))


def S(r):
    return int(round(r * K))


DECK_W, DECK_H = S(496), S(585)
SCR_X, SCR_Y = P(255, 45)
SCR_W, SCR_H = 800, 480
TOP_H = S(220)                # glossy section ends here

INK = (236, 236, 240)
DIM = (150, 152, 160)
YELLOW = (255, 214, 40)

# name -> (centre, radius, label, PANL key, style). All round, as on the deck.
CONTROLS = {
    "BACK":     (P(573, 68), S(11), "BACK", "Back", "black"),
    "TAGTRACK": (P(607, 68), S(8.5), "TAG TRACK\n/REMOVE", "Tagtrack", "black"),
    "USBSTOP":  (P(210, 125), S(11.5), "USB STOP", None, "black"),
    "IN":       (P(203, 258), S(15.5), "IN / 4BEAT", "LoopIn", "yellow"),
    "OUT":      (P(252, 258), S(15.5), "OUT", "LoopOut", "yellow"),
    "RELOOP":   (P(310, 258), S(10.5), "RELOOP/EXIT", "ReloopExit", "reloop"),
    "TRK-":     (P(202, 320), S(12.5), "@|<<", "TrackRev", "search"),
    "TRK+":     (P(236, 320), S(12.5), "@>>|", "TrackFwd", "search"),
    "SCH-":     (P(202, 368), S(12.5), "@<<", "ScanRev", "search"),
    "SCH+":     (P(236, 368), S(12.5), "@>>", "ScanFwd", "search"),
    "CUE":      (P(218, 458), S(25), "CUE", "Cue", "cue"),
    "PLAY":     (P(218, 535), S(25), "@>/||", "PlayPause", "play"),
    "VINYL":    (P(540, 258), S(9.5), "VINYL MODE", "JogModeVinyl", "led_blue"),
    "RANGE":    (P(591, 258), S(8.5), "TEMPO\n±6/±10/±16/WIDE", "TempoRange", "black"),
    "MT":       (P(592, 291), S(8.5), "MASTER TEMPO", "MasterTempo", "led_orange"),
}
JOG_C, JOG_R = P(405, 405), S(140)        # outer (dimple ring) radius
JOG_IN = S(106)                           # matte platter radius
ROT_C, ROT_R = P(590, 120), S(25)
_fx, _fy0 = P(592, 330)
_fy1 = P(592, 553)[1]
FADER = pygame.Rect(_fx - S(14), _fy0, S(28), _fy1 - _fy0)


# ---------------------------------------------------------------- helpers
def _vgrad(w, h, top, bot):
    s = pygame.Surface((w, h))
    for y in range(h):
        t = y / max(1, h - 1)
        pygame.draw.line(s, [int(top[i] + (bot[i] - top[i]) * t) for i in range(3)],
                         (0, y), (w, y))
    return s


@functools.lru_cache(maxsize=256)
def radial(r, inner, outer, hi=None, lo=None):
    """Radial-gradient disc, cached; hi = top-left highlight RGBA."""
    s = pygame.Surface((2 * r + 2, 2 * r + 2), pygame.SRCALPHA)
    for i in range(r, 0, -1):
        t = i / r
        pygame.draw.circle(s, [int(inner[k] + (outer[k] - inner[k]) * t) for k in range(3)],
                           (r + 1, r + 1), i)
    if hi:
        # soft sheen, weaker on big discs (a sharp blob reads as plastic)
        g = pygame.Surface(s.get_size(), pygame.SRCALPHA)
        a = int(hi[3] * min(1.0, 40.0 / r))
        pygame.draw.ellipse(g, hi[:3] + (a,), (int(r * .45), int(r * .18), int(r * 1.1), int(r * .6)))
        s.blit(g, (0, 0))
    return s


def disc(surf, c, r, inner, outer, hi=None):
    surf.blit(radial(r, inner, outer, hi), (c[0] - r - 1, c[1] - r - 1))


def text(surf, font, s, c, col=INK, anchor="c"):
    t = font.render(s, True, col)
    x = c[0] - t.get_width() // 2 if anchor == "c" else c[0]
    surf.blit(t, (x, c[1] - t.get_height() // 2))
    return t.get_width()


def glyph(surf, spec, cx, cy, s, col):
    """Transport symbols as shapes: '<<', '>>', '|<<', '>>|', '>/||'."""
    if spec == ">/||":
        items = [("tri>", -s * .9), ("bar", s * .75), ("bar", s * 1.35)]
    else:
        ks = [{"<": "tri<", ">": "tri>", "|": "bar"}[ch] for ch in spec]
        w = s * 1.1
        items = [(k, -w * (len(ks) - 1) / 2 + i * w) for i, k in enumerate(ks)]
    for kind, dx in items:
        x = cx + dx
        if kind == "tri>":
            pygame.draw.polygon(surf, col, [(x - s * .55, cy - s * .7), (x + s * .55, cy),
                                            (x - s * .55, cy + s * .7)])
        elif kind == "tri<":
            pygame.draw.polygon(surf, col, [(x + s * .55, cy - s * .7), (x - s * .55, cy),
                                            (x + s * .55, cy + s * .7)])
        else:
            pygame.draw.rect(surf, col, (x - s * .14, cy - s * .7, max(2, s * .28), s * 1.4))


ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def load_image(path):
    """PNG -> Surface via Pillow: this pygame build has no SDL_image
    (pygame.image.get_extended() is False), so pygame.image.load only
    reads BMP and PNG logos / the window icon silently failed to load."""
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    return pygame.image.fromstring(im.tobytes(), im.size, "RGBA")


def logo_img(surf, name, centre, width):
    """Blit a logo from emu/assets scaled to width, centred. False if missing."""
    try:
        im = load_image(os.path.join(ASSETS, name))
    except Exception:
        return False
    box = im.get_bounding_rect()
    im = im.subsurface(box)
    h = int(width * box.h / box.w)
    im = pygame.transform.smoothscale(im, (width, h))
    surf.blit(im, (centre[0] - width // 2, centre[1] - h // 2))
    return True


# ------------------------------------------------------------- background
def background(fonts):
    """Static panel: body, the two sections, printing, fixed hardware."""
    small, label, big, logo = fonts
    s = pygame.Surface((DECK_W, DECK_H), pygame.SRCALPHA)
    body = pygame.Rect(0, 0, DECK_W, DECK_H)
    pygame.draw.rect(s, (6, 6, 7), body, border_radius=S(10))
    # lower section: dark brushed metal (horizontal grain)
    low = _vgrad(DECK_W - S(6), DECK_H - TOP_H - S(6), (58, 59, 63), (34, 35, 38))
    px = pygame.surfarray.pixels3d(low)
    rng = np.random.default_rng(3)
    grain = rng.integers(-6, 7, size=(1, px.shape[1], 1))
    px[:] = np.clip(px.astype(int) + grain, 0, 255).astype(np.uint8)
    del px
    s.blit(low, (S(3), TOP_H + S(3)))
    # top section: glossy black, raised, with a soft sheen
    top = _vgrad(DECK_W - S(4), TOP_H, (30, 30, 33), (8, 8, 9))
    s.blit(top, (S(2), S(2)))
    sheen = pygame.Surface((DECK_W, TOP_H // 3), pygame.SRCALPHA)
    for y in range(sheen.get_height()):
        pygame.draw.line(sheen, (255, 255, 255, int(14 * (1 - y / sheen.get_height()))),
                         (0, y), (DECK_W, y))
    s.blit(sheen, (0, S(2)))
    pygame.draw.line(s, (90, 91, 96), (S(3), TOP_H + S(2)), (DECK_W - S(3), TOP_H + S(2)), 2)
    pygame.draw.rect(s, (70, 70, 74), body, 2, border_radius=S(10))

    # --- top section printing / hardware
    logo_img(s, "pioneer_dj_white.png", P(400, 27), S(122)) or \
        text(s, logo, "XDJ-700", P(400, 27))      # no third-party logos
    pc = P(566, 16)                           # power symbol, drawn
    pygame.draw.arc(s, DIM, (pc[0] - 8, pc[1] - 8, 16, 16), math.radians(120),
                    math.radians(420), 2)
    pygame.draw.line(s, DIM, (pc[0], pc[1] - 10), (pc[0], pc[1] - 1), 2)
    text(s, small, "•  SLEEP", P(592, 16), DIM)
    # screen bezel
    bz = pygame.Rect(SCR_X - S(4), SCR_Y - S(4), SCR_W + S(8), SCR_H + S(8))
    pygame.draw.rect(s, (2, 2, 3), bz, border_radius=6)
    pygame.draw.rect(s, (55, 56, 60), bz, 1, border_radius=6)
    # USB port
    u = pygame.Rect(*P(192, 51), S(42), S(40))
    pygame.draw.rect(s, (40, 41, 45), u, border_radius=S(4))
    pygame.draw.rect(s, (110, 112, 118), u, 2, border_radius=S(4))
    slot = pygame.Rect(u.x + S(8), u.y + S(13), u.w - S(16), S(13))
    pygame.draw.rect(s, (160, 162, 168), slot, border_radius=3)
    pygame.draw.rect(s, (10, 10, 11), slot.inflate(-S(6), -S(5)))
    text(s, small, "USB", P(213, 44), DIM)
    text(s, small, "—  USB STOP", P(212, 106), INK)
    # rekordbox badge: centred on the USB column, just under USB STOP
    if not logo_img(s, "rekordbox_white.png", P(212, 152), S(56)):
        rb = P(193, 152)
        pygame.draw.circle(s, (214, 40, 50), rb, S(4.5), 3)
        pygame.draw.circle(s, (214, 40, 50), rb, S(1.6))
        text(s, label, "rekordbox", (rb[0] + S(7), rb[1]), INK, anchor="l")
    text(s, small, "MP3/AAC/WAV/AIFF", P(212, 163), DIM)
    # model plate
    text(s, small, "MULTI PLAYER", P(588, 182), INK)
    text(s, big, "XDJ-700", P(588, 194))
    text(s, small, "BACK", P(579, 51), INK)
    bx, by = P(566, 51)                       # back arrow, drawn
    pygame.draw.lines(s, INK, False, [(bx + 7, by + 5), (bx + 7, by - 3), (bx - 5, by - 3)], 2)
    pygame.draw.polygon(s, INK, [(bx - 8, by - 3), (bx - 3, by - 7), (bx - 3, by + 1)])

    # --- lower section printing
    text(s, small, "• IN / — 4BEAT", P(203, 237), INK)
    text(s, small, "OUT", P(252, 237), INK)
    text(s, small, "RELOOP/EXIT", P(310, 237), INK)
    text(s, small, "LOOP", P(283, 268), INK)
    pygame.draw.line(s, INK, P(222, 258), P(234, 258), 2)
    for c, t in ((P(203, 282), "IN ADJUST"), (P(252, 282), "OUT ADJUST")):
        r = pygame.Rect(0, 0, S(34), S(8))
        r.center = c
        pygame.draw.rect(s, (22, 22, 24), r, border_radius=3)
        pygame.draw.rect(s, (120, 122, 128), r, 1, border_radius=3)
        text(s, small, t, c, DIM)
    text(s, small, "TRACK SEARCH", P(219, 299), INK)
    text(s, small, "SEARCH", P(219, 348), INK)
    text(s, label, "PLAY / PAUSE", P(218, 497), INK)
    text(s, small, "VINYL MODE", P(538, 237), INK)
    text(s, small, "MASTER TEMPO", P(592, 276), INK)
    # jog: shadow well, chrome rim, dimple ring base, platter
    # glossy black bezel the jog sits in (no chrome on the real deck)
    disc(s, JOG_C, JOG_R + S(14), (3, 3, 4), (12, 12, 13))
    pygame.draw.circle(s, (54, 55, 60), JOG_C, JOG_R + S(14), 1)
    text(s, label, "—  REV", P(292, 541), INK)
    text(s, label, "+  FWD", P(512, 541), INK)
    # tempo fader: dark panel with a ribbed rail, dotted scale on the
    # left (- / 0 / +), TEMPO printed inside at the bottom (product photo)
    f = FADER
    pan = pygame.Rect(0, 0, f.w + S(18), f.h + S(34))
    pan.midtop = (f.centerx, f.y - S(8))
    pygame.draw.rect(s, (8, 8, 9), pan.inflate(S(3), S(3)), border_radius=S(4))
    pygame.draw.rect(s, (24, 24, 27), pan, border_radius=S(3))
    pygame.draw.rect(s, (70, 71, 76), pan, 1, border_radius=S(3))
    for y in range(f.y, f.bottom, S(3)):                     # rail ribs
        for x0, x1 in ((pan.x + S(4), f.centerx - S(4)),
                       (f.centerx + S(4), pan.right - S(4))):
            pygame.draw.line(s, (52, 53, 58), (x0, y), (x1, y), 1)
            pygame.draw.line(s, (14, 14, 16), (x0, y + 1), (x1, y + 1), 1)
    pygame.draw.rect(s, (2, 2, 3), (f.centerx - S(2), f.y, S(4), f.h))  # slot
    sx = pan.x - S(10)
    for y in range(f.y + S(4), f.bottom - S(2), S(4)):          # dotted scale
        pygame.draw.circle(s, (150, 152, 158), (sx, y), 1)
    text(s, label, "–", (sx - S(8), f.y + S(6)))
    text(s, small, "0", (sx - S(8), f.centery))
    text(s, label, "+", (sx - S(8), f.bottom - S(4)))
    text(s, small, "TEMPO", (f.centerx, pan.bottom - S(10)))
    return s


# ---------------------------------------------------------------- controls
def draw_control(surf, name, lit, fonts):
    small, label, big, logo = fonts
    (cx, cy), r, lab, key, style = CONTROLS[name]
    pygame.draw.circle(surf, (4, 4, 5), (cx, cy), r + S(2.2))
    if style == "yellow":
        disc(surf, (cx, cy), r, (255, 236, 120) if lit else (212, 176, 40),
             (230, 180, 20) if lit else (150, 120, 25), (255, 255, 255, 60))
    elif style == "cue" or style == "play":
        ring = (255, 150, 30) if style == "cue" else (70, 230, 100)
        pygame.draw.circle(surf, ring if lit else (60, 60, 64), (cx, cy), r + S(1.6), S(1.4))
        disc(surf, (cx, cy), r, (246, 246, 250) if lit else (228, 229, 233),
             (140, 142, 148), (255, 255, 255, 70))
        pygame.draw.circle(surf, (170, 172, 178), (cx, cy), int(r * .72), 1)
        if style == "cue":
            text(surf, big, "CUE", (cx, cy), (255, 130, 0) if lit else (205, 110, 20))
        else:
            glyph(surf, ">/||", cx, cy, S(6), (40, 200, 70) if lit else (40, 150, 60))
        return
    else:
        disc(surf, (cx, cy), r, (120, 122, 128) if lit else (58, 59, 64),
             (18, 18, 20), (255, 255, 255, 30))
        pygame.draw.circle(surf, (140, 142, 148), (cx, cy), r, 1)
        if style == "reloop":
            pygame.draw.circle(surf, YELLOW if lit else (120, 100, 30), (cx, cy), int(r * .55), S(1.3))
        elif style == "led_blue":
            pygame.draw.circle(surf, (90, 200, 255) if lit else (30, 70, 100), (cx, cy), int(r * .45))
        elif style == "led_orange":
            pygame.draw.circle(surf, (255, 140, 30) if lit else (100, 55, 15), (cx, cy), int(r * .45))
        elif style == "search":
            glyph(surf, lab[1:], cx, cy, S(3.6), (220, 222, 228))
    if not lab.startswith("@") and name not in ("VINYL", "MT", "USBSTOP", "IN", "OUT",
                                                 "RELOOP", "BACK", "CUE"):
        lines = lab.split("\n")
        y = cy - r - S(3) - 17 * (len(lines) - 1)
        for ln in lines:
            text(surf, small, ln, (cx, y), INK)
            y += 17


_JOG = {}


def _bowl(w, h):
    """One scalloped finger bowl of the jog ring, long side horizontal: a
    dark DISH -- light only on its far rim, darkest toward the near side,
    like the photo's oval dimples (a bright centre reads as a bump)."""
    b = pygame.Surface((w + 4, h + 4), pygame.SRCALPHA)
    pygame.draw.ellipse(b, (64, 65, 70, 255), (2, 2, w, h))          # rim light
    for k in range(9):
        t = k / 8.0
        v = int(34 - 22 * t)
        ww, hh = int(w * (0.94 - .45 * t)), int(h * (0.9 - .45 * t))
        r = pygame.Rect(0, 0, ww, hh)
        r.center = (w // 2 + 2, h // 2 + 2 + int(h * .07 * t))
        pygame.draw.ellipse(b, (v, v, v + 2, 255), r)
    pygame.draw.ellipse(b, (8, 8, 9, 255), (2, 2, w, h), 1)
    return b


def _jog_ring():
    """The ring of 24 bowls + the row of small dots inside it, at 0 deg."""
    if "ring" in _JOG:
        return _JOG["ring"]
    R = JOG_R
    ring = pygame.Surface((2 * R + 4, 2 * R + 4), pygame.SRCALPHA)
    c = (R + 2, R + 2)
    disc(ring, c, R, (30, 30, 33), (16, 16, 18))
    rc = (JOG_IN + S(9) + R) / 2.0
    bw = int(2 * math.pi * rc / 24 * 0.97)
    bh = int((R - JOG_IN - S(9)) * 0.92)
    sprite = _bowl(bw, bh)
    for i in range(24):
        a = i * 2 * math.pi / 24
        rot = pygame.transform.rotozoom(sprite, -math.degrees(a) - 90, 1)
        x, y = c[0] + rc * math.cos(a), c[1] + rc * math.sin(a)
        ring.blit(rot, (x - rot.get_width() / 2, y - rot.get_height() / 2))
    rd = JOG_IN + S(6)
    for i in range(72):                                   # perforation dots
        a = i * 2 * math.pi / 72
        pygame.draw.circle(ring, (16, 16, 18),
                           (int(c[0] + rd * math.cos(a)), int(c[1] + rd * math.sin(a))), max(1, S(1.3)))
    _JOG["ring"] = ring
    return ring


def _jog_platter(touched):
    key = ("plat", touched)
    if key in _JOG:
        return _JOG[key]
    r = JOG_IN
    p = pygame.Surface((2 * r + 4, 2 * r + 4), pygame.SRCALPHA)
    c = (r + 2, r + 2)
    pygame.draw.circle(p, (6, 6, 7), c, r + 2)               # raised rim edge
    pygame.draw.circle(p, (66, 67, 72), c, r + 1, 1)
    base = 30 if touched else 20
    for k in range(r, 0, -2):                               # brushed rings
        v = base + (2 if (k // 2) % 2 else 0) + int(6 * (1 - k / r))
        pygame.draw.circle(p, (v, v, v + 2), c, k)
    sheen = pygame.Surface(p.get_size(), pygame.SRCALPHA)   # diagonal gloss
    for k in range(16):
        a0 = math.radians(-24 + k * 1.6)
        a1 = math.radians(-52 - k * 1.6)
        pts = [c, (c[0] + r * math.cos(a0), c[1] + r * math.sin(a0)),
               (c[0] + r * math.cos(a1), c[1] + r * math.sin(a1))]
        pygame.draw.polygon(sheen, (255, 255, 255, 2), pts)
        pts = [c, (c[0] - r * math.cos(a0), c[1] - r * math.sin(a0)),
               (c[0] - r * math.cos(a1), c[1] - r * math.sin(a1))]
        pygame.draw.polygon(sheen, (255, 255, 255, 1), pts)
    p.blit(sheen, (0, 0))
    _JOG[key] = p
    return p


def draw_jog(surf, jog_count, touched, fonts):
    """Bowl ring that turns with the jog (cached per degree: the ring
    repeats every 15 deg) and the glossy black platter."""
    deg = int(jog_count * 0.0025 * 180 / math.pi) % 15
    key = ("rot", deg)
    if key not in _JOG:
        _JOG[key] = pygame.transform.rotozoom(_jog_ring(), -deg, 1)
    r = _JOG[key]
    surf.blit(r, (JOG_C[0] - r.get_width() // 2, JOG_C[1] - r.get_height() // 2))
    pl = _jog_platter(bool(touched))
    surf.blit(pl, (JOG_C[0] - pl.get_width() // 2, JOG_C[1] - pl.get_height() // 2))


def draw_rotary(surf, rotary, pushed):
    """The browse push-encoder: a short rubber knob with fine knurling on
    the SIDE and a smooth slightly-domed top -- no pointer line (the real
    one is a continuous encoder). The knurling rotates as you turn it."""
    c = ROT_C
    # recess + chrome collar
    pygame.draw.circle(surf, (6, 6, 7), c, ROT_R + S(5))
    disc(surf, c, ROT_R + S(3), (150, 152, 158), (70, 72, 78))
    # side knurling (fine ticks around the rim)
    for i in range(48):
        a = i * math.pi / 24 + rotary * 0.18
        r0, r1 = ROT_R - S(3), ROT_R + S(1)
        pygame.draw.line(surf, (24, 24, 27),
                         (c[0] + r0 * math.cos(a), c[1] + r0 * math.sin(a)),
                         (c[0] + r1 * math.cos(a), c[1] + r1 * math.sin(a)), 1)
    # domed rubber top
    top = (86, 88, 94) if pushed else (58, 59, 64)
    disc(surf, c, ROT_R - S(3), top, (26, 27, 30), (255, 255, 255, 36))
    pygame.draw.circle(surf, (22, 22, 25), c, ROT_R - S(3), 1)


def draw_fader(surf, fader_raw):
    """Black slider cap with a white index line and grip ridges."""
    f = FADER
    fy = f.y + int(fader_raw / 499.0 * (f.h - S(12)))
    cap = pygame.Rect(0, 0, f.w + S(10), S(20))
    cap.midtop = (f.centerx, fy - S(4))
    pygame.draw.rect(surf, (0, 0, 0), cap.inflate(4, 6), border_radius=3)
    surf.blit(pygame.transform.smoothscale(
        _vgrad(8, 8, (62, 63, 68), (16, 16, 18)), cap.size), cap)
    for k in range(1, 4):                                   # grip ridges
        y = cap.y + k * cap.h // 8
        pygame.draw.line(surf, (80, 81, 86), (cap.x + 2, y), (cap.right - 3, y), 1)
        y = cap.bottom - k * cap.h // 8
        pygame.draw.line(surf, (8, 8, 9), (cap.x + 2, y), (cap.right - 3, y), 1)
    pygame.draw.line(surf, (245, 245, 248), (cap.x + 2, cap.centery),
                     (cap.right - 3, cap.centery), 2)
    pygame.draw.rect(surf, (96, 97, 103), cap, 1, border_radius=2)
