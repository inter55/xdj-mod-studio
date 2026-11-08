#!/usr/bin/env python3
"""aafont.py -- anti-aliased fonts for the PERFORM-screen mods, matched to
the XDJ-700's own pad lettering.

  PAD font (14-px caps): digits 1 2 4 6 8 are the firmware's OWN glyphs,
      lifted from the stock BEAT LOOP pads (tools/data/stock_pad_digits.npz,
      captured from the emu framebuffer = the deck's artwork); 0 3 5 7 9 + -
      are synthesised into the same 9x14 box from the closest system face;
      < > are anti-aliased triangles (the CUE/LOOP CALL arrow style).
  SMALL font (11-px caps): A-Z 0-9 + - fitted to the stock "CALL"/"REC" pad
      text.

Blob layout (what the SH renderer in perform_pads.py reads):
  +0 u8 H (rows)  +1 u8 space advance  +2,+3 0
  +4  MAP[64]  ASCII 32..95 -> glyph index, 0xFF = none
  +68 n x {u8 w, u8 advance, u16 offset-from-blob-start}
  then alpha bytes 0..32 (row-major, w per row, H rows)
  aafont.py preview   -> screenshots/aafont_preview.png
"""
import os
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys_path_root = os.path.dirname(HERE)


def _stock():
    """pad digits from the firmware home (made from the user's own update
    file; see emu/fwhome.py)"""
    import sys
    if sys_path_root not in sys.path:
        sys.path.insert(0, sys_path_root)
    from emu import fwhome
    return fwhome.path("pad_digits.npz")
NEUE = "/System/Library/Fonts/HelveticaNeue.ttc"
CONDENSE = 0.84      # Neue Condensed Bold width / Arial Bold width (measured)


def _font_file(path, index):
    """Helvetica Neue only exists on a Mac. Elsewhere (Windows) use Arial:
    every glyph is scaled to the target cap height, so only the letter
    shapes differ slightly. Neue faces 0/1 regular, 4/10 bold/medium."""
    if os.path.exists(path):
        return path, index
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    bold = index not in (0, 1)
    for name in (("arialbd.ttf", "seguisb.ttf") if bold else ("arial.ttf", "segoeui.ttf")):
        p = os.path.join(win, name)
        if os.path.exists(p):
            return p, 0
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
              else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
        if os.path.exists(p):
            return p, 0
    return path, index          # let truetype raise the real error


def _lum(a):
    a = a.astype(np.uint32)
    return (((a >> 11) & 31) * 255 / 31 * 0.3 + ((a >> 5) & 63) * 255 / 63 * 0.59
            + (a & 31) * 255 / 31 * 0.11)


def _alpha_from_pad(pad, y0=19, y1=33):
    """Pad crop (53 x 79 RGB565) -> alpha 0..1 of the white label rows."""
    L = _lum(pad)[y0:y1, 10:69]          # inside the pad borders
    bg = np.median(L, axis=1, keepdims=True)
    a = np.clip((L - bg) / (255.0 - bg), 0, 1)
    m = a.max()                          # dimmed (no track) or lit pads:
    return a / m if m > 0.05 else a      # same glyph shape either way


def _cols(al):
    xs = np.nonzero(al.max(axis=0) > 0.08)[0]
    if len(xs) == 0:
        return []
    # split into glyphs on empty columns
    groups, cur = [], [xs[0]]
    for x in xs[1:]:
        if x == cur[-1] + 1:
            cur.append(x)
        else:
            groups.append(cur)
            cur = [x]
    groups.append(cur)
    return [al[:, g[0]:g[-1] + 1] for g in groups]


