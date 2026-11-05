#!/usr/bin/env python3
"""tfx_glyphs.h: three glyph sets for the TOUCH FX panel (alpha 0..32),
rendered THROUGH aafont.py so the panel uses the deck's own lettering
(user, 2026-11-01: the old freelance Neue Medium render didn't match
the KEY SHIFT buttons / stock tabs):

  TG_*  buttons + button caps: Neue Condensed Bold, 10-px caps (the
        stock SYNC/MASTER/MEMORY-DELETE button weight)
  TS_*  small labels: Neue Regular, 11-px caps (the stock TRACK /
        CUE LOOP CALL label face)
  TT_*  the tab-strip label: Neue Condensed Bold, 9-px -- exactly the
        face perform_pads uses for the HOT CUE / KEY SHIFT / STEMS tabs
"""
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import numpy as np
import aafont as AF

CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789%-:/.+"
SETS = [
    ("TG", AF.NEUE, 4, 10),     # button caps: condensed bold
    ("TS", AF.NEUE, 0, 11),     # small labels: regular
    ("TT", AF.NEUE, 4, 9),      # tab strip: perform_pads tab face
]
out = []
for pre, path, idx, H in SETS:
    gl = [AF._render(c, path, idx, H) for c in CHARS]
    W = max(g.shape[1] for g in gl)
    out += [f"#define {pre}_H {H}\n#define {pre}_W {W}\n#define {pre}_N {len(CHARS)}\n",
            f"static const char {pre}_chars[] = \"{CHARS}\";\n",
            f"static const unsigned char {pre}_w[{len(CHARS)}] = {{"
            + ",".join(str(g.shape[1]) for g in gl) + "};\n",
            f"static const unsigned char {pre}_a[{len(CHARS)}][{H} * {W}] = {{\n"]
    for g in gl:
        a = np.zeros((H, W))
        a[:, :g.shape[1]] = g
        out.append("  {" + ",".join(str(int(round(v * 32))) for v in a.ravel()) + "},\n")
    out.append("};\n")
open(os.path.join(HERE, "tfx_glyphs.h"), "w").write("".join(out))
print("tfx_glyphs.h:", "; ".join(f"{p} h={H}" for p, _, _, H in SETS), "chars", len(CHARS))