def _render(ch, font_path, index, box_h, box_w=None, cap_ref="H"):
    """Supersampled render of ch scaled so cap height == box_h."""
    S = 8
    want = index
    font_path, index = _font_file(font_path, index)
    # Helvetica Neue Condensed Bold (face 4) is narrow; the stand-in used
    # where it doesn't exist (Arial Bold on Windows) is ~16% wider, which
    # pushed "<< BEAT JUMP >>" past its 92-px tab (2026-10-21 report: the
    # tab name vanished). Squeeze the stand-in to the Mac's widths.
    squeeze = CONDENSE if (want == 4 and not os.path.exists(NEUE)) else 1.0
    f = ImageFont.truetype(font_path, 40 * S // 4, index=index)
    im = Image.new("L", (f.size * 3, f.size * 3), 0)
    ImageDraw.Draw(im).text((f.size, f.size * 2), cap_ref, font=f, fill=255,
                            anchor="ls")
    cap = np.nonzero(np.array(im).max(axis=1) > 128)[0]
    cap_top, base = cap[0], f.size * 2
    im = Image.new("L", (f.size * 3, f.size * 3), 0)
    ImageDraw.Draw(im).text((f.size, base), ch, font=f, fill=255, anchor="ls")
    a = np.array(im, float)[cap_top:base]
    xs = np.nonzero(a.max(axis=0) > 20)[0]
    if len(xs) == 0:
        return np.zeros((box_h, 3))
    a = a[:, xs[0]:xs[-1] + 1]
    scale = box_h / a.shape[0]
    w = box_w or max(1, round(a.shape[1] * scale * squeeze))
    out = Image.fromarray(a.astype(np.uint8)).resize((w, box_h), Image.LANCZOS)
    return np.clip(np.array(out, float) / 255.0, 0, 1)


def _triangle(w, h, left, pad_top, H):
    S = 8
    im = Image.new("L", (w * S, H * S), 0)
    y0, y1 = pad_top * S, (pad_top + h) * S
    pts = ([(w * S, y0), (w * S, y1), (0, (y0 + y1) // 2)] if left else
           [(0, y0), (0, y1), (w * S, (y0 + y1) // 2)])
    ImageDraw.Draw(im).polygon(pts, fill=255)
    return np.array(im.resize((w, H), Image.LANCZOS), float) / 255.0


def _stock_digits():
    """The firmware's own 1 2 4 6 8, or None when the captured pads hold no
    digits (a first-run capture that missed the PERFORM screen -- seen on
    Windows 2026-11: every PERFORM install and the emulator crashed with
    'index 0 is out of bounds for axis 0 with size 0')."""
    try:
        z = np.load(_stock())
        g = {}
        one_six = _cols(_alpha_from_pad(z["16"]))
        if len(one_six) < 2:
            return None
        g["1"], g["6"] = one_six[:2]
        for d in "248":
            cols = _cols(_alpha_from_pad(z[d]))
            if not cols:
                return None
            g[d] = cols[0]
        if len({g[d].shape[0] for d in "12468"}) != 1 or g["2"].shape[1] < 3:
            return None
        return g
    except Exception as e:                # missing/broken file: same answer
        print("aafont: stock pad digits unusable (%s); drawing our own" % e)
        return None


def pad_glyphs():
    g = _stock_digits()
    if g is None:                        # same 9x14 box, system face
        g = {d: _render(d, NEUE, 10, 14, 9) for d in "2468"}
        g["1"] = _render("1", NEUE, 10, 14)
    H = g["2"].shape[0]
    W = g["2"].shape[1]
    for d in "03579":
        g[d] = _render(d, NEUE, 10, H, W)              # Helvetica Neue Medium
    g["+"] = _render("+", NEUE, 10, H - 2)
    g["+"] = np.vstack([np.zeros((1, g["+"].shape[1])), g["+"],
                        np.zeros((1, g["+"].shape[1]))])
    m = np.zeros((H, 8))
    m[6:8, :] = 1.0
    g["-"] = m
    g["<"] = _triangle(9, 11, True, 1, H)
    g[">"] = _triangle(9, 11, False, 1, H)
    return H, g, 2


def _double(left, H, tw=6, th=7):
    """XDJ-1000 beat-jump tab icon: two touching triangles (<< or >>)."""
    a = _triangle(tw, th, left, (H - th) // 2, H)
    return np.hstack([a, a])


def small_glyphs():
    H = 11
    g = {}
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-":
        g[ch] = _render(ch, NEUE, 0, H)               # Helvetica Neue Regular
    g["["] = _double(True, H)                         # << (jump back)
    g["]"] = _double(False, H)                        # >> (jump forward)
    return H, g, 1


# Lower-case / special glyphs live on spare ASCII codes (the map covers
# 32..95): '^' = 'm' (minor), '_' = 'b' (flat), '\\' = the +/- sign.
KEY_SUBST = {"m": "^", "b": "_", "\u00b1": "\\"}


def tab_glyphs():
    """XDJ-1000 mode tabs: Helvetica Neue Condensed Bold, 9-px caps."""
    H = 9
    g = {ch: _render(ch, NEUE, 4, H) for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
    a = _render("+", NEUE, 4, 7)
    g["+"] = np.vstack([np.zeros((1, a.shape[1])), a, np.zeros((1, a.shape[1]))])
    m = np.zeros((H, 5))
    m[4, :] = 1.0
    g["-"] = m
    g["["] = _double(True, H, 5, 7)                   # << (jump back)
    g["]"] = _double(False, H, 5, 7)                  # >> (jump forward)
    return H, g, 1


def key_glyphs():
    """CDJ-3000 key-shift names (C#m, Ebm, +1, \u00b10): Condensed Bold,
    13-px caps, box tall enough for the lower-case m / b."""
    H = 15
    g = {}
    for ch in "ABCDEFG0123456789":
        g[ch] = np.vstack([_render(ch, NEUE, 4, 13), np.zeros((2, 1))[:, :0].reshape(0, 0) if False else np.zeros((2, _render(ch, NEUE, 4, 13).shape[1]))])
    sym = {"#": "#", "+": "+", "-": "\u2212"}
    for k, ch in sym.items():
        a = _render(ch, NEUE, 4, 9)
        g[k] = np.vstack([np.zeros((2, a.shape[1])), a, np.zeros((4, a.shape[1]))])
    for code, ch in (("^", "m"), ("_", "b"), ("\\", "\u00b1")):
        a = _render(ch, NEUE, 4, 13, cap_ref="H") if ch == "b" else _render(ch, NEUE, 4, 9, cap_ref="x" if ch == "m" else "H")
        top = 13 - a.shape[0]
        g[code] = np.vstack([np.zeros((top, a.shape[1])), a, np.zeros((2, a.shape[1]))])
    return H, g, 1


def blob(kind):
    H, g, gap = {"pad": pad_glyphs, "small": small_glyphs, "tab": tab_glyphs,
                 "key": key_glyphs}[kind]()
    space = 4 if kind in ("pad", "small") else 3
    names = sorted(g)
    mp = bytearray([0xFF] * 64)
    table, pool = bytearray(), bytearray()
    base = 68 + 4 * len(names)
    for i, ch in enumerate(names):
        mp[ord(ch) - 32] = i
        a = g[ch]
        assert a.shape[0] == H, (ch, a.shape)
        w = a.shape[1]
        table += struct.pack("<BBH", w, w + gap, base + len(pool))
        pool += bytes(np.clip(np.round(a * 32), 0, 32).astype(np.uint8).ravel())
    out = bytes([H, space, 0, 0]) + bytes(mp) + bytes(table) + bytes(pool)
    return out


def render_string(b, s):
    """Python model of the SH renderer (alpha image) -- for previews."""
    H, space = b[0], b[1]
    cols = []
    for ch in (s if any(c in s for c in "^_\\") else s.upper()):
        c = ord(ch) - 32
        gi = b[4 + c] if 0 <= c < 64 else 0xFF
        if gi == 0xFF:
            cols.append(np.zeros((H, space)))
            continue
        w, adv, off = struct.unpack_from("<BBH", b, 68 + 4 * gi)
        a = np.frombuffer(b, np.uint8, H * w, off).reshape(H, w) / 32.0
        cols.append(np.hstack([a, np.zeros((H, adv - w))]))
    return np.hstack(cols) if cols else np.zeros((H, 1))


def preview():
    z = np.load(STOCK)
    rows = []
    for kind, txt in (("pad", "< 16  32 >  4 8 1 2 64 + -"), ("small", "KEY+1 MIX ACAPELLA INSTR KEY-2"),
                      ("tab", "HOT CUE  BEAT JUMP  KEY SHIFT"), ("key", "C#^ E_^ D^  F# \\0 +1 -2")):
        a = render_string(blob(kind), txt)
        rows.append(np.pad(a, ((2, 2), (2, 2))))
    ref = _alpha_from_pad(z["16"])
    w = max(r.shape[1] for r in rows + [ref])
    img = np.vstack([np.pad(r, ((0, 0), (0, w - r.shape[1]))) for r in [ref] + rows])
    Image.fromarray((img * 255).astype(np.uint8)).resize(
        (img.shape[1] * 6, img.shape[0] * 6), Image.NEAREST).save(
        os.path.join(HERE, "..", "screenshots", "aafont_preview.png"))
    print({k: len(blob(k)) for k in ("pad", "small")})


if __name__ == "__main__":
    preview()
